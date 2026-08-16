from unittest.mock import MagicMock, patch

import pytest
from aitlc.adapters.jira.tasks import JiraTaskError, create_task


def test_create_task_calls_jira_client_correctly():
    fake_issue = MagicMock()
    fake_issue.key = "PROJ-99999"

    with patch("aitlc.adapters.jira.tasks.JIRA") as MockJIRA:
        mock_client = MockJIRA.return_value
        mock_client.create_issue.return_value = fake_issue
        mock_client.current_user.return_value = "me@example.com"

        task = create_task(
            server_url="https://example.atlassian.net",
            email="me@example.com",
            api_token="tok",  # nosec B106 - dummy test token
            project_key="PROJ",
            summary="A task",
            description="details",
        )

    MockJIRA.assert_called_once_with(
        server="https://example.atlassian.net",
        basic_auth=("me@example.com", "tok"),
    )
    mock_client.create_issue.assert_called_once_with(
        project="PROJ",
        summary="A task",
        description="details",
        issuetype={"name": "Task"},
    )
    # FR-7.2: always assigned to the authenticating identity, no assignee param exposed.
    mock_client.assign_issue.assert_called_once_with(fake_issue, "me@example.com")
    assert task.key == "PROJ-99999"
    assert task.url == "https://example.atlassian.net/browse/PROJ-99999"


def test_server_url_trailing_slash_stripped_in_url():
    fake_issue = MagicMock()
    fake_issue.key = "PROJ-1"
    with patch("aitlc.adapters.jira.tasks.JIRA") as MockJIRA:
        mock_client = MockJIRA.return_value
        mock_client.create_issue.return_value = fake_issue
        mock_client.current_user.return_value = "me"

        task = create_task(
            server_url="https://example.atlassian.net/",
            email="me",
            api_token="tok",  # nosec B106 - dummy test token
            project_key="PROJ",
            summary="s",
        )
    assert task.url == "https://example.atlassian.net/browse/PROJ-1"


def test_jira_error_is_wrapped():
    with patch("aitlc.adapters.jira.tasks.JIRA") as MockJIRA:
        MockJIRA.return_value.create_issue.side_effect = Exception("401 Unauthorized")
        with pytest.raises(JiraTaskError, match="401 Unauthorized"):
            create_task(
                server_url="https://example.atlassian.net",
                email="me",
                api_token="bad",  # nosec B106 - dummy test token
                project_key="PROJ",
                summary="s",
            )
