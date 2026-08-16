import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aitlc.adapters.s3.evidence import S3ObjectMatch
from aitlc.cli import app
from typer.testing import CliRunner

runner = CliRunner()

FIXTURE_HTML = """
<div class="dashboard">
    <div class="metric-card">
       <h2>Overall Statistics</h2>
       <p>Pass Rate: <span class="stats-value">100.0%</span></p>
       <p>Total Features: <span class="stats-value">1</span></p>
       <p>Total Scenarios: <span class="stats-value">1</span></p>
    </div>
    <div class="metric-card">
       <h2>Feature Statistics</h2>
       <p>Passed: <span class="stats-value">1</span></p>
       <p>Failed: <span class="stats-value">0</span></p>
       <p>Skipped: <span class="stats-value">0</span></p>
    </div>
    <div class="metric-card">
       <h2>Scenario Statistics</h2>
       <p>Passed: <span class="stats-value">1</span></p>
       <p>Failed: <span class="stats-value">0</span></p>
       <p>Skipped: <span class="stats-value">0</span></p>
    </div>
</div>
<div class="chart-container"></div>
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "aitlc.toml").write_text(
        '[project]\nname = "t"\n'
        '[env]\ns3_access_key_id = "S3_AK"\ns3_secret_access_key = "S3_SK"\n'
        '[s3]\nbucket = "my-bucket"\nreport_prefix = "myproject_automation"\n'
    )
    monkeypatch.setenv("S3_AK", "ak")
    monkeypatch.setenv("S3_SK", "sk")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_report_summary_parses_and_prints_compact_json(project: Path):
    with patch(
        "aitlc.commands.s3_cmd.s3_evidence.build_client", return_value=MagicMock()
    ), patch(
        "aitlc.commands.s3_cmd.s3_evidence.find_objects",
        return_value=[
            S3ObjectMatch(
                key="myproject_automation/Stage/report.html", last_modified_epoch=2.0
            )
        ],
    ), patch(
        "aitlc.commands.s3_cmd.s3_evidence.fetch_object_bytes",
        return_value=FIXTURE_HTML.encode("utf-8"),
    ):
        result = runner.invoke(app, ["s3", "report-summary"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["total_scenarios"] == 1
    assert payload["s3_key"] == "myproject_automation/Stage/report.html"
    assert payload["raw_size_bytes"] == len(FIXTURE_HTML.encode("utf-8"))


def test_report_summary_exit_code_reflects_scenario_failures(project: Path):
    failing_html = FIXTURE_HTML.replace(
        '<p>Failed: <span class="stats-value">0</span></p>\n       <p>Skipped: <span class="stats-value">0</span></p>\n    </div>\n</div>',
        '<p>Failed: <span class="stats-value">1</span></p>\n       <p>Skipped: <span class="stats-value">0</span></p>\n    </div>\n</div>',
    )
    with patch(
        "aitlc.commands.s3_cmd.s3_evidence.build_client", return_value=MagicMock()
    ), patch(
        "aitlc.commands.s3_cmd.s3_evidence.find_objects",
        return_value=[S3ObjectMatch(key="k", last_modified_epoch=1.0)],
    ), patch(
        "aitlc.commands.s3_cmd.s3_evidence.fetch_object_bytes",
        return_value=failing_html.encode("utf-8"),
    ):
        result = runner.invoke(app, ["s3", "report-summary"])
    assert result.exit_code == 1


def test_report_summary_uses_explicit_key_without_listing(project: Path):
    with patch(
        "aitlc.commands.s3_cmd.s3_evidence.build_client", return_value=MagicMock()
    ), patch("aitlc.commands.s3_cmd.s3_evidence.find_objects") as mock_find, patch(
        "aitlc.commands.s3_cmd.s3_evidence.fetch_object_bytes",
        return_value=FIXTURE_HTML.encode("utf-8"),
    ):
        result = runner.invoke(app, ["s3", "report-summary", "--key", "exact/key.html"])
    assert result.exit_code == 0, result.stdout
    mock_find.assert_not_called()
    payload = json.loads(result.stdout)
    assert payload["s3_key"] == "exact/key.html"


def test_report_summary_no_match_found(project: Path):
    with patch(
        "aitlc.commands.s3_cmd.s3_evidence.build_client", return_value=MagicMock()
    ), patch("aitlc.commands.s3_cmd.s3_evidence.find_objects", return_value=[]):
        result = runner.invoke(app, ["s3", "report-summary"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["found"] is False


def test_report_summary_saves_raw_when_requested(project: Path):
    out_path = project / "raw.html"
    with patch(
        "aitlc.commands.s3_cmd.s3_evidence.build_client", return_value=MagicMock()
    ), patch(
        "aitlc.commands.s3_cmd.s3_evidence.find_objects",
        return_value=[S3ObjectMatch(key="k", last_modified_epoch=1.0)],
    ), patch(
        "aitlc.commands.s3_cmd.s3_evidence.fetch_object_bytes",
        return_value=FIXTURE_HTML.encode("utf-8"),
    ):
        result = runner.invoke(
            app, ["s3", "report-summary", "--save-raw", str(out_path)]
        )
    assert result.exit_code == 0, result.stdout
    assert out_path.read_text() == FIXTURE_HTML
