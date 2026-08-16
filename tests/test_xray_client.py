from unittest.mock import MagicMock, patch

import pytest
from aitlc.adapters.xray.client import XrayClient, XrayError


def _client() -> XrayClient:
    return XrayClient(
        graphql_url="https://example.com/graphql", token="tok"
    )  # nosec B106 - dummy token for a stubbed client


def _mock_response(json_data: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    return resp


def test_get_test_executions_for_test_parses_results():
    client = _client()
    payload = {
        "data": {
            "getTests": {
                "results": [
                    {
                        "testExecutions": {
                            "results": [
                                {"issueId": "1", "jira": {"key": "PROJ-EXEC-1"}},
                                {"issueId": "2", "jira": {"key": "PROJ-EXEC-2"}},
                            ]
                        }
                    }
                ]
            }
        }
    }
    with patch(
        "aitlc.adapters.xray.client.requests.post", return_value=_mock_response(payload)
    ):
        result = client.get_test_executions_for_test("PROJ-1")
    assert [e.key for e in result] == ["PROJ-EXEC-1", "PROJ-EXEC-2"]


def test_get_test_executions_for_test_raises_when_test_not_found():
    client = _client()
    payload = {"data": {"getTests": {"results": []}}}
    with patch(
        "aitlc.adapters.xray.client.requests.post", return_value=_mock_response(payload)
    ):
        with pytest.raises(XrayError, match="No Xray Test found"):
            client.get_test_executions_for_test("PROJ-nonexistent")


def test_get_tests_for_execution_parses_results():
    client = _client()
    payload = {
        "data": {
            "getTestExecutions": {
                "results": [
                    {
                        "tests": {
                            "results": [
                                {
                                    "issueId": "1",
                                    "gherkin": "When x",
                                    "jira": {"key": "PROJ-1"},
                                },
                            ]
                        }
                    }
                ]
            }
        }
    }
    with patch(
        "aitlc.adapters.xray.client.requests.post", return_value=_mock_response(payload)
    ):
        result = client.get_tests_for_execution("PROJ-EXEC-1")
    assert result[0].key == "PROJ-1"
    assert result[0].gherkin == "When x"


def test_get_test_runs_for_execution_parses_status():
    client = _client()
    payload = {
        "data": {
            "getTestExecutions": {
                "results": [
                    {
                        "testRuns": {
                            "results": [
                                {
                                    "id": "run-1",
                                    "status": {"name": "PASSED"},
                                    "test": {"jira": {"key": "PROJ-1"}},
                                    "testExecution": {"jira": {"key": "PROJ-EXEC-1"}},
                                },
                                {
                                    "id": "run-2",
                                    "status": {"name": "FAILED"},
                                    "test": {"jira": {"key": "PROJ-2"}},
                                    "testExecution": {"jira": {"key": "PROJ-EXEC-1"}},
                                },
                            ]
                        }
                    }
                ]
            }
        }
    }
    with patch(
        "aitlc.adapters.xray.client.requests.post", return_value=_mock_response(payload)
    ):
        runs = client.get_test_runs_for_execution("PROJ-EXEC-1")
    assert runs[0].status == "PASSED"
    assert runs[0].test_key == "PROJ-1"
    assert runs[1].status == "FAILED"


def test_find_step_usage_single_page():
    client = _client()
    payload = {
        "data": {
            "getTests": {
                "total": 2,
                "results": [
                    {
                        "gherkin": "When open mobile menu\nThen x",
                        "jira": {"key": "PROJ-1"},
                    },
                    {"gherkin": "When something else", "jira": {"key": "PROJ-2"}},
                ],
            }
        }
    }
    with patch(
        "aitlc.adapters.xray.client.requests.post", return_value=_mock_response(payload)
    ):
        matches = client.find_step_usage("project = PROJ", "open mobile menu")
    assert matches == ["PROJ-1"]


def test_find_step_usage_paginates_and_reports_progress():
    client = _client()

    def make_page(total, results):
        return _mock_response(
            {"data": {"getTests": {"total": total, "results": results}}}
        )

    # Page 1 (start=0): total=150, 100 results. Page 2 (start=100): 50 more.
    page1_results = [
        {"gherkin": "no match", "jira": {"key": f"PROJ-{i}"}} for i in range(100)
    ]
    page2_results = [
        {"gherkin": "no match", "jira": {"key": f"PROJ-{100 + i}"}} for i in range(49)
    ]
    page2_results.append(
        {"gherkin": "has step_text_marker here", "jira": {"key": "PROJ-149"}}
    )

    responses = [make_page(150, page1_results), make_page(150, page2_results)]

    progress_calls = []

    def fake_post(*args, **kwargs):
        return responses.pop(0)

    with patch("aitlc.adapters.xray.client.requests.post", side_effect=fake_post):
        matches = client.find_step_usage(
            "project = PROJ",
            "step_text_marker",
            page_size=100,
            max_workers=1,
            progress_callback=lambda f, t: progress_calls.append((f, t)),
        )
    assert matches == ["PROJ-149"]
    assert progress_calls == [(100, 150), (150, 150)]


def test_link_to_execution_uses_real_issue_ids():
    client = _client()
    call_log = []

    def fake_post(*args, **kwargs):
        body = kwargs.get("json", {})
        call_log.append(body)
        if "AddToExecution" in body.get("query", ""):
            return _mock_response(
                {"data": {"addTestsToTestExecution": {"addedTests": ["1"]}}}
            )
        # get_gherkin calls (used internally to resolve issueIds)
        key = body["variables"]["jql"].split("= ")[-1]
        return _mock_response(
            {
                "data": {
                    "getTests": {"results": [{"issueId": f"id-{key}", "gherkin": "x"}]}
                }
            }
        )

    with patch("aitlc.adapters.xray.client.requests.post", side_effect=fake_post):
        client.link_to_execution("PROJ-1", "PROJ-EXEC-1")

    add_call = next(c for c in call_log if "AddToExecution" in c.get("query", ""))
    assert add_call["variables"]["executionIssueId"] == "id-PROJ-EXEC-1"
    assert add_call["variables"]["testIssueIds"] == ["id-PROJ-1"]
