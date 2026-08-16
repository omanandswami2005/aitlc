from unittest.mock import MagicMock, patch

import pytest
from aitlc.adapters.teams import webhook
from aitlc.core.behave_runner import RunResult, ScenarioResult, StepFailure


def _passing_run(name: str, duration: float) -> RunResult:
    return RunResult(
        steps_by_status={"passed": 1},
        scenarios=[
            ScenarioResult(
                feature=name, name=name, status="passed", duration_seconds=duration
            )
        ],
    )


def _failing_run(name: str, duration: float) -> RunResult:
    return RunResult(
        steps_by_status={"failed": 1},
        failures=[StepFailure(scenario=name, step="Then check it", error="boom")],
        scenarios=[
            ScenarioResult(
                feature=name, name=name, status="failed", duration_seconds=duration
            )
        ],
        exit_code=1,
    )


def test_build_summary_card_counts_and_duration():
    runs = [
        webhook.RunSummary(test_id="PROJ-1", result=_passing_run("A", 12.5)),
        webhook.RunSummary(test_id="PROJ-2", result=_failing_run("B", 4.25)),
    ]
    card = webhook.build_summary_card("nightly run", runs)

    body = card["attachments"][0]["content"]["body"]
    subject_block = body[0]
    assert subject_block["text"] == "nightly run"
    assert subject_block["color"] == "Attention"  # at least one failure

    summary_block = body[1]
    assert "1 passed, 1 failed, of 2 total" in summary_block["text"]

    table = body[2]
    # header row + 2 data rows
    assert len(table["rows"]) == 3
    failed_row = table["rows"][2]["cells"]
    assert failed_row[0]["items"][0]["text"] == "PROJ-2"
    assert failed_row[1]["items"][0]["text"] == "FAILED"
    assert failed_row[2]["items"][0]["text"] == "4.25"
    assert "boom" in failed_row[3]["items"][0]["text"]


def test_build_summary_card_includes_report_link_when_given():
    runs = [webhook.RunSummary(test_id="PROJ-1", result=_passing_run("A", 1.0))]
    card = webhook.build_summary_card(
        "run", runs, report_url="https://example.com/report.html"
    )
    body = card["attachments"][0]["content"]["body"]
    action_set = next(b for b in body if b.get("type") == "ActionSet")
    assert action_set["actions"][0]["url"] == "https://example.com/report.html"


def test_build_summary_card_no_report_link_by_default():
    runs = [webhook.RunSummary(test_id="PROJ-1", result=_passing_run("A", 1.0))]
    card = webhook.build_summary_card("run", runs)
    body = card["attachments"][0]["content"]["body"]
    assert not any(b.get("type") == "ActionSet" for b in body)


def test_post_raises_on_non_2xx():
    resp = MagicMock()
    resp.status_code = 500
    resp.text = "server error"
    with patch("aitlc.adapters.teams.webhook.requests.post", return_value=resp):
        with pytest.raises(webhook.TeamsWebhookError, match="500"):
            webhook.post("https://example.com/webhook", {"type": "message"})


def test_post_succeeds_on_202():
    resp = MagicMock()
    resp.status_code = 202
    with patch(
        "aitlc.adapters.teams.webhook.requests.post", return_value=resp
    ) as mock_post:
        webhook.post("https://example.com/webhook", {"type": "message"})
    mock_post.assert_called_once()


def test_post_raises_on_network_error():
    import requests

    with patch(
        "aitlc.adapters.teams.webhook.requests.post",
        side_effect=requests.ConnectionError("dns fail"),
    ), pytest.raises(webhook.TeamsWebhookError, match="dns fail"):
        webhook.post("https://example.com/webhook", {"type": "message"})
