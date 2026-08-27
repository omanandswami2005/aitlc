"""`aitlc stepatlas ...` -- build/serve the StepAtlas site, and look up a step.

StepAtlas is a separate, sibling tool (its own repo, its own uv/pnpm
environment) that catalogs every real Gherkin step via the same
behave.step_registry walk aitlc's own `steps unused` uses. These commands
are a thin wrapper: they shell out to StepAtlas's own CLI/site tooling and
read its generated catalog.json, they don't reimplement any of it.
"""

from __future__ import annotations

import json
import re
import subprocess

import typer
from aitlc.config import AitlcConfig

app = typer.Typer(help="Build/serve the StepAtlas site, and look up a step.")

_MAX_MATCHES = 20


def _require_stepatlas_path(config):
    path = config.stepatlas_path()
    if path is None:
        typer.echo(
            json.dumps(
                {
                    "error": "no [stepatlas] path configured",
                    "hint": 'add [stepatlas]\\npath = "../StepAtlas" to aitlc.toml',
                }
            ),
            err=True,
        )
        raise typer.Exit(code=2)
    return path


@app.command("build")
def build() -> None:
    """Run `stepatlas build` against this project (no site preview)."""
    config = AitlcConfig.find_and_load()
    stepatlas_path = _require_stepatlas_path(config)
    proc = subprocess.run(
        [
            "uv", "run", "stepatlas", "build",
            "--project", str(config.root_dir),
            "--step-dir", config.step_dir,
            "--feature-dir", config.feature_dir,
            "--skip-site-build",
        ],
        cwd=stepatlas_path,
    )
    raise typer.Exit(code=proc.returncode)


@app.command("serve")
def serve(
    rebuild: bool = typer.Option(
        None,
        "--rebuild/--skip-build",
        help="Force a regenerate, or skip it and serve whatever's already built. "
        "Default: regenerate only if nothing's built yet (dist/ is empty/missing).",
    ),
) -> None:
    """Serve the StepAtlas site, regenerating first only if needed."""
    config = AitlcConfig.find_and_load()
    stepatlas_path = _require_stepatlas_path(config)
    already_built = (stepatlas_path / "site" / "dist" / "index.html").is_file()
    do_build = rebuild if rebuild is not None else not already_built
    if do_build:
        proc = subprocess.run(
            [
                "uv", "run", "stepatlas", "build",
                "--project", str(config.root_dir),
                "--step-dir", config.step_dir,
                "--feature-dir", config.feature_dir,
            ],
            cwd=stepatlas_path,
        )
        if proc.returncode != 0:
            raise typer.Exit(code=proc.returncode)
    proc = subprocess.run(["pnpm", "run", "preview"], cwd=stepatlas_path / "site")
    raise typer.Exit(code=proc.returncode)


@app.command("stop")
def stop() -> None:
    """Stop a running `stepatlas serve` preview server.

    `pnpm run preview` wraps astro's own server as a child process; a
    Ctrl+C sometimes only signals the pnpm wrapper (exit 143) and leaves
    astro.mjs holding the port. This finds and kills it directly.
    """
    config = AitlcConfig.find_and_load()
    stepatlas_path = _require_stepatlas_path(config)
    pattern = f"{stepatlas_path / 'site'}.*astro.*preview"
    found = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    pids = [pid for pid in found.stdout.split() if pid]
    for pid in pids:
        subprocess.run(["kill", pid])
    typer.echo(json.dumps({"stopped": [int(pid) for pid in pids]}))


def _matches_file_line(step: dict, file: str, line: int) -> bool:
    return step["file"].endswith(file) and step["line"] == line


def _nearest_before(steps: list[dict], file: str, line: int) -> dict | None:
    candidates = [s for s in steps if s["file"].endswith(file) and s["line"] <= line]
    return max(candidates, key=lambda s: s["line"]) if candidates else None


@app.command("info")
def info(
    query: str = typer.Argument(
        ..., help='A text fragment (pattern/function), or "file.py:line".'
    ),
) -> None:
    """Look up step(s) from StepAtlas's catalog.json by text or file:line."""
    config = AitlcConfig.find_and_load()
    stepatlas_path = _require_stepatlas_path(config)
    catalog_path = stepatlas_path / "catalog.json"
    if not catalog_path.is_file():
        typer.echo(
            json.dumps(
                {
                    "error": f"{catalog_path} not found",
                    "hint": "run `aitlc stepatlas build` first",
                }
            ),
            err=True,
        )
        raise typer.Exit(code=2)
    steps = json.loads(catalog_path.read_text())["steps"]

    file_line = re.match(r"^(.+):(\d+)$", query)
    matches: list[dict]
    if file_line:
        file, line = file_line.group(1), int(file_line.group(2))
        exact = [s for s in steps if _matches_file_line(s, file, line)]
        if exact:
            matches = exact
        else:
            nearest = _nearest_before(steps, file, line)
            matches = [nearest] if nearest else []
    else:
        q = query.lower()
        matches = [
            s
            for s in steps
            if q in s["pattern"].lower()
            or q in s["function"].lower()
            or q in " ".join(s["keywords"]).lower()
        ]

    if not matches:
        typer.echo(json.dumps({"query": query, "count": 0, "matches": []}), err=True)
        raise typer.Exit(code=2)

    truncated = len(matches) > _MAX_MATCHES
    payload = {
        "query": query,
        "count": len(matches),
        "truncated": truncated,
        "matches": matches[:_MAX_MATCHES],
    }
    typer.echo(json.dumps(payload, indent=2))


# Mounted by commands/_registry.py.
COMMAND = {"name": "stepatlas", "attr": "app", "kind": "group", "order": 250}
