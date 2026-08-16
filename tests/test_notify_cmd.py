import json
from pathlib import Path
from unittest.mock import patch

import pytest
from aitlc.cli import app
from aitlc.core.behave_runner import RunResult, ScenarioResult, StepFailure
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "aitlc.toml").write_text(
        '[project]\nname = "t"\nfeature_dir = "features"\n'
    )
    features_dir = tmp_path / "features"
    features_dir.mkdir()
    (features_dir / "PROJ-1.feature").write_text(
        "Feature: x\n  Scenario: y\n    Given z\n"
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _passing_result() -> RunResult:
    return RunResult(
        steps_by_status={"passed": 1},
        scenarios=[
            ScenarioResult(feature="F", name="S", status="passed", duration_seconds=2.0)
        ],
    )


def _failing_result() -> RunResult:
    return RunResult(
        steps_by_status={"failed": 1},
        failures=[StepFailure(scenario="S", step="Then check it", error="boom")],
        scenarios=[
            ScenarioResult(feature="F", name="S", status="failed", duration_seconds=1.0)
        ],
        exit_code=1,
    )


def test_print_only_never_hits_the_network(project: Path):
    with patch(
        "aitlc.commands.notify_cmd.behave_runner.run", return_value=_passing_result()
    ):
        result = runner.invoke(app, ["notify-teams", "PROJ-1", "--print-only"])
    assert result.exit_code == 0, result.stdout
    card = json.loads(result.stdout)
    assert card["type"] == "message"


def test_exit_code_reflects_failures(project: Path):
    with patch(
        "aitlc.commands.notify_cmd.behave_runner.run", return_value=_failing_result()
    ):
        result = runner.invoke(app, ["notify-teams", "PROJ-1", "--print-only"])
    assert result.exit_code == 1


def test_missing_webhook_url_errors_cleanly(project: Path):
    with patch(
        "aitlc.commands.notify_cmd.behave_runner.run", return_value=_passing_result()
    ):
        result = runner.invoke(app, ["notify-teams", "PROJ-1"])
    assert result.exit_code == 2


def test_unresolvable_test_id_errors_cleanly(project: Path):
    result = runner.invoke(app, ["notify-teams", "PROJ-does-not-exist", "--print-only"])
    assert result.exit_code == 2
