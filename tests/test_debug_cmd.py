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
    monkeypatch.setattr(
        debug_cmd.chrome_cdp,
        "launch",
        lambda *a, **k: type(
            "I", (), {"cdp_url": "http://127.0.0.1:9999", "port": 9999}
        )(),
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
    runner.invoke(debug_cmd.app, ["next", "PROJ-1"])
    session = load(tmp_path, "PROJ-1")
    assert session.index == 1
    assert [a.status for a in session.attempts] == ["passed"]


def test_commands_refuse_without_a_session(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, [])
    result = runner.invoke(debug_cmd.app, ["retry", "NOPE-1"])
    assert result.exit_code == 2
    assert "no debug session" in result.output
