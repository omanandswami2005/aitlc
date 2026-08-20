"""The gate drives a REAL behave process — no mocks, no reconstruction.

This is the differential/fidelity guard for the single-stepping engine: it
launches genuine behave with `--runner aitlc.runtime.runner:AitlcRunner`, parks
at a step, and drives status/next/retry/stop over the control socket. Because
both the setup and the stepping run through behave's own machinery, any
divergence from a real run is a real bug — which is the whole point of the
architecture (nothing is reimplemented, so nothing can silently differ).

Pure-Python steps, so no browser is needed to prove the mechanism.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from aitlc.core import gate_client


def _short_socket() -> Path:
    # A Unix socket path is capped near 104 bytes by the OS; pytest's tmp_path
    # is too long, so put the socket in the system temp dir with a short name.
    return Path(tempfile.gettempdir()) / f"aitlc-gate-{uuid.uuid4().hex[:12]}.sock"

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
'''


def _behave_bin() -> str:
    candidate = Path(sys.executable).parent / "behave"
    return str(candidate) if candidate.exists() else "behave"


def _launch(project: Path, socket_path: Path, at: str, progress: Path | None = None):
    env = {
        **os.environ,
        "AITLC_GATE": "1",
        "AITLC_GATE_SOCKET": str(socket_path),
        "AITLC_GATE_AT": at,
    }
    if progress is not None:
        env["AITLC_GATE_PROGRESS"] = str(progress)
    feature = next(project.glob("features/*.feature"))
    return subprocess.Popen(
        [
            _behave_bin(),
            f"features/{feature.name}",
            "--runner",
            "aitlc.runtime.runner:AitlcRunner",
            "--no-capture",
            "--stop",
        ],
        cwd=str(project),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_gate_single_steps_a_real_behave_run(tmp_path):
    (tmp_path / "features" / "steps").mkdir(parents=True)
    (tmp_path / "features" / "g.feature").write_text(FEATURE)
    (tmp_path / "features" / "steps" / "steps.py").write_text(STEPS)
    socket_path = _short_socket()
    progress = tmp_path / "progress.json"

    proc = _launch(tmp_path, socket_path, at="1", progress=progress)
    try:
        # Parked before step index 1 (the When); the Given already ran.
        status = gate_client.wait_until_parked(socket_path, timeout_s=60)
        assert status["index"] == 1
        assert status["total"] == 3
        assert status["current_step"] == "When the first real step runs"
        assert status["finished"] is False

        # next: run the When, advance to the Then.
        r = gate_client.request(socket_path, "next")
        assert r["status"] == "passed"
        assert r["step"] == "When the first real step runs"
        assert r["index"] == 2
        assert r["current_step"] == "Then the second real step runs"

        # retry: re-run the current step (the Then) without advancing.
        r = gate_client.request(socket_path, "retry")
        assert r["status"] == "passed"
        assert r["step"] == "Then the second real step runs"
        assert r["index"] == 2  # unchanged

        # next: run the Then, reach the end.
        r = gate_client.request(socket_path, "next")
        assert r["status"] == "passed"
        assert r["index"] == 3
        assert r["finished"] is True

        # A further next has nothing to run.
        r = gate_client.request(socket_path, "next")
        assert r["finished"] is True

        gate_client.request(socket_path, "stop")
    finally:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    # The gate hard-exits (0) so behave teardown never runs.
    assert proc.returncode == 0


def test_pause_on_failure_halts_before_teardown(tmp_path):
    """aitlc's own pause (observe mode) freezes the run before teardown.

    A failing step with AITLC_PAUSE_ON_FAILURE set must os._exit before behave
    runs after_scenario/after_all -- otherwise a suite's teardown closes the
    browser and the failure is gone. Proven without a browser: an after_all
    hook writes a marker file, and the marker must NOT appear.
    """
    (tmp_path / "features" / "steps").mkdir(parents=True)
    (tmp_path / "features" / "f.feature").write_text(
        "Feature: f\n\n  Scenario: s\n    Given a passing step\n    Then a failing step\n"
    )
    (tmp_path / "features" / "steps" / "steps.py").write_text(
        "from behave import given, then\n\n"
        "@given('a passing step')\n"
        "def _g(context):\n    pass\n\n"
        "@then('a failing step')\n"
        "def _t(context):\n    assert False, 'boom'\n"
    )
    marker = tmp_path / "teardown_ran.txt"
    (tmp_path / "features" / "environment.py").write_text(
        f"def after_all(context):\n    open({str(marker)!r}, 'w').write('x')\n"
    )

    env = {**os.environ, "AITLC_PAUSE_ON_FAILURE": "1"}
    proc = subprocess.run(
        [_behave_bin(), "features/f.feature", "--runner",
         "aitlc.runtime.runner:AitlcRunner", "--no-capture"],
        cwd=str(tmp_path), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    assert proc.returncode == 1          # os._exit(1) from the pause
    assert not marker.exists()           # teardown never ran -- failure frozen


def test_gate_reports_a_failed_step(tmp_path):
    (tmp_path / "features" / "steps").mkdir(parents=True)
    (tmp_path / "features" / "f.feature").write_text(
        "Feature: f\n\n  Scenario: s\n    Given a passing step\n    Then a failing step\n"
    )
    (tmp_path / "features" / "steps" / "steps.py").write_text(
        "from behave import given, then\n\n"
        "@given('a passing step')\n"
        "def _g(context):\n    pass\n\n"
        "@then('a failing step')\n"
        "def _t(context):\n    assert False, 'boom'\n"
    )
    socket_path = _short_socket()

    proc = _launch(tmp_path, socket_path, at="1")
    try:
        gate_client.wait_until_parked(socket_path, timeout_s=60)
        r = gate_client.request(socket_path, "next")
        assert r["status"] == "failed"
        assert "boom" in (r["error"] or "")
        gate_client.request(socket_path, "stop")
    finally:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
