"""`aitlc init` — detect a project's layout and write aitlc.toml."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from aitlc.core import init_config

app = typer.Typer(help="Set up aitlc in a project.")


@app.command("init")
def init(
    root: Path | None = typer.Option(
        None, "--root", help="Project root to inspect (default: current directory)."
    ),
    env_file: str = typer.Option(
        ".env", "--env-file", help="Env file to read variable NAMES from."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be written, write nothing."
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing aitlc.toml."
    ),
    merge: bool = typer.Option(
        False,
        "--merge",
        help="Add newly detected keys to an existing aitlc.toml, keeping every "
        "value already set there.",
    ),
) -> None:
    """Detect this project's layout and write a working aitlc.toml.

    Everything in that file is discoverable from the repo: feature and step
    directories are where the `.feature` files and step decorators actually
    are, the issue-key prefix shows up in feature filenames, and the
    per-scenario setup hook is a call inside the project's own
    `before_scenario`.

    Each detection reports how it was reached and how certain it is, and
    anything undetected is written as a commented placeholder rather than
    guessed — a wrong value written confidently fails later and somewhere
    else, which is worse than an obviously missing one.

    Only variable NAMES are read from the env file. No secret is read,
    stored or printed.
    """
    project_root = (root or Path.cwd()).resolve()
    if not project_root.is_dir():
        typer.echo(json.dumps({"error": f"not a directory: {project_root}"}), err=True)
        raise typer.Exit(code=2)

    target = project_root / "aitlc.toml"
    if merge and force:
        typer.echo(
            json.dumps({"error": "--merge and --force are mutually exclusive"}),
            err=True,
        )
        raise typer.Exit(code=2)
    if target.exists() and not force and not merge and not dry_run:
        typer.echo(
            json.dumps(
                {
                    "error": f"{target} already exists",
                    "hint": (
                        "re-run with --merge to add only newly detected keys and "
                        "keep your edits, --force to overwrite, or --dry-run to "
                        "preview"
                    ),
                }
            ),
            err=True,
        )
        raise typer.Exit(code=2)

    profile = init_config.profile_project(project_root, env_file=env_file)
    content = init_config.render_toml(profile)

    payload = profile.to_dict()
    payload["target"] = str(target)
    payload["written"] = False

    added: list[str] = []
    if merge and target.exists():
        content, added = init_config.merge_toml(
            target.read_text(encoding="utf-8"), content
        )
        payload["merged"] = True
        payload["added_keys"] = added

    if dry_run:
        payload["preview"] = content
        typer.echo(json.dumps(payload, indent=2))
        return

    if merge and target.exists() and not added:
        payload["written"] = False
        payload["message"] = "nothing to add; every detected key is already set"
        typer.echo(json.dumps(payload, indent=2))
        return

    target.write_text(content, encoding="utf-8")
    payload["written"] = True
    typer.echo(json.dumps(payload, indent=2))

    if profile.unresolved:
        typer.echo(
            json.dumps(
                {
                    "note": "some settings could not be detected",
                    "unresolved": profile.unresolved,
                    "hint": "they are commented out in the file; fill in if you need them",
                }
            ),
            err=True,
        )
