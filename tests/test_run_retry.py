import json
from pathlib import Path

import pytest
from aitlc.cli import app
from aitlc.core import behave_runner
from typer.testing import CliRunner

runner = CliRunner()

PATTERNS_YAML = """
patterns:
  - id: known-flake
    description: "A known flake"
    match:
      error_contains: ["flaky error"]
    suggested_action: "retry"
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "aitlc.toml").write_text(
        '[project]\nname = "t"\nfeature_dir = "features"\n'
    )
    (tmp_path / "features").mkdir()
    (tmp_path / "features" / "PROJ-1.feature").write_text("Feature: X\n")
    (tmp_path / "patterns.yaml").write_text(PATTERNS_YAML)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _failed_result(error: str) -> behave_runner.RunResult:
    return behave_runner.RunResult(
        steps_by_status={"failed": 1},
        failures=[behave_runner.StepFailure(scenario="S", step="step", error=error)],
        exit_code=1,
    )


def _failed_result_with_no_parsed_failures() -> behave_runner.RunResult:
    # Real case hit building this: behave's own JSON formatter can crash
    # handling a hook error, leaving the report with no parseable step
    # data — exit_code is still non-zero (failed), but failures is empty.
    return behave_runner.RunResult(steps_by_status={}, failures=[], exit_code=1)


def _passed_result() -> behave_runner.RunResult:
    return behave_runner.RunResult(
        steps_by_status={"passed": 1}, failures=[], exit_code=0
    )


def test_retries_known_flake_until_it_passes(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    calls = [_failed_result("flaky error"), _passed_result()]

    def fake_run(*args, **kwargs):
        return calls.pop(0)

    monkeypatch.setattr(behave_runner, "run", fake_run)

    result = runner.invoke(
        app,
        ["run", "PROJ-1", "--retry", "2", "--retry-only-if-known-flake", "--no-lock"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["retry"]["attempts"] == 2
    assert payload["retry"]["retried"] is True


def test_stops_immediately_on_unmatched_failure(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    calls = [_failed_result("a totally novel error"), _passed_result()]

    def fake_run(*args, **kwargs):
        return calls.pop(0)

    monkeypatch.setattr(behave_runner, "run", fake_run)

    result = runner.invoke(
        app,
        ["run", "PROJ-1", "--retry", "2", "--retry-only-if-known-flake", "--no-lock"],
    )
    assert result.exit_code == 1  # still failed — did not retry
    payload = json.loads(result.stdout)
    assert payload["retry"]["retried"] is False
    assert "unmatched" in payload["retry"]["stopped_reason"]
    # the mocked second (passing) result should never have been consumed
    assert len(calls) == 1


def test_does_not_vacuously_retry_a_failure_with_no_parsed_failures(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    calls = [_failed_result_with_no_parsed_failures(), _passed_result()]

    def fake_run(*args, **kwargs):
        return calls.pop(0)

    monkeypatch.setattr(behave_runner, "run", fake_run)

    result = runner.invoke(
        app,
        ["run", "PROJ-1", "--retry", "2", "--retry-only-if-known-flake", "--no-lock"],
    )
    assert result.exit_code == 1  # did not retry into the passing second result
    payload = json.loads(result.stdout)
    assert payload["retry"]["retried"] is False
    assert len(calls) == 1  # the mocked passing result was never consumed


def test_plain_retry_without_flake_check_retries_regardless(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    calls = [_failed_result("anything"), _passed_result()]

    def fake_run(*args, **kwargs):
        return calls.pop(0)

    monkeypatch.setattr(behave_runner, "run", fake_run)

    result = runner.invoke(app, ["run", "PROJ-1", "--retry", "2", "--no-lock"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["retry"]["retried"] is True


def test_no_retry_flag_means_single_attempt(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    calls = [_failed_result("anything")]

    def fake_run(*args, **kwargs):
        return calls.pop(0)

    monkeypatch.setattr(behave_runner, "run", fake_run)

    result = runner.invoke(app, ["run", "PROJ-1", "--no-lock"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert "retry" not in payload
