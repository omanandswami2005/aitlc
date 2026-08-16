"""Credentialed sub-Typer command groups must load .env before running.

The same way `run`/`report`/`notify-teams`/`start`/`steps`
already did. Real bug found live: `xray`/`s3`/`jira`/`tunnel`/`trace`
never called load_dotenv at all, so a command only worked if its
credentials happened to already be in the shell's real environment —
`aitlc.toml`'s own `--env-file .env` convention was silently ignored.
"""

from unittest.mock import MagicMock, patch

import pytest
from aitlc.cli import app
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def project_with_env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "aitlc.toml").write_text(
        '[project]\nname = "t"\n'
        '[env]\njira_xray_client_id = "TEST_XRAY_CID"\n'
        'jira_xray_client_secret = "TEST_XRAY_CSECRET"\n'
    )
    (tmp_path / ".env").write_text(
        "TEST_XRAY_CID=cid-from-dotenv\nTEST_XRAY_CSECRET=secret-from-dotenv\n"
    )
    monkeypatch.delenv("TEST_XRAY_CID", raising=False)
    monkeypatch.delenv("TEST_XRAY_CSECRET", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_xray_group_loads_dotenv_before_subcommand_runs(project_with_env):
    fake_client = MagicMock()
    fake_client.get_gherkin.return_value = MagicMock(
        key="PROJ-1", issue_id="1", gherkin="When x"
    )
    with patch(
        "aitlc.commands.xray_cmd.XrayClient.from_client_credentials",
        return_value=fake_client,
    ) as mock_from_creds:
        result = runner.invoke(app, ["xray", "get-gherkin", "PROJ-1"])

    assert result.exit_code == 0, result.stdout
    mock_from_creds.assert_called_once()
    assert mock_from_creds.call_args.kwargs["client_id"] == "cid-from-dotenv"
    assert mock_from_creds.call_args.kwargs["client_secret"] == "secret-from-dotenv"


def test_xray_group_respects_custom_env_file(tmp_path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "aitlc.toml").write_text(
        '[project]\nname = "t"\n'
        '[env]\njira_xray_client_id = "TEST_XRAY_CID2"\n'
        'jira_xray_client_secret = "TEST_XRAY_CSECRET2"\n'
    )
    (tmp_path / "custom.env").write_text(
        "TEST_XRAY_CID2=custom-cid\nTEST_XRAY_CSECRET2=custom-secret\n"
    )
    monkeypatch.delenv("TEST_XRAY_CID2", raising=False)
    monkeypatch.delenv("TEST_XRAY_CSECRET2", raising=False)
    monkeypatch.chdir(tmp_path)

    fake_client = MagicMock()
    fake_client.get_gherkin.return_value = MagicMock(
        key="PROJ-1", issue_id="1", gherkin="x"
    )
    with patch(
        "aitlc.commands.xray_cmd.XrayClient.from_client_credentials",
        return_value=fake_client,
    ) as mock_from_creds:
        result = runner.invoke(
            app, ["xray", "--env-file", "custom.env", "get-gherkin", "PROJ-1"]
        )

    assert result.exit_code == 0, result.stdout
    assert mock_from_creds.call_args.kwargs["client_id"] == "custom-cid"
