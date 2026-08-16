"""Run a target-project script under its own environment, structured.

Every wrapper in this package shells out the same way — `poetry run python3
<script> <args>` from the project root — so the mechanics live here once
rather than being re-derived (and subtly diverging) per command.

Two properties the wrappers depend on:

* **The project's own env, not aitlc's.** These scripts import the target's
  modules (`config.configs`, `helper.api_clients...`), which only resolve
  under the target's poetry environment. Running them with aitlc's
  interpreter fails at import time.
* **Output is captured and redacted.** The scripts log freely and some
  handle credentials; echoing raw stdout into a JSON result is how a token
  ends up in a terminal scrollback or a pasted bug report.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from aitlc.core.redact import redact_text

# Enough trailing output to see the outcome and any traceback, without
# dumping a multi-thousand-line log into a JSON payload.
DEFAULT_TAIL_LINES = 40


@dataclass
class ScriptResult:
    """The outcome of running a target-project script."""

    command: list[str]
    exit_code: int
    stdout_tail: str
    stderr_tail: str

    @property
    def passed(self) -> bool:
        """True when the script exited successfully."""
        return self.exit_code == 0

    def to_dict(self) -> dict:
        """Return a JSON-serializable form of this result."""
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "passed": self.passed,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


def _tail(text: str, lines: int) -> str:
    if not text:
        return ""
    split = text.splitlines()
    return "\n".join(split[-lines:])


def run_project_script(
    script_path: str,
    *,
    cwd: Path,
    poetry_cmd: list[str],
    args: list[str] | None = None,
    secret_values: list[str] | None = None,
    tail_lines: int = DEFAULT_TAIL_LINES,
    env: dict[str, str] | None = None,
) -> ScriptResult:
    """Execute `script_path` under the target project's poetry environment."""
    cmd = poetry_cmd + ["run", "python3", script_path] + list(args or [])

    proc_env = None
    if env:
        import os

        proc_env = {**os.environ, **env}

    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        env=proc_env,
    )

    secrets = [s for s in (secret_values or []) if s]
    return ScriptResult(
        command=cmd,
        exit_code=proc.returncode,
        stdout_tail=redact_text(_tail(proc.stdout, tail_lines), secrets),
        stderr_tail=redact_text(_tail(proc.stderr, tail_lines), secrets),
    )
