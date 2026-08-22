"""`aitlc env` — activate the venv and load .env in one command.

    source "$(aitlc env)"

One line replaces the poetry shell + PYTHONPATH export + manual multiline
.env parsing dance that run.md otherwise spells out as a dozen commands
every debug session repeats.

Deliberately does NOT print secret values to stdout. `aitlc`'s own output is
routinely captured by whatever is driving it -- an AI agent's tool call, a
CI log, a terminal scrollback kept for later -- and every one of those is a
place a plaintext credential must not land. So this writes the `export`
lines (real secret values included) to a workspace file with owner-only
permissions and prints ONLY that path; `source "$(...)"` still makes it a
single command, but nothing sensitive ever appears in the command's own
captured output. Found live building this: the first version printed
everything straight to stdout, and the very next command run through this
console dumped a full AWS session token set and an RSA private key into the
transcript.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import typer
from aitlc.config import AitlcConfig
from aitlc.core import workspace
from aitlc.core.behave_runner import resolve_poetry


def _venv_activate_line(root_dir: Path) -> str | None:
    """Return a `source .../activate` line for this project's poetry venv, or None.

    None (not an error) when poetry can't resolve a venv here — the caller
    just gets exports without an activate line rather than a failed eval.
    """
    try:
        proc = subprocess.run(
            resolve_poetry() + ["env", "info", "--path"],
            cwd=root_dir,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    venv_path = proc.stdout.strip()
    activate = Path(venv_path) / "bin" / "activate"
    if not venv_path or not activate.exists():
        return None
    return f"source {shlex.quote(str(activate))}"


def _export_lines(env_file: Path) -> list[str]:
    """Parse KEY=VALUE lines from env_file into `export` statements.

    Handles a double-quoted value spanning multiple lines (PEM keys and
    similar) the same way run.md's manual bash snippet does: everything up
    to the line whose quote closes it is one value, joined with real
    newlines inside the export via $'...' quoting.
    """
    if not env_file.exists():
        return []
    lines = env_file.read_text().splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in raw:
            i += 1
            continue
        key, _, value = raw.partition("=")
        key = key.strip()
        if value.startswith('"') and value.count('"') % 2 == 1:
            parts = [value]
            i += 1
            while i < len(lines):
                parts.append(lines[i])
                if '"' in lines[i]:
                    i += 1
                    break
                i += 1
            value = "\n".join(parts)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out.append(f"export {key}={shlex.quote(value)}")
        i += 1
    return out


def env(
    env_file: str = typer.Option(
        ".env", "--env-file", help="Path to load KEY=VALUE pairs from."
    ),
    no_activate: bool = typer.Option(
        False,
        "--no-activate",
        help="Skip the venv `source` line; write only PYTHONPATH + .env exports.",
    ),
) -> None:
    """Write venv-activate + .env exports to a private file; print its path.

        source "$(aitlc env)"

    The path is the only thing on stdout — see this module's docstring for
    why secret values themselves never are.
    """
    config = AitlcConfig.find_and_load()
    lines: list[str] = []
    if not no_activate:
        activate = _venv_activate_line(config.root_dir)
        if activate:
            lines.append(activate)
    lines.append(f"export PYTHONPATH={shlex.quote(str(config.root_dir))}")
    lines.extend(_export_lines(config.root_dir / env_file))

    out_path = workspace.ensure(config.root_dir, ".env_activate.sh")
    out_path.write_text("\n".join(lines) + "\n")
    out_path.chmod(0o600)
    typer.echo(str(out_path))


# Mounted by commands/_registry.py (see that module for the convention).
COMMAND = {"name": "env", "attr": "env", "order": 15}
