"""Regression: _resolve_platform_environment must pass env to its subprocess.

Found live: that internal subprocess ran with only the parent process's
inherited os.environ,
which doesn't yet have TESTING_PLATFORM/TEST_TYPE/DEVICE_NAME set at that
point in `aitlc run --remote` — those only get merged in for the LATER
behave subprocess. This project's own LambdaTestConfig capability builder
branches on those vars, so the wrong capabilities got built and LT rejected
the session outright. Fixed by passing `remote_env` into the subprocess's
own `env=`.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from aitlc.cli import app
from aitlc.core import behave_runner
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def remote_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "aitlc.toml").write_text(
        '[project]\nname = "t"\nfeature_dir = "features"\n'
        '[lambdatest]\ntunnel_name = "t-tunnel"\n'
        'platform_environment_command = "echo capabilities-for-{feature_name}"\n'
    )
    (tmp_path / "features").mkdir()
    (tmp_path / "features" / "PROJ-1.feature").write_text("Feature: X\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_platform_environment_subprocess_receives_remote_env(remote_project: Path):
    captured_env = {}
    real_subprocess_run = __import__("subprocess").run

    def spy_run(*args, **kwargs):
        if "env" in kwargs and kwargs.get("shell"):
            captured_env.update(kwargs["env"])
        return real_subprocess_run(*args, **kwargs)

    passing_result = behave_runner.RunResult(steps_by_status={"passed": 1}, exit_code=0)

    with patch("aitlc.commands.run.subprocess.run", side_effect=spy_run), patch(
        "aitlc.commands.run.behave_runner.run", return_value=passing_result
    ):
        result = runner.invoke(
            app, ["run", "PROJ-1", "--remote", "--no-lock", "--no-status"]
        )

    assert result.exit_code == 0, result.stdout
    # The exact real bug: these three must be visible to the
    # platform_environment_command subprocess, not just the later behave one.
    assert captured_env.get("TESTING_PLATFORM") == "LAMBDATEST"
    assert captured_env.get("TEST_TYPE") == "mobile_browser"
    assert captured_env.get("LT_TUNNEL_NAME") == "t-tunnel"


def test_platform_environment_output_is_passed_to_behave_run(remote_project: Path):
    passing_result = behave_runner.RunResult(steps_by_status={"passed": 1}, exit_code=0)
    with patch(
        "aitlc.commands.run.behave_runner.run", return_value=passing_result
    ) as mock_behave_run:
        result = runner.invoke(
            app, ["run", "PROJ-1", "--remote", "--no-lock", "--no-status"]
        )

    assert result.exit_code == 0, result.stdout
    env_passed = mock_behave_run.call_args.kwargs["env"]
    assert env_passed["PLATFORM_ENVIRONMENT"] == "capabilities-for-PROJ-1"
