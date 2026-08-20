"""aitlc — top-level CLI app. `aitlc --help` lists all subcommands.

This is a thin composition root: it owns the global options (`--version`,
`--workspace`) and then hands mounting to `commands/_registry.py`, which
discovers every command module that declares a `COMMAND`. Adding a command is
dropping one file under `commands/` with a `COMMAND` line — this file does not
change.
"""

from __future__ import annotations

import json

import typer

from aitlc.commands import _registry
from aitlc.core import workspace

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
    workspace_dir: str = typer.Option(
        "",
        "--workspace",
        "-w",
        help=(
            "Directory under the project root for every artifact this command "
            "produces. Point it at what you are investigating (e.g. -w PROJ-123) "
            "and traces, cached reports, session state and logs all land in one "
            "place. Also settable as AITLC_WORKSPACE or [project].workspace."
        ),
    ),
) -> None:
    """Structured CLI for Behave + Playwright debugging."""
    if workspace_dir:
        try:
            workspace.set_workspace(workspace_dir)
        except workspace.WorkspaceError as exc:
            typer.echo(json.dumps({"error": str(exc)}), err=True)
            raise typer.Exit(code=2) from exc


# Discover and mount every command module that declares a COMMAND. Order and
# grouping (plain command vs sub-Typer, including the root escape-hatch group
# `aitlc behave` / `aitlc pw`) are declared by each module, not wired here.
_registry.register_all(app)


if __name__ == "__main__":
    app()
