"""`aitlc behave ...` and `aitlc pw ...` — escape hatches to the real tools.

aitlc's own commands are opinionated: they cover the paths worth making
easy. These two cover everything else, so adopting aitlc never means losing
access to a flag it does not wrap.

What they add over typing `behave` or `playwright` directly is the
environment those tools need in order to work at all in a real project:

* the project's `.env` loaded first — a missing var here is the difference
  between a run and a `KeyError` several frames into a hook
* the correct interpreter (`poetry run`, etc.) and working directory
* for behave, aitlc's optional instrumentation, attached through behave's
  own runner option with no change to the project

`--print-command` prints the exact invocation instead of running it, so the
translation from `aitlc behave X` to the real command is never a mystery —
useful when handing a repro to someone who does not have aitlc, and when an
agent needs to explain what it is about to do.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import typer
from aitlc.config import AitlcConfig
from aitlc.core import behave_runner, chrome_cdp
from aitlc.core.dotenv import load_dotenv
from aitlc.runtime import attach

app = typer.Typer(
    help="Run behave or playwright directly, with the project's environment.",
    # Unknown flags belong to the wrapped tool, not to aitlc.
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)


def _aitlc_src_dir() -> Path:
    """Directory to put on PYTHONPATH so the target can import aitlc."""
    # .../aitlc/runtime/attach.py -> .../src
    return Path(attach.__file__).resolve().parent.parent.parent


def _run(cmd: list[str], cwd: Path, env: dict[str, str], print_only: bool) -> None:
    """Print or execute a prepared command, then exit with its status."""
    if print_only:
        typer.echo(
            json.dumps(
                {"command": cmd, "cwd": str(cwd), "env_overrides": env}, indent=2
            )
        )
        raise typer.Exit(code=0)

    proc = subprocess.run(cmd, cwd=cwd, env={**os.environ, **env})
    raise typer.Exit(code=proc.returncode)


def _cdp_overrides(
    config: AitlcConfig, *, enable: bool, explicit_url: str | None
) -> tuple[dict[str, str], dict | None]:
    """Env that attaches the suite to a live CDP browser, plus a note to log.

    An explicit URL wins; otherwise, unless disabled, a running aitlc debug
    Chrome (`aitlc cdp launch`) is reused so the suite attaches instead of
    launching a fresh browser. A value already in the environment is left
    untouched -- the caller asked for that one on purpose. Returns ({}, None)
    when there is nothing to attach to, so a plain passthrough is unchanged.
    """
    env_name = config.playwright_cdp_env
    if os.environ.get(env_name):
        return {}, None
    url = explicit_url
    if url is None and enable:
        url = chrome_cdp.resolve_live_cdp_url(config.root_dir)
    if not url:
        return {}, None
    return {env_name: url}, {"cdp_attach": url, "via": env_name}


@app.command(
    "behave",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def behave_passthrough(
    ctx: typer.Context,
    env_file: str = typer.Option(".env", "--aitlc-env-file", help="Env file to load."),
    debug: bool = typer.Option(
        False,
        "--aitlc-debug",
        help="Halt at the first failed step, before teardown, leaving the browser open.",
    ),
    events: Path | None = typer.Option(
        None, "--aitlc-events", help="Append a JSON-lines event per step to this file."
    ),
    cdp: bool = typer.Option(
        True,
        "--aitlc-cdp/--aitlc-no-cdp",
        help=(
            "Attach to a live CDP debug browser if one is running "
            "(`aitlc cdp launch`), so behave reuses that Chrome instead of "
            "launching a fresh one. On by default; a no-op when nothing runs."
        ),
    ),
    cdp_url: str | None = typer.Option(
        None, "--aitlc-cdp-url", help="Explicit CDP URL to attach to (overrides auto)."
    ),
    print_command: bool = typer.Option(
        False, "--print-command", help="Show the command instead of running it."
    ),
) -> None:
    """Run behave with the project's environment and aitlc instrumentation.

    Every argument aitlc does not recognise is forwarded to behave
    untouched, so all of behave's own options keep working. aitlc's own
    options are prefixed `--aitlc-` precisely so they cannot collide with
    a behave flag now or in a future behave release.
    """
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)

    base = [*behave_runner.resolve_poetry(), "run", "behave"]
    work_dir = Path(tempfile.mkdtemp(prefix="aitlc_attach_"))
    attach_plan = attach.plan(
        base,
        config.root_dir,
        work_dir,
        aitlc_src=_aitlc_src_dir(),
        pause_on_failure=debug,
        events_path=events,
    )

    if attach_plan.mechanism != "none":
        typer.echo(
            json.dumps(
                {
                    "instrumentation": attach_plan.mechanism,
                    "detail": attach_plan.detail,
                }
            ),
            err=True,
        )

    cdp_over, cdp_note = _cdp_overrides(config, enable=cdp, explicit_url=cdp_url)
    if cdp_note is not None:
        typer.echo(json.dumps(cdp_note), err=True)

    cmd = [*base, *attach_plan.extra_args, *ctx.args]
    _run(cmd, config.root_dir, {**attach_plan.env, **cdp_over}, print_command)


@app.command(
    "pw", context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def playwright_passthrough(
    ctx: typer.Context,
    env_file: str = typer.Option(".env", "--aitlc-env-file", help="Env file to load."),
    print_command: bool = typer.Option(
        False, "--print-command", help="Show the command instead of running it."
    ),
) -> None:
    """Run the Playwright CLI inside the project's environment.

    `aitlc pw show-trace trace.zip`, `aitlc pw codegen <url>`,
    `aitlc pw install chromium` — the browsers and versions resolved are
    the project's own, which is the point: a globally installed playwright
    can differ from the one the suite actually runs against, and debugging
    against the wrong build wastes real time.
    """
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)

    if not ctx.args:
        typer.echo(
            json.dumps(
                {
                    "error": "no playwright arguments given",
                    "examples": [
                        "aitlc pw show-trace trace.zip",
                        "aitlc pw codegen https://example.com",
                        "aitlc pw install chromium",
                    ],
                }
            ),
            err=True,
        )
        raise typer.Exit(code=2)

    if shutil.which("poetry") or (config.root_dir / "pyproject.toml").exists():
        cmd = [*behave_runner.resolve_poetry(), "run", "playwright", *ctx.args]
    else:
        cmd = ["playwright", *ctx.args]

    _run(cmd, config.root_dir, {}, print_command)


@app.command(
    "paver", context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def paver_passthrough(
    ctx: typer.Context,
    env_file: str = typer.Option(".env", "--aitlc-env-file", help="Env file to load."),
    cdp: bool = typer.Option(
        True,
        "--aitlc-cdp/--aitlc-no-cdp",
        help=(
            "Attach the suite to a live CDP debug browser if one is running "
            "(`aitlc cdp launch`), so it reuses that Chrome instead of launching "
            "a fresh one. On by default and a no-op when nothing is running."
        ),
    ),
    cdp_url: str | None = typer.Option(
        None, "--aitlc-cdp-url", help="Explicit CDP URL to attach to (overrides auto)."
    ),
    print_command: bool = typer.Option(
        False, "--print-command", help="Show the command instead of running it."
    ),
) -> None:
    """Run the project's `paver` tasks inside the project's environment.

    `aitlc paver run parallel --local`, `aitlc paver build_hybrid_yaml` — the
    project's own paver tasks, with `.env` loaded and the project interpreter
    (`poetry run`) resolved, so they run the same way they do for the team.
    Every argument is forwarded to paver untouched. Unless `--aitlc-no-cdp`, a
    live debug Chrome is reused via the project's CDP env var, so a local sweep
    attaches to the open browser instead of launching a fresh one per feature.
    """
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)

    if not ctx.args:
        typer.echo(
            json.dumps(
                {
                    "error": "no paver arguments given",
                    "examples": [
                        "aitlc paver run parallel --local",
                        "aitlc paver build_hybrid_yaml",
                        "aitlc paver <task>",
                    ],
                }
            ),
            err=True,
        )
        raise typer.Exit(code=2)

    overrides, note = _cdp_overrides(config, enable=cdp, explicit_url=cdp_url)
    if note is not None:
        typer.echo(json.dumps(note), err=True)

    cmd = [*behave_runner.resolve_poetry(), "run", "paver", *ctx.args]
    _run(cmd, config.root_dir, overrides, print_command)


# Mounted by commands/_registry.py. Empty name mounts these at the root as the
# escape hatch (`aitlc behave ...`, `aitlc pw ...`). Ordered last.
COMMAND = {"name": "", "attr": "app", "kind": "group", "order": 1000}