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


class _DebugCfg:
    def __init__(self) -> None:
        self.continue_output = "compact"


class _Cfg:
    def __init__(self, root: Path, feature: Path) -> None:
        self.root_dir = root
        self._feature = feature
        self.playwright_cdp_env = "PLAYWRIGHT_CDP_URL"
        self.scenario_setup = None
        self.step_dir = "features/steps"
        self.browser_actions = None
        self.browser_factory = None
        self.debug = _DebugCfg()

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
            # Matches the real gate_launch.launch: stdout goes to a file,
            # not a TTY, so force unbuffered I/O or a step's print()/logging
            # output can sit in the child's own buffer past when the test
            # reads the log.
            "PYTHONUNBUFFERED": "1",
            # Matches the real gate_launch.launch: a breakpoint() in a step
            # must reach aitlc's own hook, not hang on a real pdb prompt
            # nothing here could ever answer.
            "PYTHONBREAKPOINT": "aitlc.runtime.runner._aitlc_breakpointhook",
        }
        if socket_path_.exists():
            socket_path_.unlink()
        target = f"{feature}:{line}" if line else str(feature)
        # Real gate_launch.launch redirects the gate's stdout/stderr into
        # workspace.ensure(...)'s .aitlc/debug/<log_name> -- next/retry/
        # continue tail that exact file for a step's real console output
        # (--no-capture means it never lands anywhere else), so this fake
        # must write to the SAME path `workspace` resolves (under the
        # "reports" default workspace prefix), not a hand-rolled one that
        # would silently diverge from what debug_cmd.py's `start()` records
        # as `session.log_path`.
        from aitlc.core import workspace as _workspace

        log_path = _workspace.ensure(config.root_dir, ".aitlc", "debug", log_name)
        with log_path.open("ab") as log_handle:
            return subprocess.Popen(
                [behave_bin, target, "--runner", attach.RUNNER_PATH,
                 "--no-capture", "--stop"],
                cwd=str(config.root_dir), env=env,
                stdout=log_handle, stderr=subprocess.STDOUT,
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
    payload = json.loads(result.stdout)
    assert payload["parked_at"] == 1
    assert payload["total_steps"] == 3
    assert payload["current_step"] == "When the first real step runs"
    assert payload["engine"] == "behave-gate"

    # status: same position, from a fresh command (reads the saved session).
    result = runner.invoke(debug_cmd.app, ["status", "PROJ-1"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["parked_at"] == 1

    # next: run the When, advance to the Then.
    result = runner.invoke(debug_cmd.app, ["next", "PROJ-1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "passed"
    assert payload["index"] == 2

    # retry: re-run the current step (the Then) without advancing.
    result = runner.invoke(debug_cmd.app, ["retry", "PROJ-1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "passed"
    assert payload["index"] == 2

    # stop: tear the gate down and drop the session.
    result = runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["gate_stopped"] is True


def test_debug_continue_runs_all_remaining_steps_in_one_command(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "0", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(debug_cmd.app, ["continue", "PROJ-1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["stopped_reason"] == "finished"
    assert payload["steps_run"] == 3
    assert all(r["status"] == "passed" for r in payload["results"])

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_debug_continue_stops_at_the_first_failure(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    (tmp_path / "features" / "steps" / "steps.py").write_text(
        STEPS.replace(
            '@when("the first real step runs")\ndef _w(context):\n    pass',
            '@when("the first real step runs")\ndef _w(context):\n    assert False, "boom"',
        )
    )

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "0", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(debug_cmd.app, ["continue", "PROJ-1"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["stopped_reason"] == "failed"
    assert payload["steps_run"] == 2  # the passing Given, then the failing When
    assert payload["results"][-1]["status"] == "failed"

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_continue_compact_output_drops_captured_output_but_journal_keeps_it(
    monkeypatch, tmp_path
):
    """Compact (the default) must strip captured_output/traceback/page_state
    from the STDOUT summary -- already shown once, live, per step -- while
    the journal entry keeps the full, untrimmed record regardless. --full
    must restore the complete records on stdout too.
    """
    from aitlc.core import journal

    _wire(monkeypatch, tmp_path)
    (tmp_path / "features" / "steps" / "steps.py").write_text(
        STEPS.replace(
            '@when("the first real step runs")\ndef _w(context):\n    pass',
            '@when("the first real step runs")\ndef _w(context):\n    print("hello")',
        )
    )

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "0", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(debug_cmd.app, ["continue", "PROJ-1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert all("captured_output" not in r for r in payload["results"])
    assert all(set(r) <= set(debug_cmd._COMPACT_STEP_FIELDS) for r in payload["results"])

    entries = journal.entries(tmp_path)
    continue_entry = next(e for e in entries if e.command == "debug continue")
    assert any("hello" in (r.get("captured_output") or "") for r in continue_entry.payload["results"])

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "0", "--timeout", "60"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(debug_cmd.app, ["continue", "PROJ-1", "--full"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert any("hello" in (r.get("captured_output") or "") for r in payload["results"])

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_debug_picks_up_a_gherkin_edit_without_restart(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    feature_path = tmp_path / "features" / "g.feature"

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["current_step"] == "When the first real step runs"

    # Edit the feature under the live session: change the step at the cursor.
    feature_path.write_text(
        FEATURE.replace(
            "When the first real step runs", "When the first real step runs FASTER"
        )
    )

    # next reloads the feature first (no restart), so it runs the EDITED step.
    result = runner.invoke(debug_cmd.app, ["next", "PROJ-1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
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
    payload = json.loads(result.stdout)
    assert payload["status"] == "passed"
    assert payload["error"] is None

    # The cursor did not move: status still reports index 1.
    result = runner.invoke(debug_cmd.app, ["status", "PROJ-1"])
    assert json.loads(result.stdout)["parked_at"] == 1

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_debug_eval_reports_no_page_for_pure_python_steps(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    # These steps never touch a browser, so there is no page to find -- eval
    # must report that cleanly rather than crash the gate.
    result = runner.invoke(debug_cmd.app, ["eval", "1 + 1", "PROJ-1"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
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
    payload = json.loads(result.stdout)
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
    payload = json.loads(result.stdout)
    assert any("helper_mod2.py" in f for f in payload["stale_modules"])
    assert "cannot reload me" in payload["warning"]

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_debug_next_survives_ambiguous_pattern_across_reload_cycles(monkeypatch, tmp_path):
    """G56: a specific/general pattern overlap across two files must not make
    either step vanish after the first reload cycle.

    Real bug found live: `_reload_steps` evicted-then-re-added one file at a
    time. On the 2nd+ cycle, a file already re-added this cycle could
    transiently collide with another file's entry not yet re-evicted this
    cycle (still holding last cycle's registration) -- if the two files have
    a specific-vs-general pattern overlap, that transient coexistence raises
    AmbiguousStep, silently dropping everything defined later in the failing
    file. Never happens on the real one-time initial load, only on reload.
    """
    _wire(monkeypatch, tmp_path)
    steps_dir = tmp_path / "features" / "steps"
    (steps_dir / "aaa_specific.py").write_text(
        '''
from behave import then

@then('click on "{option}" for contact name "{first_name}" and "{last_name}"')
def _specific(context, option, first_name, last_name):
    pass


@then("canary step ran")
def _canary(context):
    pass
'''
    )
    (steps_dir / "zzz_general.py").write_text(
        '''
from behave import then

@then('click on "{text}"')
def _general(context, text):
    pass
'''
    )

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "0", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    # Reload cycle #1 (via next): clean, matches the real initial load --
    # the bug only ever showed up from the 2nd cycle onward.
    result = runner.invoke(debug_cmd.app, ["next", "PROJ-1"])
    assert result.exit_code == 0, result.output

    # Reload cycle #2 (via run-text, which reloads before running): this is
    # where the transient collision used to raise AmbiguousStep and
    # silently drop "canary step ran".
    result = runner.invoke(debug_cmd.app, ["run-text", "Then canary step ran", "PROJ-1"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "passed"

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_debug_with_no_id_uses_the_default_feature(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    # No test id at all — resolves to the sole feature (stem "g").
    result = runner.invoke(debug_cmd.app, ["start", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["test_id"] == "g"
    assert payload["parked_at"] == 1

    # status/stop with no id default to the same session key.
    result = runner.invoke(debug_cmd.app, ["status"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["parked_at"] == 1

    runner.invoke(debug_cmd.app, ["stop"])


def test_debug_start_reuses_a_live_cdp_launch_instance_instead_of_isolating(
    monkeypatch, tmp_path
):
    """A tracked, running `aitlc cdp launch` browser must be reused by
    `debug start` -- launching an isolated one unconditionally (the old
    behavior) orphaned a second Chrome window on every `debug start`,
    defeating the point of `cdp launch` once per day.
    """
    _wire(monkeypatch, tmp_path)

    launch_calls = []
    monkeypatch.setattr(
        debug_cmd.chrome_cdp,
        "launch",
        lambda *a, **k: launch_calls.append((a, k))
        or (type("I", (), {"cdp_url": "http://127.0.0.1:9999", "port": 9999})(), False),
    )

    tracked = type(
        "I", (), {"cdp_url": "http://127.0.0.1:8888", "port": 8888}
    )()
    monkeypatch.setattr(
        debug_cmd.chrome_cdp,
        "list_instances",
        lambda *_a, **_k: [{"port": 8888, "running": True}],
    )
    monkeypatch.setattr(
        debug_cmd.chrome_cdp, "load_state", lambda *_a, **_k: tracked
    )
    mark_driven_calls = []
    monkeypatch.setattr(
        debug_cmd.chrome_cdp,
        "mark_driven",
        lambda root, port, driver: mark_driven_calls.append((port, driver)),
    )

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)

    assert launch_calls == []  # never fell back to an isolated browser
    assert payload["cdp_url"] == "http://127.0.0.1:8888"
    assert payload["reused"] is True
    assert mark_driven_calls == [(8888, "PROJ-1")]

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_debug_start_warns_when_reusing_a_previously_driven_instance(monkeypatch, tmp_path):
    """`debug start` itself (not just plain `run`) must warn when the
    tracked instance it's about to attach to has been driven before --
    `is_dirty_for` (used by `run --debug`) only fires for a *different*
    driver, so the same test_id reusing its own browser across repeated
    `debug start` attempts previously got no warning at all.
    """
    _wire(monkeypatch, tmp_path)

    tracked = type(
        "I",
        (),
        {
            "cdp_url": "http://127.0.0.1:8888",
            "port": 8888,
            "driven_count": 3,
            "last_driven_by": "PROJ-1",
        },
    )()
    monkeypatch.setattr(
        debug_cmd.chrome_cdp,
        "list_instances",
        lambda *_a, **_k: [{"port": 8888, "running": True}],
    )
    monkeypatch.setattr(debug_cmd.chrome_cdp, "load_state", lambda *_a, **_k: tracked)
    monkeypatch.setattr(debug_cmd.chrome_cdp, "mark_driven", lambda *_a, **_k: None)

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "driven 3 time(s) before" in payload["warning"]

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_debug_restart_reruns_from_zero_on_the_same_browser(monkeypatch, tmp_path):
    """`debug restart` = stop (browser-preserving) + start --at 0 on the SAME
    cdp_url, in one command -- the scenario re-runs from the top without
    paying for a whole new browser. --extra-tag must reach the fresh run's
    hooks too, same as a plain `debug start --extra-tag`.
    """
    _wire(monkeypatch, tmp_path)
    seen_tags = tmp_path / "seen_tags.txt"
    (tmp_path / "features" / "environment.py").write_text(
        f'''
def before_feature(context, feature):
    with open(r"{seen_tags}", "w") as f:
        f.write(",".join(feature.tags))
'''
    )

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output
    original_cdp_url = json.loads(result.stdout)["cdp_url"]

    result = runner.invoke(debug_cmd.app, ["next", "PROJ-1"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(
        debug_cmd.app,
        ["restart", "PROJ-1", "--timeout", "60", "--extra-tag", "skip_login"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["parked_at"] == 0
    assert payload["cdp_url"] == original_cdp_url
    assert "skip_login" in seen_tags.read_text().split(",")

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_debug_start_no_cdp_forces_an_isolated_browser(monkeypatch, tmp_path):
    """--no-cdp must skip a live tracked instance even when one exists."""
    _wire(monkeypatch, tmp_path)

    launch_calls = []
    monkeypatch.setattr(
        debug_cmd.chrome_cdp,
        "launch",
        lambda *a, **k: launch_calls.append((a, k))
        or (type("I", (), {"cdp_url": "http://127.0.0.1:9999", "port": 9999})(), False),
    )
    monkeypatch.setattr(
        debug_cmd.chrome_cdp,
        "list_instances",
        lambda *_a, **_k: [{"port": 8888, "running": True}],
    )
    monkeypatch.setattr(
        debug_cmd.chrome_cdp,
        "load_state",
        lambda *_a, **_k: type("I", (), {"cdp_url": "http://127.0.0.1:8888", "port": 8888})(),
    )

    result = runner.invoke(
        debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60", "--no-cdp"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)

    assert len(launch_calls) == 1  # isolated browser launched despite a live tracked one
    assert payload["cdp_url"] == "http://127.0.0.1:9999"
    assert payload["reused"] is False

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_debug_stop_leaves_a_reused_browser_running_by_default(monkeypatch, tmp_path):
    """A session that only attached to a live `cdp launch` browser (G68's
    reuse) must not kill it on `debug stop` -- otherwise every stop tears
    down the exact persistent browser `cdp launch` exists to keep alive.
    """
    _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(
        debug_cmd.chrome_cdp,
        "list_instances",
        lambda *_a, **_k: [{"port": 8888, "running": True}],
    )
    monkeypatch.setattr(
        debug_cmd.chrome_cdp,
        "load_state",
        lambda *_a, **_k: type("I", (), {"cdp_url": "http://127.0.0.1:8888", "port": 8888})(),
    )
    monkeypatch.setattr(debug_cmd.chrome_cdp, "mark_driven", lambda *a, **k: None)

    stop_calls = []
    monkeypatch.setattr(
        debug_cmd.chrome_cdp, "stop", lambda *a, port=None, **k: stop_calls.append(port) or True
    )

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["reused"] is True

    result = runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)

    assert stop_calls == []  # the reused browser was never killed
    assert payload["browser_left_running"] is True
    assert payload["stopped_port"] is None


def test_debug_stop_kill_browser_overrides_the_reuse_default(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(
        debug_cmd.chrome_cdp,
        "list_instances",
        lambda *_a, **_k: [{"port": 8888, "running": True}],
    )
    monkeypatch.setattr(
        debug_cmd.chrome_cdp,
        "load_state",
        lambda *_a, **_k: type("I", (), {"cdp_url": "http://127.0.0.1:8888", "port": 8888})(),
    )
    monkeypatch.setattr(debug_cmd.chrome_cdp, "mark_driven", lambda *a, **k: None)

    stop_calls = []
    monkeypatch.setattr(
        debug_cmd.chrome_cdp, "stop", lambda *a, port=None, **k: stop_calls.append(port) or True
    )

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(debug_cmd.app, ["stop", "PROJ-1", "--kill-browser"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)

    assert stop_calls == [8888]
    assert payload["browser_left_running"] is False
    assert payload["stopped_port"] == 8888


def test_reload_skips_unchanged_step_files(monkeypatch, tmp_path):
    """An unchanged step file must not be re-exec'd on every next/retry.

    Real cost found live: `_reload_steps` re-executed EVERY .py file under
    step_dir on every single next/retry/continue call, unconditionally --
    on a suite with dozens of step files (this project has 82), that is
    pure avoidable overhead on top of the step's own work when the common
    case during iteration is "one file changed, the rest didn't".
    """
    import time as _time

    _wire(monkeypatch, tmp_path)
    steps_dir = tmp_path / "features" / "steps"
    counter_path = tmp_path / "exec_count.txt"
    counted_step = steps_dir / "zzz_counted.py"
    counted_step.write_text(
        f'''
from behave import then

with open(r"{counter_path}", "a") as _f:
    _f.write("x")


@then("counted canary step ran")
def _counted_canary(context):
    pass
'''
    )

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output
    assert counter_path.read_text() == "x"  # behave's own native initial load

    # `_reload_steps`'s own mtime tracking only starts on its first call, so
    # this first retry (no edit made) still does one real load per file --
    # that's the baseline "x" -> "xx". From here on, an untouched file must
    # never be re-exec'd again.
    result = runner.invoke(debug_cmd.app, ["retry", "PROJ-1"])
    assert result.exit_code == 0, result.output
    assert counter_path.read_text() == "xx"

    # No edit at all since the last reload -- must not re-exec the counted file.
    result = runner.invoke(debug_cmd.app, ["retry", "PROJ-1"])
    assert result.exit_code == 0, result.output
    assert counter_path.read_text() == "xx"

    result = runner.invoke(debug_cmd.app, ["next", "PROJ-1"])
    assert result.exit_code == 0, result.output
    assert counter_path.read_text() == "xx"  # still unchanged, no edit happened

    # Now genuinely edit a DIFFERENT file -- the counted file still didn't
    # change and must still be skipped.
    _time.sleep(1.1)
    (steps_dir / "steps.py").write_text(
        STEPS.replace('@when("the first real step runs")', '@when("the first real step runs")')
    )
    result = runner.invoke(debug_cmd.app, ["run-text", "Then counted canary step ran", "PROJ-1"])
    assert result.exit_code == 0, result.output
    assert counter_path.read_text() == "xx"  # unrelated file's edit, still skipped

    # Finally touch the counted file itself -- it must reload exactly once.
    _time.sleep(1.1)
    counted_step.write_text(counted_step.read_text())
    result = runner.invoke(debug_cmd.app, ["run-text", "Then counted canary step ran", "PROJ-1"])
    assert result.exit_code == 0, result.output
    assert counter_path.read_text() == "xxx"

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_next_and_retry_include_real_captured_output_and_keyword(monkeypatch, tmp_path):
    """`next`/`retry` must surface the step's real console output (the
    "make it look like a real run" ask) -- not just a bare pass/fail JSON --
    and the Gherkin keyword, for both a passing and a failing step.
    """
    _wire(monkeypatch, tmp_path)
    steps_dir = tmp_path / "features" / "steps"
    (steps_dir / "steps.py").write_text(
        '''
from behave import given, when


@given("the setup step runs")
def _s(context):
    pass


@when("the first real step runs")
def _w(context):
    print("hello from the real step")


@when("a step that fails on purpose runs")
def _fail(context):
    print("about to fail")
    assert False, "deliberate failure"
'''
    )
    feature = tmp_path / "features" / "g.feature"
    feature.write_text(
        """Feature: gate demo

  Scenario: capture check
    Given the setup step runs
    When the first real step runs
    Then the second real step runs
"""
    )

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(debug_cmd.app, ["next", "PROJ-1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "passed"
    assert payload["keyword"] == "When"
    assert "hello from the real step" in payload["captured_output"]
    # Captured output is only printed live (pretty, stderr) on a FAILURE --
    # a passing step's own real output is still complete on stdout's JSON
    # above, just not repeated on the terminal where there's nothing to act on.
    assert "hello from the real step" not in result.stderr
    # step_index is the step that just ran (1), distinct from "index" (2,
    # the post-increment cursor / next step to run) -- and the pretty line
    # must render it as "[1/3]", not silently drop position information.
    assert payload["step_index"] == 1
    assert payload["total"] == 3
    assert payload["index"] == 2
    assert "[1/3]" in result.stderr

    result = runner.invoke(debug_cmd.app, ["retry", "PROJ-1"])
    assert result.exit_code == 1  # re-running index 2 ("Then the second...") is undefined -> fails
    payload = json.loads(result.stdout)
    assert payload["step_index"] == payload["index"] == 2
    assert payload["total"] == 3

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_print_pretty_step_truncates_huge_captured_output(capsys):
    """A step whose code logs something large (a full GraphQL query/response,
    say) must not bury the pass/fail line and the real error under
    thousands of lines in the pretty (stderr) rendering -- the raw JSON on
    stdout is untouched; only this human-facing view is capped.
    """
    huge = "x" * 5000 + "\nTHE ACTUAL ERROR IS RIGHT HERE"
    debug_cmd._print_pretty_step(
        {"step": "do a thing", "status": "failed", "captured_output": huge, "error": "boom"}
    )
    captured = capsys.readouterr()
    assert "THE ACTUAL ERROR IS RIGHT HERE" in captured.err  # tail survives
    assert "truncated" in captured.err
    assert len(captured.err) < len(huge)


def test_print_pretty_step_is_quiet_on_a_passing_step(capsys):
    """captured_output must NOT print live for a PASSING step -- real
    complaint hit live: a project that logs INFO:root:... on every action
    flooded the terminal for every single step, not just the ones that
    actually need attention. Still complete on stdout's JSON/the journal;
    this only decides what the human watching live actually sees.
    """
    debug_cmd._print_pretty_step(
        {
            "step": "do a thing",
            "status": "passed",
            "captured_output": "INFO:root:some routine log line\n",
        }
    )
    captured = capsys.readouterr()
    assert "do a thing" in captured.err
    assert "some routine log line" not in captured.err


def test_next_includes_captured_output_on_a_failing_step(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    steps_dir = tmp_path / "features" / "steps"
    (steps_dir / "steps.py").write_text(
        '''
from behave import given, when


@given("the setup step runs")
def _s(context):
    pass


@when("the first real step runs")
def _w(context):
    print("about to fail")
    assert False, "deliberate failure"
'''
    )

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(debug_cmd.app, ["next", "PROJ-1"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert "about to fail" in payload["captured_output"]
    # An AssertionError (the most common step failure) gets NO traceback at
    # all in behave's own error_message unless behave itself runs verbose --
    # this must come from the raw exception/traceback behave always stores
    # on the step regardless (Step.store_exception_context), not from
    # error_message's own verbose-gated formatting.
    assert "deliberate failure" in payload["traceback"]
    assert "steps.py" in payload["traceback"]
    assert payload["failed_at"]["file"].endswith("steps.py")
    assert payload["failed_at"]["line"] == 13
    assert payload["failed_at"]["function"] == "_w"

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_debug_start_does_not_park_on_a_hook_injected_step(monkeypatch, tmp_path):
    """G72: a before_feature hook's own context.execute_steps() call must run
    to completion, uninterrupted, before `--at 0` parks on the TARGET
    scenario's real step 0.

    Real bug found live in a target project: an automatic admin-login hook
    injects its own steps via execute_steps() in before_feature (context.
    scenario is None at that point -- Scenario.run() hasn't started yet).
    `--at 0` was parking on the FIRST such hook step, permanently
    interrupting the login mid-flight and skipping before_scenario (and
    everything it sets up) entirely, since control never returned to
    behave's own scenario loop to fire it.
    """
    _wire(monkeypatch, tmp_path)
    marker1 = tmp_path / "hook_step_1.txt"
    marker2 = tmp_path / "hook_step_2.txt"
    (tmp_path / "features" / "environment.py").write_text(
        f'''
def before_feature(context, feature):
    context.execute_steps("""
        Given the hook injected step runs
        When the hook injected second step runs
        """)
'''
    )
    (tmp_path / "features" / "steps" / "steps.py").write_text(
        STEPS
        + f'''

@given("the hook injected step runs")
def _hook1(context):
    with open(r"{marker1}", "w") as f:
        f.write("ran")


@when("the hook injected second step runs")
def _hook2(context):
    with open(r"{marker2}", "w") as f:
        f.write("ran")
'''
    )

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "0", "--timeout", "60"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)

    # Both hook-injected steps ran to completion BEFORE the park.
    assert marker1.read_text() == "ran"
    assert marker2.read_text() == "ran"

    # Parked on the target scenario's own step 0, not a hook-injected step.
    assert payload["parked_at"] == 0
    assert payload["total_steps"] == 3
    assert payload["current_step"] == "Given the setup step runs"

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_debug_start_extra_tag_makes_the_project_s_own_hook_logic_see_it(monkeypatch, tmp_path):
    """--extra-tag must add the tag onto feature.tags before before_feature
    runs, so a project's own tag-driven hook logic (e.g. `if "skip_login" in
    feature.tags`) treats it exactly as if it were physically in the file --
    without ever editing the file. Generic: aitlc itself knows nothing about
    what the tag does, only where tags live on behave's own objects.
    """
    _wire(monkeypatch, tmp_path)
    seen_tags = tmp_path / "seen_tags.txt"
    (tmp_path / "features" / "environment.py").write_text(
        f'''
def before_feature(context, feature):
    with open(r"{seen_tags}", "w") as f:
        f.write(",".join(feature.tags))
'''
    )

    result = runner.invoke(
        debug_cmd.app,
        ["start", "PROJ-1", "--at", "0", "--timeout", "60", "--extra-tag", "skip_login"],
    )
    assert result.exit_code == 0, result.output
    assert "skip_login" in seen_tags.read_text().split(",")

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_debug_start_stops_a_pre_existing_session_instead_of_orphaning_it(
    monkeypatch, tmp_path
):
    """G89: calling `start` again for a test_id that already has a session
    must stop the OLD gate process first, not just silently overwrite its
    session record -- real orphans found live: three separate `debug
    start` calls for the same test_id left three real behave processes
    running, none ever told to stop, because only `restart` used to do
    this check.
    """
    import os
    import time as _time

    from aitlc.core import debug_session

    _wire(monkeypatch, tmp_path)

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "0", "--timeout", "60"])
    assert result.exit_code == 0, result.output
    config = debug_cmd.AitlcConfig.find_and_load()
    old_pid = debug_session.load(config.root_dir, "PROJ-1").pid

    # Confirm the OLD process is genuinely alive before the second start.
    os.kill(old_pid, 0)

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "0", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    # The OLD pid must actually exit -- not just look "alive" because
    # nothing reaped it (os.kill(pid, 0) succeeds against an unreaped
    # zombie too; this test process IS the real parent, so os.waitpid is
    # the honest check -- see gate_launch.await_park_or_exit's own
    # docstring for the same lesson learned elsewhere in this codebase).
    deadline = _time.time() + 10
    old_gate_gone = False
    while _time.time() < deadline:
        wpid, _status = os.waitpid(old_pid, os.WNOHANG)
        if wpid == old_pid:
            old_gate_gone = True
            break
        _time.sleep(0.2)
    assert old_gate_gone, f"old gate (pid {old_pid}) was never told to stop"

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_reap_kills_a_real_orphan_with_no_session_record(monkeypatch, tmp_path):
    """`debug list --prune` only prunes stale SESSION RECORDS -- an orphan
    from before that record existed (a crash, a killed shell) has no
    record to prune. `reap` must find and kill it anyway, by scanning
    real `ps` output for this project's own gate invocations, and must
    never touch a PID a known session still legitimately points at.
    """
    import os
    import time as _time

    from aitlc.core import debug_session

    _wire(monkeypatch, tmp_path)

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "0", "--timeout", "60"])
    assert result.exit_code == 0, result.output
    config = debug_cmd.AitlcConfig.find_and_load()
    orphan_pid = debug_session.load(config.root_dir, "PROJ-1").pid

    # Simulate the real-world case: the session record is gone (deleted,
    # never written, whatever) but the real process is still alive.
    debug_session.clear(config.root_dir, "PROJ-1")

    result = runner.invoke(debug_cmd.app, ["reap", "--dry-run"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert orphan_pid in payload["would_kill"]

    result = runner.invoke(debug_cmd.app, ["reap"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert orphan_pid in payload["killed"]

    deadline = _time.time() + 10
    reaped = False
    while _time.time() < deadline:
        wpid, _status = os.waitpid(orphan_pid, os.WNOHANG)
        if wpid == orphan_pid:
            reaped = True
            break
        _time.sleep(0.2)
    assert reaped, f"orphan (pid {orphan_pid}) was never actually killed"


def test_reap_never_touches_a_pid_a_known_session_still_points_at(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "0", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(debug_cmd.app, ["reap", "--dry-run"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["would_kill"] == []

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_next_attaches_page_state_on_a_failing_step(monkeypatch, tmp_path):
    """A failed step's reply must carry the live page's URL + a compact
    accessibility snapshot -- answers "was there an unexpected page?" (an
    onboarding wizard, a popup) without a separate manual inspect round trip.
    """
    _wire(monkeypatch, tmp_path)
    (tmp_path / "features" / "steps" / "steps.py").write_text(
        '''
from behave import given, when


class _FakePage:
    url = "https://example.test/onboarding"

    def aria_snapshot(self):
        return "- heading \\"Welcome\\"\\n- button \\"Continue\\""


@given("the setup step runs")
def _s(context):
    context.page = _FakePage()


@when("the first real step runs")
def _w(context):
    assert False, "blocked by onboarding"
'''
    )

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(debug_cmd.app, ["next", "PROJ-1"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    page_state = payload["page_state"]
    assert page_state["url"] == "https://example.test/onboarding"
    assert "Welcome" in page_state["accessibility"]["tree"]
    assert "https://example.test/onboarding" in result.stderr
    assert "Welcome" in result.stderr

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_next_omits_page_state_on_a_passing_step(monkeypatch, tmp_path):
    """No noise on the common case: a passing step's reply has no page_state."""
    _wire(monkeypatch, tmp_path)

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(debug_cmd.app, ["next", "PROJ-1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "passed"
    assert "page_state" not in payload

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_run_line_runs_a_step_by_file_line_without_advancing_cursor(monkeypatch, tmp_path):
    """`run-line` runs the real, file-bound step at that line -- no need to
    retype its text -- and does not touch the debug cursor, same contract
    as `run-text`.
    """
    _wire(monkeypatch, tmp_path)
    feature_path = tmp_path / "features" / "g.feature"
    lines = feature_path.read_text().splitlines()
    # FEATURE:
    # 1  Feature: gate demo
    # 2  (blank)
    # 3    Scenario: three steps
    # 4      Given the setup step runs
    # 5      When the first real step runs
    # 6      Then the second real step runs
    then_line = next(i for i, l in enumerate(lines, start=1) if "the second real step runs" in l)

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    # Run the LATER "Then" step by line number, without having advanced there.
    result = runner.invoke(debug_cmd.app, ["run-line", str(then_line), "PROJ-1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "passed"
    assert payload["step"] == "Then the second real step runs"

    # Cursor untouched: status still shows the ORIGINAL current step.
    result = runner.invoke(debug_cmd.app, ["status", "PROJ-1"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["current_step"] == "When the first real step runs"

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_run_line_reports_an_error_for_an_out_of_range_line(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(debug_cmd.app, ["run-line", "1", "PROJ-1"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert "error" in payload

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_jump_moves_the_cursor_without_running_anything(monkeypatch, tmp_path):
    """`jump` must move the cursor to the target line's step and run
    NOTHING -- the opposite contract of `run-line` (runs a step, cursor
    untouched). Both directions: forward past unrun steps, and back to an
    earlier one, since a human may have driven the browser either way.
    """
    _wire(monkeypatch, tmp_path)
    feature_path = tmp_path / "features" / "g.feature"
    lines = feature_path.read_text().splitlines()
    then_line = next(i for i, l in enumerate(lines, start=1) if "the second real step runs" in l)
    given_line = next(i for i, l in enumerate(lines, start=1) if "the setup step runs" in l)

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "0", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    # Forward: jump from step 0 straight to step 2, skipping step 1 entirely.
    result = runner.invoke(debug_cmd.app, ["jump", str(then_line), "PROJ-1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["jumped_to"] == 2
    assert payload["current_step"] == "Then the second real step runs"

    result = runner.invoke(debug_cmd.app, ["status", "PROJ-1"])
    assert json.loads(result.stdout)["parked_at"] == 2

    # Backward: jump back to step 0.
    result = runner.invoke(debug_cmd.app, ["jump", str(given_line), "PROJ-1"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["jumped_to"] == 0

    result = runner.invoke(debug_cmd.app, ["status", "PROJ-1"])
    assert json.loads(result.stdout)["parked_at"] == 0

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_continue_from_jumps_then_runs_only_from_there(monkeypatch, tmp_path):
    """`continue --from <line>` must jump the cursor first, then run only
    from that point on -- the two earlier steps are never executed.
    """
    _wire(monkeypatch, tmp_path)
    feature_path = tmp_path / "features" / "g.feature"
    lines = feature_path.read_text().splitlines()
    then_line = next(i for i, l in enumerate(lines, start=1) if "the second real step runs" in l)

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "0", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(debug_cmd.app, ["continue", "PROJ-1", "--from", str(then_line)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["steps_run"] == 1
    assert payload["results"][0]["step"] == "Then the second real step runs"

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_debug_screenshot_resolves_cdp_url_from_the_session(monkeypatch, tmp_path):
    """`debug screenshot` must use the SESSION's own cdp_url, not a port
    the caller has to look up and type -- the same friction `eval`/
    `run-text` already avoid.
    """
    _wire(monkeypatch, tmp_path)
    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output
    session_cdp_url = json.loads(result.stdout)["cdp_url"]

    calls = []

    class _FakeResult:
        screenshot_path = tmp_path / "shot.png"

        def to_dict(self):
            return {"screenshot_path": str(self.screenshot_path)}

    def fake_inspect(cdp_url, **kwargs):
        calls.append((cdp_url, kwargs))
        return _FakeResult()

    monkeypatch.setattr(debug_cmd, "cdp_inspect", fake_inspect)

    result = runner.invoke(debug_cmd.app, ["screenshot", "PROJ-1"])
    assert result.exit_code == 0, result.output
    assert calls[0][0] == session_cdp_url
    assert "screenshot_path" in calls[0][1]

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_debug_inspect_resolves_cdp_url_from_the_session(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output
    session_cdp_url = json.loads(result.stdout)["cdp_url"]

    calls = []

    class _FakeResult:
        def to_dict(self):
            return {"accessibility": {"tree": "fake"}}

    def fake_inspect(cdp_url, **kwargs):
        calls.append((cdp_url, kwargs))
        return _FakeResult()

    monkeypatch.setattr(debug_cmd, "cdp_inspect", fake_inspect)

    result = runner.invoke(debug_cmd.app, ["inspect", "PROJ-1", "--a11y-query", "Continue"])
    assert result.exit_code == 0, result.output
    assert calls[0][0] == session_cdp_url
    assert calls[0][1]["accessibility"] is True
    assert calls[0][1]["a11y_query"] == "Continue"

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_debug_next_and_continue_journal_like_a_plain_run_does(monkeypatch, tmp_path):
    """A debug session must leave the same durable trace plain `run` already
    does -- before this, `aitlc journal list` showed every `run` but nothing
    from a whole `debug` session, however long, ever ran.
    """
    from aitlc.core import journal

    _wire(monkeypatch, tmp_path)

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    runner.invoke(debug_cmd.app, ["next", "PROJ-1"])
    runner.invoke(debug_cmd.app, ["continue", "PROJ-1"])

    entries = journal.entries(tmp_path)
    commands = [e.command for e in entries]
    assert "debug next" in commands
    assert "debug continue" in commands
    next_entry = next(e for e in entries if e.command == "debug next")
    assert next_entry.payload["step"]
    assert "debug" in next_entry.tags and "PROJ-1" in next_entry.tags

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_debug_list_shows_every_tracked_session(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    monkeypatch.setattr(
        debug_cmd.chrome_cdp,
        "list_instances",
        lambda *_a, **_k: [{"port": 9999, "running": True}],
    )

    result = runner.invoke(debug_cmd.app, ["list"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    row = payload["sessions"][0]
    assert row["test_id"] == "PROJ-1"
    assert row["port"] == 9999
    assert row["browser_alive"] is True

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_debug_list_prune_drops_only_the_unreachable_session(monkeypatch, tmp_path):
    """`--prune` must remove a session whose gate is gone, and leave a
    genuinely live one (and its bookkeeping) untouched -- it can't tell an
    abandoned session from one the user just hasn't finished with yet, so
    it only ever acts on sessions it can prove are dead.
    """
    from aitlc.core import debug_session

    _wire(monkeypatch, tmp_path)

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    config = debug_cmd.AitlcConfig.find_and_load()
    dead = debug_session.DebugSession(
        test_id="PROJ-DEAD",
        feature="features/g.feature",
        cdp_url="http://127.0.0.1:1",
        port=1,
        socket=str(tmp_path / "no-such-gate.sock"),
    )
    debug_session.save(config.root_dir, dead)

    result = runner.invoke(debug_cmd.app, ["list", "--prune"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["pruned"] == ["PROJ-DEAD"]
    assert [s["test_id"] for s in payload["sessions"]] == ["PROJ-1"]
    assert payload["sessions"][0]["gate_alive"] is True
    assert debug_session.load(config.root_dir, "PROJ-DEAD") is None
    assert debug_session.load(config.root_dir, "PROJ-1") is not None

    runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])


def test_breakpoint_in_a_step_pauses_and_resume_continues_it(monkeypatch, tmp_path):
    """A real breakpoint() in step code must park on a separate socket
    (the main one is busy blocking on this very step's `next` reply) --
    `debug status`/`eval` work while paused, and `debug resume` lets the
    ORIGINAL call finish normally, from exactly where it stopped.
    """
    import threading
    import time as _time

    from aitlc.core import debug_session, gate_client

    _wire(monkeypatch, tmp_path)
    (tmp_path / "features" / "steps" / "steps.py").write_text(
        '''
from behave import given, when


@given("the setup step runs")
def _s(context):
    pass


@when("the first real step runs")
def _w(context):
    local_answer = 1 + 1
    breakpoint()
'''
    )

    result = runner.invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1", "--timeout", "60"])
    assert result.exit_code == 0, result.output

    holder = {}

    def _run_next():
        holder["result"] = runner.invoke(debug_cmd.app, ["next", "PROJ-1"])

    thread = threading.Thread(target=_run_next)
    thread.start()
    try:
        config = debug_cmd.AitlcConfig.find_and_load()
        session = debug_session.load(config.root_dir, "PROJ-1")
        bp_socket = session.socket + ".bp"

        deadline = _time.time() + 15
        status_reply = None
        while _time.time() < deadline:
            try:
                status_reply = gate_client.request(bp_socket, "status")
                break
            except (gate_client.GateUnavailable, OSError):
                _time.sleep(0.1)
        assert status_reply is not None, "breakpoint never paused"
        assert status_reply["paused_at"] == "breakpoint"
        assert "steps.py" in (status_reply.get("file") or "")

        pyeval_reply = gate_client.request(bp_socket, "pyeval", expr="local_answer")
        assert pyeval_reply.get("result") == 2

        eval_reply = gate_client.request(bp_socket, "eval", expr="1 + 1")
        assert eval_reply.get("error") == "no live page found on context"

        resume_reply = gate_client.request(bp_socket, "resume")
        assert resume_reply.get("resumed") is True
    finally:
        thread.join(timeout=15)
        # A behave subprocess spawned here is real, not a mock -- if an
        # assert above fails, it must still be told to stop, or it leaks
        # as an orphaned process for the rest of the test session.
        runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])
    assert not thread.is_alive(), "next never returned after resume"
    payload = json.loads(holder["result"].stdout)
    assert payload["status"] == "passed"
