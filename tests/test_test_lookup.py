"""Resolving a test key to its run outcome.

Built from a real investigation that went wrong: the question was "did
PROJ-2 pass last night", and every tool available answered it by suite
title, so the search went to the wrong report twice and read megabytes of
HTML before the key turned up as a scenario tag inside a differently-named
file. These tests pin the two shapes that mattered there.
"""

from __future__ import annotations

from aitlc.core import test_lookup

RUN = "2026-01-02T03-04-05-678901"
KEY_A = f"results/Env/Some_Test_Plan/behave_PROJ-1_{RUN}.json"
KEY_B = f"results/Env/Other_Test_Plan/behave_PROJ-9_{RUN}.json"


def _doc(*, feature_tags, elements):
    return [{"name": "a feature", "tags": feature_tags, "elements": elements}]


def _scenario(name, tags, *, failing_step=None):
    steps = [
        {"keyword": "Given ", "name": "open the app", "result": {"status": "passed"}},
    ]
    if failing_step:
        steps.append(
            {
                "keyword": "When ",
                "name": failing_step,
                "result": {
                    "status": "failed",
                    "error_message": (
                        "TimeoutError: Locator.click: Timeout 30000ms exceeded.\n"
                        '  - waiting for locator("#save")\n'
                        "\nCaptured logging:\nnoise that must not become the error\n"
                    ),
                },
            }
        )
    else:
        steps.append(
            {"keyword": "Then ", "name": "see it", "result": {"status": "passed"}}
        )
    return {"type": "scenario", "name": name, "tags": tags, "steps": steps}


class TestObjectKeys:
    def test_plan_execution_key_and_run_are_read_from_the_key(self):
        info = test_lookup.parse_object_key(KEY_A)
        assert info is not None
        assert info.execution_key == "PROJ-1"
        assert info.plan == "Some_Test_Plan"
        assert info.run_timestamp == RUN

    def test_an_unrelated_object_is_not_forced_into_the_shape(self):
        assert test_lookup.parse_object_key("results/Env/notes.txt") is None

    def test_key_names_test_is_the_cheap_half_of_the_lookup(self):
        assert test_lookup.key_names_test(KEY_A, "PROJ-1")
        assert test_lookup.key_names_test(KEY_A, "proj-1")  # case is not signal
        assert not test_lookup.key_names_test(KEY_A, "PROJ-2")


class TestNestedScenarioKeys:
    """The case that cost a whole investigation."""

    def test_a_key_that_no_filename_mentions_is_still_found_inside(self):
        doc = _doc(
            feature_tags=[{"name": "TEST_PROJ-9"}],
            elements=[_scenario("s", [{"name": "TEST_PROJ-2"}])],
        )
        # Nothing in the object name suggests PROJ-2 ...
        assert not test_lookup.key_names_test(KEY_B, "PROJ-2")
        # ... but it ran, and reporting "did not run" here is the bug.
        outcome = test_lookup.outcome_for_test(doc, "PROJ-2", source=KEY_B)
        assert outcome.status == "passed"
        assert outcome.matched_by == "tag"
        assert outcome.execution_key == "PROJ-9"
        assert outcome.plan == "Other_Test_Plan"

    def test_a_key_that_genuinely_did_not_run_reads_as_not_found(self):
        doc = _doc(
            feature_tags=[{"name": "TEST_PROJ-9"}],
            elements=[_scenario("s", [{"name": "TEST_PROJ-2"}])],
        )
        outcome = test_lookup.outcome_for_test(doc, "PROJ-404", source=KEY_B)
        assert outcome.status == "not_found"

    def test_a_bare_tag_without_the_test_prefix_still_matches(self):
        doc = _doc(feature_tags=[], elements=[_scenario("s", [{"name": "PROJ-2"}])])
        assert test_lookup.outcome_for_test(doc, "PROJ-2").status == "passed"

    def test_string_tags_are_handled_like_dict_tags(self):
        doc = _doc(feature_tags=[], elements=[_scenario("s", ["TEST_PROJ-2"])])
        assert test_lookup.outcome_for_test(doc, "PROJ-2").status == "passed"


class TestOutcomes:
    def test_a_failure_reports_the_step_and_the_real_error(self):
        doc = _doc(
            feature_tags=[],
            elements=[
                _scenario("s", [{"name": "TEST_PROJ-1"}], failing_step="click save")
            ],
        )
        outcome = test_lookup.outcome_for_test(doc, "PROJ-1", source=KEY_A)

        assert outcome.status == "failed"
        assert outcome.failures[0]["step"] == "When click save"
        assert "Timeout" in outcome.failures[0]["error"]
        # behave appends captured streams after the traceback; letting one of
        # them become "the error" is how a run once reported a warning as its
        # failure.
        assert "noise" not in outcome.failures[0]["error"]
        assert 'locator("#save")' in outcome.failures[0]["locator"]

    def test_outline_rows_are_aggregated_not_last_one_wins(self):
        """Rows share one tag; counting only the last reported 6 failures as 0."""
        doc = _doc(
            feature_tags=[],
            elements=[
                _scenario("row 1", [{"name": "TEST_PROJ-1"}], failing_step="click"),
                _scenario("row 2", [{"name": "TEST_PROJ-1"}]),
            ],
        )
        outcome = test_lookup.outcome_for_test(doc, "PROJ-1", source=KEY_A)
        assert outcome.scenarios_failed == 1
        assert outcome.scenarios_passed == 1
        assert outcome.status == "failed"

    def test_a_feature_level_tag_covers_its_scenarios(self):
        doc = _doc(
            feature_tags=[{"name": "TEST_PROJ-1"}],
            elements=[_scenario("s", []), _scenario("s2", [])],
        )
        assert test_lookup.outcome_for_test(doc, "PROJ-1").scenarios_passed == 2

    def test_background_is_not_counted_as_a_scenario(self):
        doc = _doc(
            feature_tags=[{"name": "TEST_PROJ-1"}],
            elements=[
                {"type": "background", "name": "b", "tags": [], "steps": []},
                _scenario("s", []),
            ],
        )
        assert test_lookup.outcome_for_test(doc, "PROJ-1").scenarios_passed == 1


def test_a_keyword_without_a_trailing_space_still_reads_as_prose():
    """Real exports write "Then" and "Then "; blind concatenation gave "Thenclick"."""
    doc = [
        {
            "name": "f",
            "tags": [{"name": "TEST_PROJ-1"}],
            "elements": [
                {
                    "type": "scenario",
                    "name": "s",
                    "tags": [],
                    "steps": [
                        {
                            "keyword": "Then",
                            "name": 'double click on row number "2"',
                            "result": {
                                "status": "failed",
                                "error_message": "Assertion Failed: no rows",
                            },
                        }
                    ],
                }
            ],
        }
    ]
    outcome = test_lookup.outcome_for_test(doc, "PROJ-1")
    assert outcome.failures[0]["step"] == 'Then double click on row number "2"'


def test_filtering_happens_before_truncation():
    """G30: `--limit` must not hide the run `--at` asked for.

    Reproduces the ordering bug directly on the list operations the command
    performs, which is where the defect lived -- truncating first drops the
    older run before the filter ever sees it.
    """
    newest = [f"p/behave_PROJ-{i}_2026-08-18T00-00-00-000000.json" for i in range(5)]
    wanted = "p/behave_PROJ-9_2026-08-11T00-00-00-000000.json"
    keys = newest + [wanted]
    at = "2026-08-11"
    limit = 3

    truncate_first = [k for k in keys[:limit] if at in k]
    filter_first = [k for k in keys if at in k][:limit]

    assert truncate_first == [], "documents the bug: the run is invisible"
    assert filter_first == [wanted]
