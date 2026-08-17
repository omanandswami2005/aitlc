"""`aitlc run` — structured Behave runner (FR-1, FR-1.7)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import typer
from aitlc.adapters.lambdatest import queue as remote_queue
from aitlc.config import AitlcConfig
from aitlc.core import behave_runner, journal, chrome_cdp
from aitlc.core import history as history_core
from aitlc.core import locks, toon
from aitlc.core.dotenv import load_dotenv
from aitlc.core.feature_select import split_line_spec
from aitlc.core.patterns import PatternLibrary
from aitlc.core.redact import redact_text
from aitlc.runtime import attach


def _all_failures_are_known_flakes(
    result: behave_runner.RunResult, library: PatternLibrary
) -> tuple[bool, list[dict]]:
    """FR-1.7: retry only when EVERY failure matches the pattern library.

    One unmatched failure among several is enough to stop retrying — a
    partially-known failure set could be masking a genuinely new bug behind
    a familiar-looking flake.

    A failed run with an EMPTY failures list (real case hit building this:
    behave's own JSON formatter can crash while handling a hook error,
    leaving the report with no parseable step data at all) must NOT
    vacuously count as "all known" — that's the run we have the least
    visibility into, the opposite of a safe-to-retry-blindly case.
    """
    if not result.failures:
        return False, []

    classifications = []
    all_known = True
    for failure in result.failures:
        match = library.classify(failure.step, failure.error)
        classifications.append(
            {
                "step": failure.step,
                "pattern_id": match.pattern.id if match else None,
            }
        )
        if match is None:
            all_known = False
    return all_known, classifications


def _resolve_platform_environment(
    config: AitlcConfig, feature_path: Path, extra_env: dict[str, str]
) -> str:
    """Resolve PLATFORM_ENVIRONMENT by running the configured command.

    Run the configured platform_environment_command, return its last
    stdout line as the PLATFORM_ENVIRONMENT value. Raises RuntimeError with
    the command's stderr on failure — a bad/misconfigured command should
    fail loudly here, not silently fall through to a crash deep inside
    behave's own hook loading.

    extra_env (TESTING_PLATFORM/TEST_TYPE/DEVICE_NAME/LT_TUNNEL_NAME) MUST
    be passed into this subprocess, not just the later behave invocation —
    real bug found live: this project's own capability builder
    (config.lambdatest_config.LambdaTestConfig) branches on those vars, so
    running this subprocess with only the parent's inherited os.environ
    (which doesn't have them yet at this point) silently built the WRONG
    capabilities — LT rejected the resulting session with "capability
    platform has value android which is not supported" (a desktop-OS
    capability list), not the real mobile-device error. The
    manual workflow never hit this because its shell `export`s happen
    before resolve_lt_environment() runs, so its subprocess inherited them
    for free.
    """
    template = config.lambdatest.platform_environment_command
    assert template is not None
    poetry_cmd = " ".join(behave_runner.resolve_poetry())
    command = template.format(
        feature_name=feature_path.stem,
        platform_name="android",
        device_name="",
        poetry=poetry_cmd,
    )
    proc = subprocess.run(
        command,
        shell=True,
        cwd=config.root_dir,
        capture_output=True,
        text=True,
        env={**os.environ, **extra_env},
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"platform_environment_command failed (exit {proc.returncode}): {proc.stderr}"
        )
    lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("platform_environment_command produced no stdout output")
    return lines[-1]


def run(
    test_id: str = typer.Argument(..., help="Test ID or feature file path."),
    tags: str | None = typer.Option(None, help="Run only scenarios with this tag."),
    name: str | None = typer.Option(
        None, help="Run only scenarios matching this name."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Check steps resolve without running."
    ),
    no_capture: bool = typer.Option(False, "--no-capture", help="Verbose output."),
    toon_output: bool = typer.Option(
        False, "--toon", help="Emit the failures table as TOON instead of JSON."
    ),
    no_lock: bool = typer.Option(
        False, "--no-lock", help="Skip the per-test-ID concurrency lock (FR-1.6)."
    ),
    no_status: bool = typer.Option(
        False,
        "--no-status",
        help="Skip writing the live current_step.json status file (FR-1.5).",
    ),
    env_file: str = typer.Option(
        ".env",
        "--env-file",
        help="Load additional env vars from this file before running.",
    ),
    retry: int = typer.Option(
        0, "--retry", help="Retry up to N additional times on failure."
    ),
    retry_only_if_known_flake: bool = typer.Option(
        False,
        "--retry-only-if-known-flake",
        help=(
            "Only consume a retry when every failure matches patterns.yaml "
            "(FR-1.7). Stops immediately, without retrying, on any "
            "unmatched failure."
        ),
    ),
    patterns_file: Path | None = typer.Option(
        None,
        "--patterns",
        help="Path to patterns.yaml (default: <config root>/patterns.yaml).",
    ),
    mobile: bool = typer.Option(
        False,
        "--mobile",
        help=(
            "Run locally with mobile emulation: sets TEST_TYPE=mobile_browser "
            "and the device variable from [mobile] in aitlc.toml. Without it a "
            "mobile scenario runs at desktop viewport and nothing says so."
        ),
    ),
    remote: bool = typer.Option(
        False,
        "--remote",
        help=(
            "Run on a real LambdaTest device — sets TESTING_PLATFORM=LAMBDATEST, "
            "TEST_TYPE=mobile_browser, and LT_TUNNEL_NAME from aitlc.toml, and "
            "coordinates against the account's concurrency ceiling (FR-8)."
        ),
    ),
    queue: bool = typer.Option(
        False,
        "--queue",
        help=(
            "With --remote: wait for a free concurrency slot instead of "
            "failing immediately when the ceiling is hit."
        ),
    ),
    queue_timeout: float = typer.Option(
        1800.0, "--queue-timeout", help="Max seconds to wait when --queue is set."
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        help=(
            "Reuse (or start) the persistent CDP debug Chrome and freeze the "
            "browser on failure for live inspection. Sets PLAYWRIGHT_CDP_URL "
            "and the suite's own pause flag together — such hooks need BOTH, and "
            "setting only one silently tears the failed page down."
        ),
    ),
    cdp_port: int = typer.Option(
        chrome_cdp.DEFAULT_PORT, "--cdp-port", help="Port for --debug's Chrome."
    ),
) -> None:
    """Run one feature file and report structured results."""
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)

    # Split behave's `FILE:LINE` BEFORE resolution — resolve_feature_path()
    # runs the id through Path(...).stem, which silently swallows a
    # trailing ":30" and returns the bare feature file. That made
    # `aitlc run PROJ-24026:30` run the whole file while reporting nothing
    # unusual (verified live).
    base_test_id, line = split_line_spec(test_id)
    feature_path = config.resolve_feature_path(base_test_id)
    if feature_path is None:
        typer.echo(
            json.dumps({"error": f"Could not resolve feature for '{base_test_id}'"}),
            err=True,
        )
        raise typer.Exit(code=2)

    secret_values = [
        v
        for generic_name in (
            "lt_access_key",
            "jira_token",
            "jira_xray_client_secret",
            "s3_secret_access_key",
            "s3_session_token",
        )
        if (v := config.env.resolve(generic_name))
    ]

    library: PatternLibrary | None = None
    if retry_only_if_known_flake:
        resolved_patterns_path = patterns_file or (config.root_dir / "patterns.yaml")
        if not resolved_patterns_path.exists():
            typer.echo(
                json.dumps(
                    {
                        "error": (
                            f"--retry-only-if-known-flake needs patterns.yaml "
                            f"at {resolved_patterns_path}"
                        )
                    }
                ),
                err=True,
            )
            raise typer.Exit(code=2)
        library = PatternLibrary.load(resolved_patterns_path)

    remote_env: dict[str, str] = {}
    debug_extra_args: list[str] = []
    if remote:
        if not config.lambdatest.tunnel_name:
            typer.echo(
                json.dumps(
                    {
                        "error": "--remote needs [lambdatest].tunnel_name set in aitlc.toml"
                    }
                ),
                err=True,
            )
            raise typer.Exit(code=2)
        remote_env = {
            "TESTING_PLATFORM": "LAMBDATEST",
            "TEST_TYPE": "mobile_browser",
            "LT_TUNNEL_NAME": config.lambdatest.tunnel_name,
            # Real project requirement found live building this: LAMBDATEST
            # capability building crashes (KeyError) without this also set
            # — TESTING_PLATFORM=LAMBDATEST alone is not sufficient.
            config.mobile.mobile_device_env_var: config.mobile.mobile_device_env_value,
        }
        if config.lambdatest.platform_environment_command:
            try:
                remote_env["PLATFORM_ENVIRONMENT"] = _resolve_platform_environment(
                    config, feature_path, remote_env
                )
            except RuntimeError as exc:
                typer.echo(json.dumps({"error": str(exc)}), err=True)
                raise typer.Exit(code=2) from exc

    local_mobile_env: dict[str, str] = {}
    if mobile:
        if remote:
            typer.echo(
                json.dumps(
                    {"error": "--mobile is for local runs; --remote already implies it"}
                ),
                err=True,
            )
            raise typer.Exit(code=2)
        local_mobile_env = {
            "TEST_TYPE": "mobile_browser",
            config.mobile.mobile_device_env_var: config.mobile.mobile_device_env_value,
        }

    if debug:
        dirty, why = chrome_cdp.is_dirty_for(config.root_dir, cdp_port, base_test_id)
        if dirty:
            typer.echo(json.dumps({"warning": why}), err=True)

        if remote:
            typer.echo(
                json.dumps(
                    {
                        "error": "--debug attaches to a local Chrome; it cannot be combined with --remote"
                    }
                ),
                err=True,
            )
            raise typer.Exit(code=2)
        try:
            instance, _reused = chrome_cdp.launch(config.root_dir, port=cdp_port)
        except chrome_cdp.ChromeCdpError as exc:
            typer.echo(json.dumps({"error": str(exc)}), err=True)
            raise typer.Exit(code=2) from exc
        remote_env["PLAYWRIGHT_CDP_URL"] = instance.cdp_url
        # Claim the profile, so the next run against it can say who was here.
        chrome_cdp.mark_driven(config.root_dir, instance.port, base_test_id)

        # Pause-on-failure is attached through behave's own runner option
        # rather than a hook block the project has to carry. Projects that
        # still have such a block keep working — this sets a different
        # variable, so the two cannot both fire.
        attach_work_dir = Path(tempfile.mkdtemp(prefix="aitlc_attach_"))
        attach_plan = attach.plan(
            [*behave_runner.resolve_poetry(), "run", "behave"],
            config.root_dir,
            attach_work_dir,
            aitlc_src=Path(attach.__file__).resolve().parent.parent.parent,
            pause_on_failure=True,
        )
        remote_env.update(attach_plan.env)
        debug_extra_args = attach_plan.extra_args
        typer.echo(
            json.dumps(
                {"instrumentation": attach_plan.mechanism, "detail": attach_plan.detail}
            ),
            err=True,
        )

    safe_test_id = test_id.replace("/", "_").replace(":", "_")
    status_path = (
        None
        if no_status
        else config.root_dir / "reports" / ".status" / f"{safe_test_id}.json"
    )

    def _run_once() -> behave_runner.RunResult:
        return behave_runner.run(
            feature_path,
            cwd=config.root_dir,
            tags=tags,
            name_pattern=name,
            dry_run=dry_run,
            no_capture=no_capture,
            env={**remote_env, **local_mobile_env} or None,
            status_file=status_path,
            line=line,
            extra_args=debug_extra_args,
        )

    def _run_with_retries() -> tuple[behave_runner.RunResult, dict]:
        attempts = 0
        retry_info: dict = {"attempts": 1, "retried": False, "stopped_reason": None}
        result = _run_once()
        attempts = 1

        while not result.passed and attempts <= retry:
            if retry_only_if_known_flake:
                assert library is not None
                all_known, classifications = _all_failures_are_known_flakes(
                    result, library
                )
                if not all_known:
                    retry_info["stopped_reason"] = (
                        "unmatched failure — does not match any known pattern, "
                        "not retrying"
                    )
                    retry_info["classifications"] = classifications
                    break
                retry_info["classifications"] = classifications
            result = _run_once()
            attempts += 1
            retry_info["attempts"] = attempts
            retry_info["retried"] = True

        return result, retry_info

    def _run_with_local_lock() -> tuple[behave_runner.RunResult, dict]:
        if no_lock:
            return _run_with_retries()
        lock_dir = config.root_dir / "reports" / ".locks"
        with locks.held(test_id, lock_dir):
            return _run_with_retries()

    try:
        if remote:
            slot_dir = config.root_dir / "reports" / ".remote_slots"
            pool = remote_queue.RemoteSlotPool(
                max_concurrent=config.lambdatest.max_concurrent_sessions,
                slot_dir=slot_dir,
            )
            with remote_queue.held(
                pool, wait_timeout_s=queue_timeout if queue else None
            ):
                result, retry_info = _run_with_local_lock()
        else:
            result, retry_info = _run_with_local_lock()
    except locks.LockHeldError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=3) from exc
    except remote_queue.NoSlotAvailableError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=4) from exc

    # Recorded for every run so flake rate comes from observed outcomes
    # rather than only from hand-written signatures in patterns.yaml.
    history_core.record(
        config.root_dir,
        test_id=base_test_id,
        passed=result.passed,
        failed_step=result.failures[0].step if result.failures else None,
    )

    payload = result.to_dict()
    if retry > 0:
        payload["retry"] = retry_info

    if toon_output and payload["failures"]:
        output = toon.encode_table(payload["failures"], name="failures")
        output += f"\n\nsteps_by_status: {json.dumps(payload['steps_by_status'])}"
        if retry > 0:
            output += f"\nretry: {json.dumps(retry_info)}"
    else:
        output = json.dumps(payload)

    output = redact_text(output, secret_values)
    typer.echo(output)

    # Journalled because this is the most expensive command in the tool: a run
    # takes minutes and, on suites that create users or move balances, changes
    # real state. Re-running it to re-read an answer is the cost this avoids.
    journal.record(
        config.root_dir,
        command="run",
        argv=[base_test_id],
        exit_code=0 if result.passed else 1,
        payload=payload,
        secret_values=secret_values,
        tags=["run", base_test_id],
    )

    raise typer.Exit(code=0 if result.passed else 1)
