import json
from pathlib import Path

import pytest
from aitlc.cli import app
from typer.testing import CliRunner

runner = CliRunner()

SAMPLE_DIFF = """\
diff --git a/pages/rabbitmq/rabbitmq_page.py b/pages/rabbitmq/rabbitmq_page.py
index abc..def 100644
--- a/pages/rabbitmq/rabbitmq_page.py
+++ b/pages/rabbitmq/rabbitmq_page.py
@@ -1,1 +1,1 @@
-old
+new
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "aitlc.toml").write_text(
        '[project]\nname = "t"\nstep_dir = "features/steps"\nlocators_dir = "config/web_locators"\n'
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_generates_proposal_with_diff_and_no_report(project: Path):
    diff_file = project / "fix.diff"
    diff_file.write_text(SAMPLE_DIFF)

    result = runner.invoke(
        app, ["propose-fix", "PROJ-1", "--diff", str(diff_file), "--out", "proposal.md"]
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["scoped_narrowly"] is True
    assert payload["touches_shared_dirs"] == []

    content = Path(payload["proposal"]).read_text()
    assert "This is a proposal, not an applied change" in content
    assert "pages/rabbitmq/rabbitmq_page.py" in content
    assert "No `--report` given" in content


def test_flags_shared_code_touch(project: Path):
    diff_file = project / "fix.diff"
    diff_file.write_text(
        "diff --git a/features/steps/x.py b/features/steps/x.py\n"
        "--- a/features/steps/x.py\n"
        "+++ b/features/steps/x.py\n"
        "@@ -1 +1 @@\n-a\n+b\n"
    )
    result = runner.invoke(
        app, ["propose-fix", "PROJ-1", "--diff", str(diff_file), "--out", "proposal.md"]
    )
    payload = json.loads(result.stdout)
    assert payload["scoped_narrowly"] is False
    content = Path(payload["proposal"]).read_text()
    assert "touches shared code" in content


def test_classifies_against_prior_report(project: Path):
    (project / "patterns.yaml").write_text(
        "patterns:\n"
        "  - id: known-thing\n"
        '    description: "d"\n'
        "    match:\n"
        '      error_contains: ["boom"]\n'
        '    suggested_action: "retry"\n'
    )
    report_file = project / "report.json"
    report_file.write_text(
        json.dumps({"failures": [{"step": "Then check it", "error": "boom happened"}]})
    )
    diff_file = project / "fix.diff"
    diff_file.write_text(SAMPLE_DIFF)

    result = runner.invoke(
        app,
        [
            "propose-fix",
            "PROJ-1",
            "--diff",
            str(diff_file),
            "--report",
            str(report_file),
            "--out",
            "proposal.md",
        ],
    )
    payload = json.loads(result.stdout)
    content = Path(payload["proposal"]).read_text()
    assert "known-thing" in content
