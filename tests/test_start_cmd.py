import json
from pathlib import Path

import pytest
from aitlc.cli import app
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "aitlc.toml").write_text(
        '[project]\nname = "t"\nfeature_dir = "features"\n'
    )
    (tmp_path / "features").mkdir()
    (tmp_path / "features" / "PROJ-1.feature").write_text(
        "Feature: Mobile browser: Something\n\n"
        "\t@TEST_PROJ-1\n"
        "\tScenario: Something\n"
        "\t\tWhen do a thing\n"
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_creates_session_folder_with_feature_and_context(project: Path):
    result = runner.invoke(app, ["start", "PROJ-1"])
    assert result.exit_code == 0, result.stdout

    payload = json.loads(result.stdout)
    session_dir = Path(payload["session_dir"])
    assert session_dir.exists()
    assert (session_dir / "PROJ-1.feature").exists()
    assert (session_dir / "context.md").exists()


def test_context_md_contains_all_expected_sections(project: Path):
    result = runner.invoke(app, ["start", "PROJ-1"])
    payload = json.loads(result.stdout)
    content = Path(payload["context_md"]).read_text()

    assert "# PROJ-1 — Debugging Context" in content
    assert "## 1. Preflight" in content
    assert "## 2. Gherkin drift" in content
    assert "## 3. Known-flake classification" in content
    assert "## 4. Evidence" in content
    assert "## 6. When you're done" in content


def test_gracefully_degrades_without_xray_config(project: Path):
    # No [env].jira_xray_client_id/secret configured at all in this fixture's
    # aitlc.toml — must not crash, must say plainly why it's unavailable.
    result = runner.invoke(app, ["start", "PROJ-1"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    content = Path(payload["context_md"]).read_text()
    assert "Not available" in content or "not configured" in content


def test_gracefully_degrades_without_prior_report(project: Path):
    result = runner.invoke(app, ["start", "PROJ-1"])
    payload = json.loads(result.stdout)
    content = Path(payload["context_md"]).read_text()
    assert "No prior run data given" in content


def test_missing_feature_errors_cleanly(project: Path):
    result = runner.invoke(app, ["start", "PROJ-nonexistent"])
    assert result.exit_code == 2


def test_mobile_mismatch_detected_in_doctor_section(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("DEVICE_NAME", raising=False)
    result = runner.invoke(app, ["start", "PROJ-1"])
    payload = json.loads(result.stdout)
    content = Path(payload["context_md"]).read_text()
    # PROJ-1.feature's title says "Mobile browser:" — mismatch should be flagged.
    assert "DEVICE_NAME" in content
    assert "⚠️" in content
