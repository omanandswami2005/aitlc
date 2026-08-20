"""`aitlc debug` drives the REAL gated behave process end to end.

Only the browser launch (chrome_cdp.launch) and the poetry wrapper are replaced
-- everything the debug commands own runs for real, and the thing they drive is
a genuine behave process single-stepping through the gate. So a wrong reply
shape, a lost socket, or a mis-parsed status is a real bug, not a stubbed one.

Pure-Python steps, so no browser is needed; the faked cdp_url is simply ignored
by steps that never touch a page.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from aitlc.commands import debug_cmd
from aitlc.core import chrome_cdp

runner = CliRunner()

FEATURE = """Feature: gate demo

  Scenario: three steps
    Given the setup step runs
    When the first real step runs
    Then the second real step runs
"""

STEPS = '''\
from behave import given, when, then


@given("the setup step runs")
def _s(context):
    pass


@when("the first real step runs")
def _w(context):
    pass


@then("the second real step runs")
def _t(context):
    pass


@when("the first real step runs FASTER")
def _w2(context):
    pass
'''


class _Cfg:
    def __init__(self, root: Path, feature: Path) -> None:
        self.root_dir = root
        self._feature = feature
        self.playwright_cdp_env = "PLAYWRIGHT_CDP_URL"
        self.scenario_setup = None
        self.step_dir = "features/steps"
        self.browser_actions = None
        self.browser_factory = None

    def resolve_feature_path(self, _tid):
        return self._feature

    def default_feature_id(self):
        return self._feature.stem


def _wire(monkeypatch, tmp_path):
    (tmp_path / "features" / "steps").mkdir(parents=True)
    feature = tmp_path / "features" / "g.feature"
    feature.write_text(FEATURE)
    (tmp_path / "features" / "steps" / "steps.py").write_text(STEPS)

    cfg = _Cfg(tmp_path, feature)
    monkeypatch.setattr(debug_cmd.AitlcConfig, "find_and_load", staticmethod(lambda: cfg))
    monkeypatch.setattr(debug_cmd, "load_dotenv", lambda *_a, **_k: True)
    # Fake ONLY the browser launch (the OS/Chrome boundary). The faked URL is
    # harmless: the pure-Python steps never attach to it.
    monkeypatch.setattr(
        debug_cmd.chrome_cdp,
        "launch",
        lambda *a, **k: (
            type("I", (), {"cdp_url": "http://127.0.0.1:9999", "port": 9999})(),
            False,
        ),
    )
    monkeypatch.setattr(debug_cmd.chrome_cdp, "stop_all", lambda *a, **k: [])

    # Launch the gate with the venv's real behave, bypassing the poetry wrapper
    # (poetry is not importable in the test venv). Everything else in
    # _launch_gate -- the runner attach, the gate env, the socket -- is exercised
    # via the real runner regardless.
    behave_bin = str(Path(sys.executable).parent / "behave")
    real_launch_gate = debug_cmd._launch_gate

    def fake_launch_gate(config, *, feature, line, at, example, cdp_url, socket_path, progress_path, env_file):
        import os
        import subprocess
        from aitlc.runtime import attach

        env = {
            **os.environ,
            "AITLC_GATE": "1",
            "AITLC_GATE_SOCKET": str(socket_path),
            "AITLC_GATE_AT": str(at),
            "AITLC_GATE_EXAMPLE": str(example),
            "AITLC_GATE_PROGRESS": str(progress_path),
            config.playwright_cdp_env: cdp_url,
            "PYTHONPATH": str(debug_cmd._aitlc_src()),
        }
        if socket_path.exists():
            socket_path.unlink()
        target = f"{feature}:{line}" if line else str(feature)
        proc = subprocess.Popen(
            [behave_bin, target, "--runner", attach.RUNNER_PATH,
             "--no-capture", "--stop"],
            cwd=str(config.root_dir), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return proc.pid

    monkeypatch.setattr(debug_cmd, "_launch_gate", fake_launch_gate)
    assert real_launch_gate is not None  # sanity: the real one exists to be replaced
    return cfg


def test_debug_start_next_retry_stop_over_the_real_gate(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)

    # start: park before step index 1 (the When); the Given has run.
    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["parked_at"] == 1
    assert payload["total_steps"] == 3
    assert payload["current_step"] == "When the first real step runs"
    assert payload["engine"] == "behave-gate"

    # status: same position, from a fresh command (reads the saved session).
    result = runner.invoke(debug_cmd.app, ["status", "PROJ-1"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["parked_at"] == 1

    # next: run the When, advance to the Then.
    result = runner.invoke(debug_cmd.app, ["next", "PROJ-1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "passed"
    assert payload["index"] == 2

    # retry: re-run the current step (the Then) without advancing.
    result = runner.invoke(debug_cmd.app, ["retry", "PROJ-1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "passed"
    assert payload["index"] == 2

    # stop: tear the gate down and drop the session.
    result = runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["gate_stopped"] is True


def test_debug_picks_up_a_gherkin_edit_without_restart(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    feature_path = tmp_path / "features" / "g.feature"

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["current_step"] == "When the first real step runs"

    # Edit the feature under the live session: change the step at the cursor.
    feature_path.write_text(
        FEATURE.replace(
            "When the first real step runs", "When the first real step runs FASTER"
        )
    )

    # next reloads the feature first (no restart), so it runs the EDITED step.
    result = runner.invoke(debug_cmd.app, ["next", "PROJ-1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["step"] == "When the first real step runs FASTER"
    assert payload["status"] == "passed"
    assert "feature" in payload  # the cursor move was reported

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_debug_with_no_id_uses_the_default_feature(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    # No test id at all — resolves to the sole feature (stem "g").
    result = runner.invoke(debug_cmd.app, ["start", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["test_id"] == "g"
    assert payload["parked_at"] == 1

    # status/stop with no id default to the same session key.
    result = runner.invoke(debug_cmd.app, ["status"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["parked_at"] == 1

    runner.invoke(debug_cmd.app, ["stop"])
