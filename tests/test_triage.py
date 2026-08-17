"""Triaging a CI run from its Behave JSON.

Fixtures mirror what a real run looks like: one job covering two test plans,
each report holding one feature, and behave's captured streams appended after
the traceback.
"""

from __future__ import annotations

import json

from aitlc.core import triage


def _feature(execution: str, name: str, scenarios: list[dict]) -> dict:
    return {"name": name, "tags": [f"TEST_{execution}"], "elements": scenarios}


def _scenario(test_key: str, name: str, failing_step: dict | None) -> dict:
    steps = [
        {"keyword": "Given ", "name": "a passing step", "result": {"status": "passed"}}
    ]
    if failing_step:
        steps.append(failing_step)
    return {"name": name, "tags": [f"TEST_{test_key}"], "steps": steps}


FAILING_STEP = {
    "keyword": "Then ",
    "name": 'click on the element locator "save_as_btn"',
    "result": {
        "status": "failed",
        "error_message": [
            "playwright._impl._errors.TimeoutError: Locator.click: Timeout 30000ms exceeded.",
            "Call log:",
            '  - waiting for locator("[id=\\"saveAs\\"]").first',
            "",
            "Captured logging:",
            "INFO :: Messages Delayed: 0",
        ],
    },
}


def test_totals_and_failure_rows():
    docs = [
        (
            "plan_a.json",
            [
                _feature(
                    "PROJ-10",
                    "Plan A feature",
                    [
                        _scenario("PROJ-11", "works", None),
                        _scenario("PROJ-12", "breaks", FAILING_STEP),
                    ],
                )
            ],
        ),
        (
            "plan_b.json",
            [
                _feature(
                    "PROJ-20",
                    "Plan B feature",
                    [
                        _scenario("PROJ-21", "works", None),
                    ],
                )
            ],
        ),
    ]
    result = triage.triage_documents(docs)
    assert (result.features_passed, result.features_failed) == (1, 1)
    assert (result.scenarios_passed, result.scenarios_failed) == (2, 1)

    failure = result.failures[0]
    assert failure.test_key == "PROJ-12"
    assert failure.execution_key == "PROJ-10"
    assert "Timeout 30000ms" in failure.error
    assert "saveAs" in failure.locator


def test_the_captured_log_tail_is_never_reported_as_the_error():
    """The bug this parser exists to avoid: reporting behave's log tail."""
    error, _ = triage.extract_error(FAILING_STEP["result"]["error_message"])
    assert "Messages Delayed" not in error
    assert error.startswith("playwright")


def test_extract_error_is_honest_when_there_is_nothing():
    assert triage.extract_error(None) == ("", "")
    assert triage.extract_error("") == ("", "")


def test_run_timestamp_is_pulled_from_a_report_key():
    key = (
        "proj/behave_results/Stage/A_Plan/behave_PROJ-1_2026-08-17T04-13-46-491341.json"
    )
    assert triage.run_timestamp(key) == "2026-08-17T04-13-46-491341"
    assert triage.run_timestamp("no-timestamp-here.json") is None


def test_unparseable_files_are_skipped_not_fatal(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(
        json.dumps([_feature("PROJ-1", "f", [_scenario("PROJ-2", "s", None)])])
    )
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json")

    result = triage.triage_paths([good, bad])
    assert result.scenarios_passed == 1
