"""G45 (run --window-size) and the debug progress-file round-trip.

The debug-session output-mode / background tests that used to live here were for
the old step-console engine; the debug engine is now the gated real-behave
runner, covered end to end in test_gate_runner.py and test_debug_gate.py.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from aitlc.commands import run as run_cmd
from aitlc.core import chrome_cdp
from aitlc.core.debug_session import read_progress, write_progress
from aitlc.cli import app as cli_app

runner = CliRunner()


class _RunCfg:
    def __init__(self, root: Path, feature: Path) -> None:
        self.root_dir = root
        self._feature = feature
        self.playwright_cdp_env = "PLAYWRIGHT_CDP_URL"
        self.scenario_setup = None
        self.step_dir = "features/steps"
        self.browser_actions = None
        self.browser_factory = None

        class _M:
            mobile_device_env_var = "DEVICE_NAME"
            mobile_device_env_value = "MOBILE_DEVICE"
            mobile_feature_title_pattern = "Mobile browser:"

        class _LT:
            tunnel_name = None
            max_concurrent_sessions = 5
            platform_environment_command = None

        self.mobile = _M()
        self.lambdatest = _LT()
        self.env = type("E", (), {"resolve": staticmethod(lambda *_a, **_k: None)})()

    def resolve_feature_path(self, _tid):
        return self._feature


def _capture_launch_window(monkeypatch, tmp_path):
    feature = tmp_path / "f.feature"
    feature.write_text("Feature: f\n\n\tScenario: s\n\tGiven a\n")
    cfg = _RunCfg(tmp_path, feature)
    monkeypatch.setattr(run_cmd.AitlcConfig, "find_and_load", staticmethod(lambda: cfg))
    monkeypatch.setattr(run_cmd, "load_dotenv", lambda *_a, **_k: True)
    monkeypatch.setattr(run_cmd.chrome_cdp, "is_dirty_for", lambda *a, **k: (False, ""))

    captured = {}

    def fake_launch(root, port=None, window_size=None):
        captured["window_size"] = window_size
        raise chrome_cdp.ChromeCdpError("stop after capture")

    monkeypatch.setattr(run_cmd.chrome_cdp, "launch", fake_launch)
    return captured


def test_run_debug_defaults_to_desktop_window(monkeypatch, tmp_path):
    captured = _capture_launch_window(monkeypatch, tmp_path)
    runner.invoke(cli_app, ["run", "PROJ-1", "--debug", "--no-lock", "--no-status"])
    assert captured["window_size"] == chrome_cdp.DESKTOP_WINDOW_SIZE


def test_run_debug_explicit_window_size_wins(monkeypatch, tmp_path):
    captured = _capture_launch_window(monkeypatch, tmp_path)
    runner.invoke(
        cli_app,
        ["run", "PROJ-1", "--debug", "--window-size", "800,600", "--no-lock", "--no-status"],
    )
    assert captured["window_size"] == "800,600"


def test_run_debug_mobile_uses_phone_window(monkeypatch, tmp_path):
    captured = _capture_launch_window(monkeypatch, tmp_path)
    runner.invoke(
        cli_app, ["run", "PROJ-1", "--debug", "--mobile", "--no-lock", "--no-status"]
    )
    assert captured["window_size"] == chrome_cdp.DEFAULT_WINDOW_SIZE


def test_progress_round_trips(tmp_path):
    write_progress(tmp_path, "PROJ-1", {"state": "running", "done": 3, "total": 10})
    got = read_progress(tmp_path, "PROJ-1")
    assert got["state"] == "running" and got["done"] == 3 and "updated_at" in got
