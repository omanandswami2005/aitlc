"""CLI behaviour of the debug session, with the browser and behave stubbed."""

from __future__ import annotations

import json
from pathlib import Path

from aitlc.commands import debug_cmd
from aitlc.core.debug_session import DebugSession, load, save
from typer.testing import CliRunner

runner = CliRunner()

FEATURE = """Feature: f

\t@TEST_PROJ-1
\tScenario: s
\tGiven open the app
\tWhen click the button
\tThen validate the result
"""


class _Cfg:
    def __init__(self, root: Path, feature: Path) -> None:
        self.root_dir = root
        self._feature = feature
        self.scenario_setup = None
        self.step_dir = "features/steps"
        self.browser_actions = None
        self.browser_factory = None

    def resolve_feature_path(self, _test_id: str) -> Path:
        return self._feature


def _wire(monkeypatch, tmp_path, ran: list):
    feature = tmp_path / "f.feature"
    feature.write_text(FEATURE)
    cfg = _Cfg(tmp_path, feature)
    monkeypatch.setattr(
        debug_cmd.AitlcConfig, "find_and_load", staticmethod(lambda: cfg)
    )
    monkeypatch.setattr(debug_cmd, "load_dotenv", lambda *_a, **_k: True)
    # Must mirror chrome_cdp.launch's real signature -> (instance, reused).
    # An earlier fake returned the bare instance, which matched a buggy call
    # site and let `debug start` ship crashing on every invocation.
    monkeypatch.setattr(
        debug_cmd.chrome_cdp,
        "launch",
        lambda *a, **k: (
            type("I", (), {"cdp_url": "http://127.0.0.1:9999", "port": 9999})(),
            False,
        ),
    )

    def fake_run_steps(_config, _session, steps):
        ran.append(list(steps))
        return {
            "results": [
                {"step": s, "status": "passed", "duration_s": 0.1, "error": None}
                for s in steps
            ],
            "stderr_tail": "",
            "unhandled_events": [],
        }

    monkeypatch.setattr(debug_cmd, "_run_steps", fake_run_steps)
    return cfg


def test_start_parks_on_a_step_and_records_the_browser(monkeypatch, tmp_path):
    ran: list = []
    _wire(monkeypatch, tmp_path, ran)
    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "2"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["parked_at"] == 2
    assert payload["total_steps"] == 3
    assert payload["cdp_url"] == "http://127.0.0.1:9999"
    # the two steps before the parked one were run to reach the state
    assert len(ran[0]) == 2

    session = load(tmp_path, "PROJ-1")
    assert session is not None and session.index == 2


def test_retry_reruns_only_the_current_step(monkeypatch, tmp_path):
    """The whole point: after an edit, do not restart the scenario."""
    ran: list = []
    _wire(monkeypatch, tmp_path, ran)
    save(
        tmp_path,
        DebugSession(
            test_id="PROJ-1",
            feature=str(tmp_path / "f.feature"),
            cdp_url="http://127.0.0.1:9999",
            port=9999,
            steps=["Given open the app", "When click the button", "Then validate"],
            index=1,
        ),
    )
    result = runner.invoke(debug_cmd.app, ["retry", "PROJ-1"])
    assert result.exit_code == 0, result.output
    assert ran[-1] == ["When click the button"]
    assert load(tmp_path, "PROJ-1").index == 1  # position unchanged


def test_next_advances_and_history_accumulates(monkeypatch, tmp_path):
    ran: list = []
    _wire(monkeypatch, tmp_path, ran)
    save(
        tmp_path,
        DebugSession(
            test_id="PROJ-1",
            feature=str(tmp_path / "f.feature"),
            cdp_url="http://127.0.0.1:9999",
            port=9999,
            steps=["Given a", "When b", "Then c"],
            index=0,
        ),
    )
    # This asserted the old behaviour, where the first `next` advanced past
    # the parked step without running it. Moving forward through a scenario
    # cannot mean skipping a step, so the first call now runs the step under
    # the cursor and only the second one moves on.
    runner.invoke(debug_cmd.app, ["next", "PROJ-1"])
    session = load(tmp_path, "PROJ-1")
    assert session.index == 0
    assert [a.status for a in session.attempts] == ["passed"]

    runner.invoke(debug_cmd.app, ["next", "PROJ-1"])
    session = load(tmp_path, "PROJ-1")
    assert session.index == 1
    assert [a.status for a in session.attempts] == ["passed", "passed"]


def test_commands_refuse_without_a_session(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, [])
    result = runner.invoke(debug_cmd.app, ["retry", "NOPE-1"])
    assert result.exit_code == 2
    assert "no debug session" in result.output


def _session_at(tmp_path, steps, index):
    save(
        tmp_path,
        DebugSession(
            test_id="PROJ-1",
            feature=str(tmp_path / "f.feature"),
            cdp_url="http://127.0.0.1:9999",
            port=9999,
            steps=steps,
            index=index,
        ),
    )


def test_retry_and_next_load_env_before_dispatching(monkeypatch, tmp_path):
    """`start` loaded .env; `retry`/`next` did not.

    The step modules need config at import time, so without it every step
    resolved as `undefined`, the result list came back empty, and the command
    reported a failure for a step that never ran -- six times in a row on
    working code, during a real investigation.
    """
    for command in ("retry", "next"):
        ran: list = []
        _wire(monkeypatch, tmp_path, ran)
        loaded: list = []
        monkeypatch.setattr(
            debug_cmd, "load_dotenv", lambda path, *_a, **_k: loaded.append(path)
        )
        _session_at(tmp_path, ["Given open the app", "When click the button"], 0)

        result = runner.invoke(debug_cmd.app, [command, "PROJ-1"])

        assert result.exit_code == 0, result.output
        assert loaded, f"`debug {command}` dispatched without loading .env"


def test_a_step_that_never_ran_is_not_reported_as_failed(monkeypatch, tmp_path):
    """Empty results mean the console never ran it -- a third outcome.

    Calling that "failed" sends someone to fix code that is fine.
    """
    ran: list = []
    _wire(monkeypatch, tmp_path, ran)
    monkeypatch.setattr(
        debug_cmd,
        "_run_steps",
        lambda *_a, **_k: {"results": [], "stderr_tail": "", "unhandled_events": []},
    )
    _session_at(tmp_path, ["Given open the app", "When click the button"], 1)

    result = runner.invoke(debug_cmd.app, ["retry", "PROJ-1"])

    payload = json.loads(result.output)
    assert payload["status"] == "not_run"
    assert "did not run" in payload["message"]
    assert "error" not in payload


def test_a_lone_continuation_step_is_promoted_before_dispatch(monkeypatch, tmp_path):
    """`And ...` cannot open a parsed block, and half of real lines start that way."""
    ran: list = []
    _wire(monkeypatch, tmp_path, ran)
    _session_at(tmp_path, ["Given open the app", "And click the button"], 1)

    result = runner.invoke(debug_cmd.app, ["retry", "PROJ-1"])

    assert result.exit_code == 0, result.output
    assert ran[-1] == ["When click the button"]


def test_next_runs_the_parked_step_before_moving_past_it(monkeypatch, tmp_path):
    """`start --at N` parks on N without running it.

    Advancing first meant the very first `next` skipped N, and every later
    step that depended on it failed -- which reads as the application being
    broken rather than a step never having run.
    """
    ran: list = []
    _wire(monkeypatch, tmp_path, ran)
    _session_at(tmp_path, ["Given open the app", "When open the panel", "Then use it"], 1)

    result = runner.invoke(debug_cmd.app, ["next", "PROJ-1"])

    assert result.exit_code == 0, result.output
    assert ran[-1] == ["When open the panel"], "the parked step must actually run"
    assert load(tmp_path, "PROJ-1").index == 1


def test_next_advances_once_the_current_step_has_run(monkeypatch, tmp_path):
    ran: list = []
    _wire(monkeypatch, tmp_path, ran)
    _session_at(tmp_path, ["Given open the app", "When open the panel", "Then use it"], 1)

    runner.invoke(debug_cmd.app, ["next", "PROJ-1"])   # runs the parked step
    runner.invoke(debug_cmd.app, ["next", "PROJ-1"])   # now moves on

    assert ran[-1] == ["Then use it"]
    assert load(tmp_path, "PROJ-1").index == 2


def test_console_start_loads_env_before_spawning(monkeypatch, tmp_path):
    """The console is a child that inherits this process's environment.

    Without the env file every step module fails to import, the scenario
    setup that mints run-scoped values raises, and the console dies before it
    listens -- reported only as "did not begin listening in time".
    """
    _wire(monkeypatch, tmp_path, [])
    loaded: list = []
    monkeypatch.setattr(debug_cmd, "load_dotenv", lambda path, *_a, **_k: loaded.append(path))
    monkeypatch.setattr(
        debug_cmd, "_launch_console", lambda *_a, **_k: {"started": True}
    )
    _session_at(tmp_path, ["Given open the app"], 0)

    result = runner.invoke(debug_cmd.app, ["console", "PROJ-1", "--start"])

    assert result.exit_code == 0, result.output
    assert loaded, "`debug console --start` spawned a console without loading .env"
