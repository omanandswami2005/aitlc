"""`aitlc run --extra-tag` must attach AitlcRunner and forward AITLC_EXTRA_TAGS
-- plain `run` attaches no custom runner at all by default (unlike `debug
start`/`run --debug`), so --extra-tag has to opt IN to the attach machinery
itself, not just set an env var that would silently do nothing without it.
The tag-injection mechanism itself (`AitlcRunner._inject_extra_tags`) is
verified end-to-end against a real behave subprocess in
test_debug_gate.py's `test_debug_start_extra_tag_makes_the_project_s_own_hook_logic_see_it`;
this file is the wiring: does --extra-tag actually reach attach.plan() and
the final behave_runner.run() call.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aitlc.cli import app
from aitlc.commands import run as run_cmd
from aitlc.core import behave_runner
from aitlc.runtime.attach import AttachPlan
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


def test_extra_tag_attaches_the_runner_and_forwards_the_env_var(project, monkeypatch):
    passing = behave_runner.RunResult(steps_by_status={"passed": 1}, exit_code=0)
    run_calls = []
    monkeypatch.setattr(
        run_cmd.behave_runner,
        "run",
        lambda *a, **k: run_calls.append(k) or passing,
    )
    plan_calls = []
    monkeypatch.setattr(
        run_cmd.attach,
        "plan",
        lambda *a, **k: plan_calls.append(k)
        or AttachPlan(extra_args=["--runner", "aitlc.runtime.runner:AitlcRunner"],
                      env={"AITLC_EXTRA_TAGS": "skip_login", "PYTHONPATH": "/fake"}),
    )

    result = runner.invoke(
        app, ["run", "PROJ-1", "--no-lock", "--no-status", "--extra-tag", "skip_login"]
    )

    assert result.exit_code == 0, result.stdout
    assert plan_calls, "attach.plan() was never called for --extra-tag"
    assert plan_calls[0]["gate_env"] == {"AITLC_EXTRA_TAGS": "skip_login"}
    assert run_calls
    assert run_calls[0]["extra_args"] == ["--runner", "aitlc.runtime.runner:AitlcRunner"]
    assert run_calls[0]["env"]["AITLC_EXTRA_TAGS"] == "skip_login"
    assert run_calls[0]["env"]["PYTHONPATH"] == "/fake"


def test_without_extra_tag_the_runner_is_never_attached(project, monkeypatch):
    passing = behave_runner.RunResult(steps_by_status={"passed": 1}, exit_code=0)
    run_calls = []
    monkeypatch.setattr(
        run_cmd.behave_runner,
        "run",
        lambda *a, **k: run_calls.append(k) or passing,
    )
    plan_calls = []
    monkeypatch.setattr(
        run_cmd.attach, "plan", lambda *a, **k: plan_calls.append(k) or AttachPlan()
    )

    result = runner.invoke(app, ["run", "PROJ-1", "--no-lock", "--no-status"])

    assert result.exit_code == 0, result.stdout
    assert not plan_calls, "attach.plan() must not run when --extra-tag is unused"
    assert run_calls[0]["extra_args"] is None
