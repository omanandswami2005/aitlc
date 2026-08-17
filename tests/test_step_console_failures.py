"""A step child that dies must not look like a run with no steps.

Both cases here were observed live: a `--mobile` + `--cdp-url` invocation with
no browser_factory (fatal, message on stderr), and a `--range` slice starting on
an `And` step (fatal, reported as a structured parse_error event). Each printed
`{"loaded_step_modules": N, "results": []}` and exited 0.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from aitlc.core import step_console


class _Proc:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _run(monkeypatch, stdout: str, stderr: str = "", code: int = 0):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(stdout, stderr, code))
    return step_console.run_console(
        Path("some.feature"), cwd=Path("."), poetry_cmd=["poetry"]
    )


def test_stderr_is_carried_out_when_the_child_fails(monkeypatch):
    result = _run(
        monkeypatch,
        stdout=json.dumps({"event": "loaded_step_modules", "count": 82}),
        stderr="--mobile with --cdp-url needs a project browser factory.",
        code=1,
    )
    assert result.results == []
    assert result.exit_code == 1
    assert "browser factory" in result.stderr_tail


def test_unrecognised_child_events_are_surfaced_not_dropped(monkeypatch):
    """The child reports a bad slice as structured JSON; don't swallow it."""
    stdout = "\n".join(
        [
            json.dumps({"event": "loaded_step_modules", "count": 82}),
            json.dumps({"event": "parse_error", "error": "No previous step at line 1"}),
        ]
    )
    result = _run(monkeypatch, stdout=stdout)
    assert result.results == []
    assert any(e.get("event") == "parse_error" for e in result.unhandled_events)


def test_a_clean_empty_run_stays_clean(monkeypatch):
    """No stderr, exit 0, no stray events -> nothing invented."""
    result = _run(
        monkeypatch,
        stdout=json.dumps({"event": "loaded_step_modules", "count": 3}),
    )
    assert result.stderr_tail == ""
    assert result.unhandled_events == []
    assert result.exit_code == 0


def test_trace_and_network_events_are_carried_out(monkeypatch):
    """A slice runs without before_scenario, so whatever collector the suite
    installs there is absent — these two events are the substitute, and a
    trace is otherwise only produced on the remote grid."""
    stdout = "\n".join(
        [
            json.dumps({"event": "loaded_step_modules", "count": 3}),
            json.dumps(
                {
                    "event": "network",
                    "responses": [
                        {
                            "operation": "CheckoutCreate",
                            "status": 200,
                            "url": "https://api.example/graphql",
                        }
                    ],
                }
            ),
            json.dumps({"event": "trace_saved", "path": "/tmp/t.zip"}),
        ]
    )
    result = _run(monkeypatch, stdout=stdout)
    assert result.network[0]["operation"] == "CheckoutCreate"
    assert result.trace_path == "/tmp/t.zip"
    # known events must not be misreported as unhandled
    assert result.unhandled_events == []
