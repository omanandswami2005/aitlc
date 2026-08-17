"""`aitlc journal ...` — read back what earlier commands did."""

from __future__ import annotations

import json

import typer
from aitlc.config import AitlcConfig
from aitlc.core import artifact_cache, journal

app = typer.Typer(help="Inspect the record of earlier command runs.")


@app.command("list")
def list_entries(
    last: int = typer.Option(10, "--last", help="How many entries to show."),
    command: str | None = typer.Option(
        None, "--command", help="Only entries whose command contains this."
    ),
) -> None:
    """List recent runs, newest first."""
    config = AitlcConfig.find_and_load()
    found = journal.entries(config.root_dir, limit=None)
    if command:
        found = [e for e in found if command in e.command]
    found = found[:last]
    typer.echo(
        json.dumps(
            {
                "count": len(found),
                "entries": [
                    {
                        "id": e.entry_id,
                        "command": e.command,
                        "exit_code": e.exit_code,
                        "duration_s": e.duration_s,
                        "truncated": e.truncated,
                    }
                    for e in found
                ],
            },
            indent=2,
        )
    )


@app.command("show")
def show(
    entry_id: str = typer.Argument(..., help="Entry id, or a prefix of one.")
) -> None:
    """Print one entry, including the payload it recorded."""
    config = AitlcConfig.find_and_load()
    entry = journal.read(config.root_dir, entry_id)
    if entry is None:
        typer.echo(
            json.dumps({"error": f"no journal entry matching {entry_id!r}"}), err=True
        )
        raise typer.Exit(code=2)
    typer.echo(json.dumps(entry.__dict__, indent=2, default=str))


@app.command("diff")
def diff(
    left: str = typer.Argument(..., help="Earlier entry id (or prefix)."),
    right: str = typer.Argument(..., help="Later entry id (or prefix)."),
) -> None:
    """Compare two runs — the 'did my fix work, or was that luck' question."""
    config = AitlcConfig.find_and_load()
    a = journal.read(config.root_dir, left)
    b = journal.read(config.root_dir, right)
    missing = [x for x, e in ((left, a), (right, b)) if e is None]
    if missing:
        typer.echo(
            json.dumps({"error": f"no journal entry matching {missing}"}), err=True
        )
        raise typer.Exit(code=2)
    typer.echo(json.dumps(journal.diff(a, b), indent=2))


@app.command("cache")
def cache(
    clear_: bool = typer.Option(False, "--clear", help="Empty the artifact cache."),
) -> None:
    """Report, or empty, the fetched-artifact cache."""
    config = AitlcConfig.find_and_load()
    if clear_:
        removed = artifact_cache.clear(config.root_dir)
        typer.echo(json.dumps({"cleared": removed}))
        return
    typer.echo(json.dumps(artifact_cache.stats(config.root_dir), indent=2))
