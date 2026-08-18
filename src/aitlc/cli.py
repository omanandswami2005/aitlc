"""aitlc — top-level CLI app. `aitlc --help` lists all subcommands."""

from __future__ import annotations

import json

import typer
from aitlc.commands import (
    cdp_cmd,
    classify_cmd,
    debug_cmd,
)
from aitlc.commands import doctor as doctor_module
from aitlc.commands import (
    history_cmd,
    journal_cmd,
    locators_cmd,
    init_cmd,
    jira_cmd,
    notify_cmd,
    parallel_cmd,
    passthrough_cmd,
    propose_fix_cmd,
    record_cmd,
    report_cmd,
)
from aitlc.commands import run as run_module
from aitlc.commands import (
    s3_cmd,
    start_cmd,
    steps_cmd,
    trace_cmd,
    tunnel_cmd,
    users_cmd,
    xray_cmd,
)

app = typer.Typer(
    name="aitlc",
    help="Structured, JSON/TOON-first CLI for Behave+Playwright debugging and Xray verification.",
    no_args_is_help=True,
)


def _version() -> str:
    """The installed version, read from package metadata rather than a constant.

    A hardcoded string is a second place to forget to bump, and it silently
    disagrees with what is actually installed -- which is exactly the question
    `--version` is asked to settle when a fix "did not take".
    """
    try:
        from importlib.metadata import version

        return version("aitlc")
    except Exception:  # pragma: no cover - only when running from a bare tree
        return "unknown"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(json.dumps({"aitlc": _version()}))
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Print the installed version and exit.",
    ),
) -> None:
    """Structured CLI for Behave + Playwright debugging."""

# `run` and `doctor` are single direct commands (`aitlc run PROJ-123`, not
# `aitlc run run PROJ-123`) — registered as plain commands, not sub-Typers.
app.command("run")(run_module.run)
app.command("doctor")(doctor_module.doctor)
app.command("init")(init_cmd.init)
app.command("classify-failure")(classify_cmd.classify_failure)
app.command("report")(report_cmd.report)
app.command("record")(record_cmd.record)
app.command("start")(start_cmd.start)
app.command("propose-fix")(propose_fix_cmd.propose_fix)
app.command("notify-teams")(notify_cmd.notify_teams)

# `xray` and `cdp` genuinely host multiple subcommands
# (`aitlc xray get-gherkin ...`, `aitlc cdp inspect ...`) — kept as sub-Typers.
app.add_typer(xray_cmd.app, name="xray")
app.add_typer(cdp_cmd.app, name="cdp")
app.add_typer(debug_cmd.app, name="debug")
app.add_typer(tunnel_cmd.app, name="tunnel")
app.add_typer(jira_cmd.app, name="jira")
app.add_typer(trace_cmd.app, name="trace")
app.add_typer(steps_cmd.app, name="steps")
app.add_typer(s3_cmd.app, name="s3")
app.add_typer(parallel_cmd.app, name="parallel")
app.add_typer(users_cmd.app, name="users")
app.add_typer(history_cmd.app, name="history")
app.add_typer(journal_cmd.app, name="journal")
app.add_typer(locators_cmd.app, name="locators")

# Escape hatches: everything aitlc does not wrap is still reachable,
# with the project environment already set up.
app.add_typer(passthrough_cmd.app, name="", hidden=False)


if __name__ == "__main__":
    app()
