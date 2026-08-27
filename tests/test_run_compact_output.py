"""`aitlc run`'s final stdout JSON must not repeat every step's full record
on a clean pass -- real complaint hit live: a 37-step scenario that fully
passed still dumped the entire per-step array on stdout, redundant with
behave's own human-readable summary printed just above it in the same
output. Compact by default (`[run].output` in aitlc.toml, or `--full`/
`--compact` per call); a run with any failure keeps "steps" either way,
since the surrounding narrative matters there. The journal always keeps
the complete record regardless -- this only trims what's echoed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aitlc.cli import app
from aitlc.commands import run as run_cmd
from aitlc.core import behave_runner
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "aitlc.toml").write_text(
        '[project]\nname = "t"\nfeature_dir = "features"\n'
    )
    (tmp_path / "features").mkdir()
    (tmp_path / "features" / "PROJ-1.feature").write_text("Feature: X\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _stub(monkeypatch, result):
    monkeypatch.setattr(run_cmd.behave_runner, "run", lambda *a, **k: result)


def test_a_clean_pass_drops_the_steps_array_by_default(project, monkeypatch):
    passing = behave_runner.RunResult(
        steps_by_status={"passed": 3},
        steps=[
            {"step": "Given a", "status": "passed", "duration_s": 0.1, "error": None},
            {"step": "When b", "status": "passed", "duration_s": 0.2, "error": None},
            {"step": "Then c", "status": "passed", "duration_s": 0.1, "error": None},
        ],
        exit_code=0,
    )
    _stub(monkeypatch, passing)

    result = runner.invoke(app, ["run", "PROJ-1", "--no-lock", "--no-status"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert "steps" not in payload
    assert payload["steps_by_status"] == {"passed": 3}
    assert payload["failures"] == []


def test_a_failure_keeps_the_steps_array_even_when_compact(project, monkeypatch):
    failing = behave_runner.RunResult(
        steps_by_status={"passed": 1, "failed": 1},
        steps=[
            {"step": "Given a", "status": "passed", "duration_s": 0.1, "error": None},
            {"step": "When b", "status": "failed", "duration_s": 0.2, "error": "boom"},
        ],
        failures=[
            behave_runner.StepFailure(scenario="s", step="When b", error="boom")
        ],
        exit_code=1,
    )
    _stub(monkeypatch, failing)

    result = runner.invoke(app, ["run", "PROJ-1", "--no-lock", "--no-status"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert "steps" in payload
    assert len(payload["steps"]) == 2


def test_full_flag_keeps_the_steps_array_on_a_clean_pass(project, monkeypatch):
    passing = behave_runner.RunResult(
        steps_by_status={"passed": 1},
        steps=[{"step": "Given a", "status": "passed", "duration_s": 0.1, "error": None}],
        exit_code=0,
    )
    _stub(monkeypatch, passing)

    result = runner.invoke(
        app, ["run", "PROJ-1", "--no-lock", "--no-status", "--full"]
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert "steps" in payload


def test_run_output_full_in_toml_keeps_the_steps_array(project, monkeypatch):
    (project / "aitlc.toml").write_text(
        '[project]\nname = "t"\nfeature_dir = "features"\n\n[run]\noutput = "full"\n'
    )
    passing = behave_runner.RunResult(
        steps_by_status={"passed": 1},
        steps=[{"step": "Given a", "status": "passed", "duration_s": 0.1, "error": None}],
        exit_code=0,
    )
    _stub(monkeypatch, passing)

    result = runner.invoke(app, ["run", "PROJ-1", "--no-lock", "--no-status"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert "steps" in payload
