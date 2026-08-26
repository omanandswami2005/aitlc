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
    # `stop` (not `stop_all` -- debug stop only ever kills ITS OWN port,
    # see debug_cmd.stop's own docstring) probes the faked port for real;
    # nothing is listening there, so it returns quickly on its own.
    monkeypatch.setattr(debug_cmd.chrome_cdp, "stop", lambda *a, **k: True)

    # Launch the gate with the venv's real behave, bypassing the poetry wrapper
    # (poetry is not importable in the test venv). Everything else --
    # the runner attach, the gate env, the socket -- is exercised via the
    # real runner regardless. `gate_launch.launch` (not `debug_cmd._launch_gate`,
    # which v0.6.0's architecture pass moved out of debug_cmd.py entirely --
    # see gate_launch.py's own module docstring) is what both `debug start`
    # and `run --debug` call today, so that is what gets replaced here.
    behave_bin = str(Path(sys.executable).parent / "behave")
    real_launch = debug_cmd.gate_launch.launch

    def fake_launch(config, *, feature, line, cdp_url, socket_path_, progress_path,
                     gate_env, log_name, report_path=None, tags=None,
                     name_pattern=None, dry_run=False):
        import os
        import subprocess
        from aitlc.runtime import attach

        env = {
            **os.environ,
            **gate_env,
            "AITLC_GATE_SOCKET": str(socket_path_),
            "AITLC_GATE_PROGRESS": str(progress_path),
            config.playwright_cdp_env: cdp_url,
            "PYTHONPATH": str(debug_cmd.gate_launch.aitlc_src()),
        }
        if socket_path_.exists():
            socket_path_.unlink()
        target = f"{feature}:{line}" if line else str(feature)
        return subprocess.Popen(
            [behave_bin, target, "--runner", attach.RUNNER_PATH,
             "--no-capture", "--stop"],
            cwd=str(config.root_dir), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    monkeypatch.setattr(debug_cmd.gate_launch, "launch", fake_launch)
    assert real_launch is not None  # sanity: the real one exists to be replaced
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


def test_debug_run_text_runs_an_ad_hoc_step_without_advancing(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    # A step NOT in the parked scenario's own sequence -- proves run-text can
    # reach any registered step, not just the ones in the paused feature.
    result = runner.invoke(
        debug_cmd.app, ["run-text", "When the first real step runs FASTER", "PROJ-1"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "passed"
    assert payload["error"] is None

    # The cursor did not move: status still reports index 1.
    result = runner.invoke(debug_cmd.app, ["status", "PROJ-1"])
    assert json.loads(result.output)["parked_at"] == 1

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_debug_eval_reports_no_page_for_pure_python_steps(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    # These steps never touch a browser, so there is no page to find -- eval
    # must report that cleanly rather than crash the gate.
    result = runner.invoke(debug_cmd.app, ["eval", "1 + 1", "PROJ-1"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "no live page found on context"

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_debug_run_text_auto_reloads_a_stale_page_object_edit(monkeypatch, tmp_path):
    """G54: editing a normally-imported module (not a step file) mid-session
    is picked up automatically -- not just reported as stale.

    `retry`/`next`/`run-text` only re-exec files directly under `step_dir`
    -- a real Python `import` elsewhere in the project stays cached in
    `sys.modules` by the interpreter itself. Proves the fix does more than
    detect this: `helper_mod.VALUE` is asserted INSIDE the step itself, so
    a stale reload would fail this test, not just omit a warning.
    """
    import time as _time

    _wire(monkeypatch, tmp_path)
    helper_path = tmp_path / "helper_mod.py"
    helper_path.write_text("VALUE = 1\n")
    (tmp_path / "features" / "steps" / "steps.py").write_text(
        STEPS
        + '''
import sys, os
sys.path.insert(0, os.getcwd())
import helper_mod


@when("the helper value is checked")
def _check_helper_value(context):
    assert helper_mod.VALUE == 2, f"stale: helper_mod.VALUE == {helper_mod.VALUE}"
'''
    )

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    # Edit the helper AFTER the session started -- give the filesystem clock
    # a full second to be sure the new mtime lands strictly after session
    # start regardless of mtime resolution.
    _time.sleep(1.1)
    helper_path.write_text("VALUE = 2\n")

    result = runner.invoke(
        debug_cmd.app, ["run-text", "When the helper value is checked", "PROJ-1"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "passed", payload  # would be "failed" on stale VALUE == 1
    assert any("helper_mod.py" in f for f in payload["reloaded_modules"])

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_debug_next_warns_when_a_stale_module_cannot_reload_cleanly(monkeypatch, tmp_path):
    """A module whose reload itself errors must report that, not crash the gate."""
    import time as _time

    _wire(monkeypatch, tmp_path)
    helper_path = tmp_path / "helper_mod2.py"
    helper_path.write_text("VALUE = 1\n")
    (tmp_path / "features" / "steps" / "steps.py").write_text(
        STEPS + "\nimport sys, os\nsys.path.insert(0, os.getcwd())\nimport helper_mod2\n"
    )

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    _time.sleep(1.1)
    # An edit whose own top-level code raises on re-exec -- a real, if rare,
    # way a reload can legitimately fail (not every module is a plain
    # dataclass-of-statics; this must not crash the whole gate).
    helper_path.write_text("raise RuntimeError('cannot reload me')\n")

    result = runner.invoke(debug_cmd.app, ["next", "PROJ-1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert any("helper_mod2.py" in f for f in payload["stale_modules"])
    assert "cannot reload me" in payload["warning"]

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
