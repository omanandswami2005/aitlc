"""G75: a plain `aitlc run` reusing a CDP browser must warn when that
instance has been driven before -- `is_dirty_for` (used by `run --debug`)
only trips on a DIFFERENT driver, so the exact case hit live -- the SAME
test_id reusing its own browser across repeated attempts, still logged in
from the last run, with no `@skip_login` -- never tripped it. Any prior
`driven_count > 0` is worth a heads-up before the run pays for setup that's
likely to fail at its own login step, not after.
"""

from pathlib import Path

import pytest
from aitlc.cli import app
from aitlc.core import behave_runner, chrome_cdp
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


def _stub_behave_run(monkeypatch):
    passing_result = behave_runner.RunResult(steps_by_status={"passed": 1}, exit_code=0)
    monkeypatch.setattr("aitlc.commands.run.behave_runner.run", lambda *a, **k: passing_result)


def test_plain_run_warns_when_reusing_a_previously_driven_instance(project, monkeypatch):
    _stub_behave_run(monkeypatch)
    instance = chrome_cdp.CdpInstance(
        pid=1234,
        port=9333,
        user_data_dir=str(project / "reports" / ".cdp" / "profile-9333"),
        started_at=0.0,
        last_driven_by="PROJ-1",
        driven_count=3,
    )
    chrome_cdp.save_state(project, instance)
    monkeypatch.setattr(
        "aitlc.commands.run.chrome_cdp.resolve_live_cdp_url",
        lambda *_a, **_k: "http://127.0.0.1:9333",
    )

    result = runner.invoke(app, ["run", "PROJ-1", "--cdp", "--no-lock", "--no-status"])

    assert result.exit_code == 0, result.stdout
    assert "port 9333 has been driven 3 time(s) before" in result.stderr
    assert "PROJ-1" in result.stderr


def test_plain_run_is_quiet_for_a_never_driven_instance(project, monkeypatch):
    _stub_behave_run(monkeypatch)
    instance = chrome_cdp.CdpInstance(
        pid=1234,
        port=9333,
        user_data_dir=str(project / "reports" / ".cdp" / "profile-9333"),
        started_at=0.0,
    )
    chrome_cdp.save_state(project, instance)
    monkeypatch.setattr(
        "aitlc.commands.run.chrome_cdp.resolve_live_cdp_url",
        lambda *_a, **_k: "http://127.0.0.1:9333",
    )

    result = runner.invoke(app, ["run", "PROJ-1", "--cdp", "--no-lock", "--no-status"])

    assert result.exit_code == 0, result.stdout
    assert "has been driven" not in result.stderr
