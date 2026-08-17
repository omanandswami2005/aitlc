import json
from pathlib import Path

from aitlc.core.behave_runner import _extract_error_message, parse_report


def _write_report(tmp_path: Path, features: list) -> Path:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(features))
    return path


def test_counts_steps_by_status(tmp_path: Path):
    features = [
        {
            "elements": [
                {
                    "name": "Scenario A",
                    "steps": [
                        {
                            "keyword": "When ",
                            "name": "do a thing",
                            "result": {"status": "passed"},
                        },
                        {
                            "keyword": "Then ",
                            "name": "check it",
                            "result": {"status": "passed"},
                        },
                    ],
                }
            ]
        }
    ]
    result = parse_report(_write_report(tmp_path, features))
    assert result.steps_by_status == {"passed": 2}
    assert result.failures == []
    assert result.passed  # exit_code defaults to 0, no failures


def test_error_message_as_list_is_joined_not_crashed(tmp_path: Path):
    # Real bug found building this: error_message can be a list of lines,
    # not a str.
    features = [
        {
            "elements": [
                {
                    "name": "Scenario A",
                    "steps": [
                        {
                            "keyword": "Then ",
                            "name": "check it",
                            "result": {
                                "status": "failed",
                                "error_message": ["line one", "line two: real error"],
                            },
                        }
                    ],
                }
            ]
        }
    ]
    result = parse_report(_write_report(tmp_path, features))
    assert len(result.failures) == 1
    assert "real error" in result.failures[0].error


def test_error_message_extracts_real_exception_not_traceback_header(tmp_path: Path):
    # Real bug found building this: naively taking the first line of a
    # joined traceback gives "Traceback (most recent call last):", not the
    # actual exception.
    error_text = (
        "Traceback (most recent call last):\n"
        '  File "foo.py", line 1, in bar\n'
        "    raise ValueError('boom')\n"
        "ValueError: boom\n"
        "\nCaptured stdout:\n"
        "some noisy log output\n"
        "more noise\n"
    )
    features = [
        {
            "elements": [
                {
                    "name": "Scenario A",
                    "steps": [
                        {
                            "keyword": "Then ",
                            "name": "check it",
                            "result": {"status": "failed", "error_message": error_text},
                        }
                    ],
                }
            ]
        }
    ]
    result = parse_report(_write_report(tmp_path, features))
    assert result.failures[0].error == "ValueError: boom"
    assert "Traceback" not in result.failures[0].error
    assert "noisy log" not in result.failures[0].error


def test_missing_report_file_returns_empty_result(tmp_path: Path):
    result = parse_report(tmp_path / "does-not-exist.json")
    assert result.steps_by_status == {}
    assert result.failures == []


def test_to_dict_shape(tmp_path: Path):
    features = [
        {
            "elements": [
                {
                    "name": "Scenario A",
                    "steps": [
                        {
                            "keyword": "Then ",
                            "name": "check it",
                            "result": {"status": "failed", "error_message": "boom"},
                        }
                    ],
                }
            ]
        }
    ]
    result = parse_report(_write_report(tmp_path, features))
    payload = result.to_dict()
    assert payload == {
        "steps_by_status": {"failed": 1},
        "failures": [
            {"scenario": "Scenario A", "step": "Then check it", "error": "boom"}
        ],
    }


def test_scenario_duration_sums_step_durations(tmp_path: Path):
    features = [
        {
            "name": "Feature X",
            "elements": [
                {
                    "name": "Scenario A",
                    "type": "scenario",
                    "status": "passed",
                    "steps": [
                        {
                            "keyword": "Given ",
                            "name": "a",
                            "result": {"status": "passed", "duration": 1.5},
                        },
                        {
                            "keyword": "When ",
                            "name": "b",
                            "result": {"status": "passed", "duration": 2.25},
                        },
                    ],
                }
            ],
        }
    ]
    result = parse_report(_write_report(tmp_path, features))
    assert len(result.scenarios) == 1
    scenario = result.scenarios[0]
    assert scenario.feature == "Feature X"
    assert scenario.name == "Scenario A"
    assert scenario.status == "passed"
    assert scenario.duration_seconds == 3.75


def test_scenario_duration_handles_missing_duration_field(tmp_path: Path):
    # Steps skipped after an earlier failure have no `duration` at all.
    features = [
        {
            "name": "Feature X",
            "elements": [
                {
                    "name": "Scenario A",
                    "steps": [
                        {
                            "keyword": "Given ",
                            "name": "a",
                            "result": {"status": "failed", "duration": 1.0},
                        },
                        {
                            "keyword": "When ",
                            "name": "b",
                            "result": {"status": "skipped"},
                        },
                    ],
                }
            ],
        }
    ]
    result = parse_report(_write_report(tmp_path, features))
    assert result.scenarios[0].duration_seconds == 1.0


def test_background_elements_are_not_counted_as_scenarios(tmp_path: Path):
    features = [
        {
            "name": "Feature X",
            "elements": [
                {
                    "name": "",
                    "type": "background",
                    "steps": [
                        {
                            "keyword": "Given ",
                            "name": "setup",
                            "result": {"status": "passed", "duration": 5.0},
                        }
                    ],
                },
                {
                    "name": "Scenario A",
                    "type": "scenario",
                    "status": "passed",
                    "steps": [
                        {
                            "keyword": "When ",
                            "name": "b",
                            "result": {"status": "passed", "duration": 1.0},
                        }
                    ],
                },
            ],
        }
    ]
    result = parse_report(_write_report(tmp_path, features))
    assert len(result.scenarios) == 1
    assert result.scenarios[0].name == "Scenario A"


def test_extract_error_message_strips_captured_stderr():
    """A stderr warning after the assertion must not become the failure.

    Observed live: a step failed on a locator assertion but reported
    "warnings.warn(" -- the continuation line of urllib3's
    InsecureRequestWarning, which behave had appended under "Captured stderr:".
    """
    step = {
        "result": {
            "error_message": [
                "Assertion Failed: Locator expected to be visible",
                '  - waiting for locator("#hamburger")',
                "",
                "Captured stderr:",
                "/x/urllib3/connectionpool.py:1097: InsecureRequestWarning: ...",
                "  warnings.warn(",
            ]
        }
    }
    assert "warnings.warn" not in _extract_error_message(step)
    assert "#hamburger" in _extract_error_message(step)
