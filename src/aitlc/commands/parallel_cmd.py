"""`aitlc parallel` — a drop-in wrapper for `paver run parallel --local`.

The workflow this replaces: `pavement.py`'s `get_local_features()` is
`glob.glob("features/*.feature")`, so `paver run parallel --local` runs
*every* top-level feature. Narrowing a run therefore meant physically
adding `@skip_xray_test` to every other feature file and reverting it
afterwards — edits that are easy to forget and that show up in `git status`
as unrelated noise.

This keeps that command's shape and semantics:

* No feature arguments  -> discover features and run them all, honoring
  the suite's own skip tag exactly as its runner does, so files already
  tagged in the repo keep skipping.
* Feature arguments     -> run only those, no tag edits anywhere.

and adds what the paver path could not do: `--jobs`, per-feature structured
results, `FILE:LINE` targeting, and `--list` to preview the selection.

Two deliberate differences from paver, both fixing real gaps:

* Discovery is **recursive** by default. `glob.glob("features/*.feature")`
  is not, so nested suites (`features/PROJ-29018/*.feature`) are invisible
  to `paver run parallel --local` today. `--no-recursive` restores exact
  paver parity.
* Skipped files are **reported**, not silently dropped, so "skipped by tag"
  never looks the same as "never discovered".
"""

from __future__ import annotations

import json
import queue
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import typer
from aitlc.config import AitlcConfig
from aitlc.core import behave_runner, chrome_cdp
from aitlc.core import focus as focus_state
from aitlc.core.dotenv import load_dotenv
from aitlc.core.feature_select import (
    DEFAULT_SKIP_TAG,
    discover_features,
    select_features,
    split_line_spec,
)
from aitlc.core.redact import redact_text
from aitlc.core import interference, workspace

app = typer.Typer(
    help="Run many features in parallel — the `paver run parallel` workflow without tag edits."
)


def _display(path: Path, root: Path) -> str:
    """Repo-relative path when possible, absolute otherwise.

    `Path.relative_to` raises for anything outside the root (e.g. a
    `--dir` pointing elsewhere), and a crash while merely *formatting*
    results would throw away a completed run's output.
    """
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _resolve_requested(
    config: AitlcConfig, requested: list[str]
) -> tuple[list[tuple[Path, int | None]], list[str]]:
    """Resolve explicit ids/paths, keeping any `:LINE` suffix intact."""
    resolved: list[tuple[Path, int | None]] = []
    unresolved: list[str] = []
    for item in requested:
        base, line = split_line_spec(item)
        path = config.resolve_feature_path(base)
        if path is None:
            unresolved.append(item)
        else:
            resolved.append((path, line))
    return resolved, unresolved


@app.command("focus")
def focus(
    features: list[str] | None = typer.Argument(
        None,
        help="Test IDs / feature paths (optionally FILE:LINE). Omit to show current focus.",
    ),
    clear: bool = typer.Option(False, "--clear", help="Clear the saved focus."),
) -> None:
    """Pin what `aitlc parallel run` runs, so you stop typing filenames.

    Replaces the "tag every other file with @skip_xray_test, then revert"
    habit — same "just run the bare command" ergonomics, but nothing in the
    repo is modified, so nothing can be committed by accident.
    """
    config = AitlcConfig.find_and_load()

    if clear:
        cleared = focus_state.clear(config.root_dir)
        typer.echo(json.dumps({"cleared": cleared}))
        return

    if not features:
        saved = focus_state.load(config.root_dir)
        typer.echo(
            json.dumps(
                {
                    "focused": list(saved.features) if saved else [],
                    "active": saved is not None,
                },
                indent=2,
            )
        )
        return

    resolved, unresolved = _resolve_requested(config, list(features))
    if unresolved:
        typer.echo(
            json.dumps({"error": "could not resolve", "features": unresolved}), err=True
        )
        raise typer.Exit(code=2)

    # Store what the user typed (id + any :LINE), not the resolved absolute
    # path: re-resolving on each run keeps focus valid if a file moves.
    stored = list(features)
    saved = focus_state.save(config.root_dir, stored)
    typer.echo(
        json.dumps(
            {
                "focused": list(saved.features),
                "resolved": [_display(p, config.root_dir) for p, _ in resolved],
            },
            indent=2,
        )
    )


@app.command("run")
def run(
    features: list[str] | None = typer.Argument(
        None,
        help=(
            "Test IDs / feature paths (optionally FILE:LINE). Omit to run "
            "everything discovered, like `paver run parallel --local`."
        ),
    ),
    jobs: int = typer.Option(
        4, "--jobs", "-j", help="How many features to run concurrently."
    ),
    dir_: Path | None = typer.Option(
        None,
        "--dir",
        help="Discover features under this directory instead of the configured root.",
    ),
    skip_tag: str = typer.Option(
        DEFAULT_SKIP_TAG,
        "--skip-tag",
        help="Feature-level tag that excludes a file (matches skip_checks.py).",
    ),
    verify_failures: bool = typer.Option(
        False,
        "--verify-failures",
        help=(
            "Re-run each failure once, serially, after the pool drains, and "
            "report serial_recheck. A parallel failure can be a concurrency "
            "artifact rather than a defect."
        ),
    ),
    no_skip_tag: bool = typer.Option(
        False,
        "--no-skip-tag",
        help="Ignore the skip tag and run everything discovered.",
    ),
    recursive: bool = typer.Option(
        True,
        "--recursive/--no-recursive",
        help="Recurse into subdirectories. --no-recursive matches paver's non-recursive glob.",
    ),
    tags: str | None = typer.Option(None, "--tags", help="Passed through to behave."),
    list_only: bool = typer.Option(
        False,
        "--list",
        help="Show what would run (and what is skipped, and why) without running.",
    ),
    no_capture: bool = typer.Option(
        False, "--no-capture", help="Verbose behave output."
    ),
    env_file: str = typer.Option(".env", "--env-file", help="Env file to load first."),
    debug: bool = typer.Option(
        False,
        "--debug",
        help="Attach every run to the persistent CDP Chrome and freeze on failure.",
    ),
    cdp_port: int = typer.Option(chrome_cdp.DEFAULT_PORT, "--cdp-port"),
    isolated: bool = typer.Option(
        False,
        "--isolated",
        help=(
            "With --debug: give each concurrent job its own fresh browser "
            "instead of sharing one. Keeps --jobs parallelism (the shared "
            "browser forces --jobs 1)."
        ),
    ),
) -> None:
    """Run several features concurrently."""
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)

    # No features typed? Fall back to a saved focus, so the bare command
    # keeps working the way `paver run parallel --local` did.
    used_focus = False
    if not features:
        saved = focus_state.load(config.root_dir)
        if saved is not None:
            features = list(saved.features)
            used_focus = True

    if features:
        resolved, unresolved = _resolve_requested(config, list(features))
        if unresolved:
            typer.echo(
                json.dumps({"error": "could not resolve", "features": unresolved}),
                err=True,
            )
            raise typer.Exit(code=2)
        # An explicitly named feature is an explicit intent — do not then
        # silently drop it for carrying the skip tag.
        selections = [(path, line, None) for path, line in resolved]
    else:
        # A relative --dir is relative to the project root, not to wherever
        # the command happened to be invoked from.
        if dir_ is not None:
            root = dir_ if dir_.is_absolute() else (config.root_dir / dir_)
        else:
            root = config.root_dir / config.feature_dir
        if not root.exists():
            typer.echo(json.dumps({"error": f"no such directory: {root}"}), err=True)
            raise typer.Exit(code=2)
        discovered = discover_features(root, recursive=recursive)
        annotated = select_features(
            discovered, skip_tag=None if no_skip_tag else skip_tag
        )
        selections = [(s.path, None, s.skipped_by) for s in annotated]

    to_run = [(p, line) for p, line, skipped in selections if skipped is None]
    skipped = [
        {"feature": _display(p, config.root_dir), "skipped_by": skipped}
        for p, _line, skipped in selections
        if skipped is not None
    ]

    if list_only:
        typer.echo(
            json.dumps(
                {
                    "source": (
                        "focus" if used_focus else ("args" if features else "discovery")
                    ),
                    "would_run": [_display(p, config.root_dir) for p, _ in to_run],
                    "skipped": skipped,
                    "jobs": jobs,
                },
                indent=2,
            )
        )
        return

    if not to_run:
        typer.echo(
            json.dumps({"error": "nothing to run", "skipped": skipped}), err=True
        )
        raise typer.Exit(code=2)

    base_env: dict[str, str] = {}
    per_job_cdp: list[str] = []
    if debug:
        if isolated:
            # One browser per concurrent job. Sharing a single Chrome across
            # concurrent scenarios interleaves their navigations, which
            # surfaces as failures that look like app bugs — so isolation is
            # what makes --debug and real parallelism compatible at all.
            try:
                for _ in range(max(1, jobs)):
                    instance, _reused = chrome_cdp.launch(config.root_dir, port=None)
                    per_job_cdp.append(instance.cdp_url)
            except chrome_cdp.ChromeCdpError as exc:
                typer.echo(json.dumps({"error": str(exc)}), err=True)
                raise typer.Exit(code=2) from exc
        else:
            try:
                instance, _ = chrome_cdp.launch(config.root_dir, port=cdp_port)
            except chrome_cdp.ChromeCdpError as exc:
                typer.echo(json.dumps({"error": str(exc)}), err=True)
                raise typer.Exit(code=2) from exc
            base_env.update(chrome_cdp.debug_env(instance.cdp_url))
            if jobs != 1:
                # A single shared browser cannot host concurrent scenarios
                # coherently; use --isolated to keep the parallelism.
                jobs = 1

    # Browsers are CHECKED OUT, not assigned by index. An earlier version
    # used `per_job_cdp[index % jobs]`, which is wrong: with more features
    # than jobs, ThreadPoolExecutor starts task N as soon as *any* worker
    # frees, so task `jobs` (-> index 0) could claim the browser task 0 was
    # still driving. Two concurrent runs then shared one Chrome, which is
    # exactly the interleaving --isolated exists to prevent. A queue makes
    # "in use" the thing that gates reuse, rather than an index that only
    # looks like it does.
    browser_pool: queue.Queue[str] = queue.Queue()
    for url in per_job_cdp:
        browser_pool.put(url)

    def _run_one(index_and_target: tuple[int, tuple[Path, int | None]]) -> dict:
        index, (path, line) = index_and_target
        # paver assigns a per-thread TASK_ID and the framework reads it;
        # mirror that so parallel behavior matches what people already run.
        env = {**base_env, "TASK_ID": str(index)}
        claimed: str | None = None
        if per_job_cdp:
            claimed = browser_pool.get()
            env.update(chrome_cdp.debug_env(claimed))
        log_path = workspace.output_path(
            config.root_dir, ".parallel", f"{path.stem}.log"
        )
        started_at = time.time()
        try:
            result = behave_runner.run(
                path,
                cwd=config.root_dir,
                tags=tags,
                no_capture=no_capture,
                env=env,
                line=line,
                # Was hardcoded to reports/, so a parallel run wrote its status
                # outside the workspace everything else honours.
                status_file=workspace.output_path(
                    config.root_dir, ".status", f"{path.stem}.json"
                ),
                log_file=log_path,
            )
        finally:
            # Return it even if the run raised, or one failure would
            # permanently shrink the pool and eventually deadlock the rest.
            if claimed is not None:
                browser_pool.put(claimed)
        payload = result.to_dict()
        payload["feature"] = _display(path, config.root_dir)
        payload["log"] = str(log_path.relative_to(config.root_dir))
        payload["started_at"] = started_at
        payload["ended_at"] = time.time()
        payload["task_id"] = index
        if line is not None:
            payload["line"] = line
        payload["passed"] = result.passed
        # "skipped" is its own outcome, not a quiet kind of pass. A feature the
        # project's own hooks skipped reports passed=true with no failures and
        # no passed steps, which at twenty features nobody reads closely enough
        # to notice.
        counts = payload.get("steps_by_status") or {}
        ran_anything = any(counts.get(k) for k in ("passed", "failed", "undefined"))
        payload["outcome"] = (
            "failed"
            if not result.passed
            else ("skipped" if not ran_anything else "passed")
        )
        return payload

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        results = list(pool.map(_run_one, enumerate(to_run)))

    failed = [r for r in results if not r["passed"]]

    # A parallel failure is not evidence on its own: of four failures in one
    # sweep of twenty-one features, three were concurrency artifacts or a
    # dropped network, and only one was a defect. Re-running each failure
    # serially, once, is the difference between a work list and a list of
    # things to go and check by hand.
    if verify_failures and failed:
        for row in failed:
            recheck = behave_runner.run(
                config.root_dir / row["feature"],
                cwd=config.root_dir,
                tags=tags,
                no_capture=no_capture,
                env=base_env,
            )
            row["serial_recheck"] = "passed" if recheck.passed else "failed"
        failed = [r for r in failed if r.get("serial_recheck") != "passed"]
    # Free attribution evidence, whether or not --verify-failures was used:
    # a run that never overlapped cannot have interfered, and two runs driving
    # the same account at the same time explain most "corrupted state"
    # failures in a suite that shares accounts across features.
    accounts_by_feature: dict[str, set[str]] = {}
    for row in results:
        feature_path = config.root_dir / row["feature"]
        try:
            accounts_by_feature[row["feature"]] = interference.accounts_in(
                feature_path.read_text(encoding="utf-8")
            )
        except OSError:
            accounts_by_feature[row["feature"]] = set()

    for row in failed:
        suspects = interference.suspects_for(row, results, accounts_by_feature)
        row["concurrent_with"] = [s.to_dict() for s in suspects[:5]]
        row["interference"] = interference.interference_note(suspects)

    summary = {
        "source": "focus" if used_focus else ("args" if features else "discovery"),
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "skipped_by_tag": skipped,
        "logs": str(
            workspace.output_path(config.root_dir, ".parallel").relative_to(
                config.root_dir
            )
        ),
        "results": results,
    }

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
    typer.echo(redact_text(json.dumps(summary, indent=2), secret_values))
    raise typer.Exit(code=0 if not failed else 1)
