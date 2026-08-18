"""Chronic vs intermittent across runs.

Each class here pins one thing that was got wrong when this was done by hand.
"""

from __future__ import annotations

import json

from aitlc.core import test_history as th


def _run(date, outcome, *, step="", error="", infra=False):
    return th.RunOutcome(
        date=date,
        run=f"{date}T00-00-00-000000",
        outcome=outcome,
        step=step,
        error=error,
        signature=th.signature_of(step, error) if outcome == "failed" else "",
        infrastructure=infra,
    )


class TestSignatures:
    """Counts are not the signal -- whether it failed the *same way* is."""

    def test_the_same_defect_twice_collapses_to_one_signature(self):
        a = th.signature_of("Then click save", "TimeoutError: Timeout 30000ms exceeded")
        b = th.signature_of("Then click save", "TimeoutError: Timeout 45000ms exceeded")
        assert a == b, "a timing difference must not look like a different defect"

    def test_generated_names_do_not_split_a_signature(self):
        a = th.signature_of("Then wait", "audience 123456 not found")
        b = th.signature_of("Then wait", "audience 987654 not found")
        assert a == b

    def test_genuinely_different_failures_stay_distinct(self):
        a = th.signature_of("Then click save", "TimeoutError: Timeout exceeded")
        b = th.signature_of("Then click save", "AssertionError: no rows displayed")
        assert a != b

    def test_a_call_log_below_the_first_line_is_not_part_of_identity(self):
        a = th.signature_of("Then click", "TimeoutError: x\n  - waiting for a")
        b = th.signature_of("Then click", "TimeoutError: x\n  - waiting for b")
        assert a == b


class TestVerdicts:
    def test_failing_the_same_way_every_run_is_deterministic(self):
        runs = [_run(f"2026-08-1{i}", "failed", step="s", error="E: x") for i in range(3)]
        assert th.classify(runs) == th.VERDICT_DETERMINISTIC

    def test_two_signatures_is_intermittent_even_if_it_always_failed(self):
        """The distinction a naive fail-count erases."""
        runs = [
            _run("2026-08-11", "failed", step="s", error="E: x"),
            _run("2026-08-12", "failed", step="s", error="OtherError: y"),
        ]
        assert th.classify(runs) == th.VERDICT_INTERMITTENT

    def test_one_pass_among_failures_is_intermittent(self):
        runs = [
            _run("2026-08-11", "failed", step="s", error="E: x"),
            _run("2026-08-12", "passed"),
        ]
        assert th.classify(runs) == th.VERDICT_INTERMITTENT

    def test_all_passing_is_healthy(self):
        assert th.classify([_run("2026-08-11", "passed")]) == th.VERDICT_HEALTHY

    def test_never_having_run_is_unknown_not_healthy(self):
        assert th.classify([_run("2026-08-11", "not_found")]) == th.VERDICT_UNKNOWN


class TestOutageDays:
    """A day where everything failed is an outage, not N independent defects."""

    def test_a_run_where_every_test_failed_is_flagged(self):
        by_run = {
            "r1": [_run("2026-08-13", "failed") for _ in range(5)],
            "r2": [_run("2026-08-14", "failed") for _ in range(4)]
            + [_run("2026-08-14", "passed")],
        }
        assert th.mark_infrastructure_runs(by_run) == {"r1"}

    def test_a_single_test_failing_alone_is_not_called_an_outage(self):
        by_run = {"r1": [_run("2026-08-13", "failed")]}
        assert th.mark_infrastructure_runs(by_run) == set()

    def test_a_small_query_is_not_mistaken_for_an_outage(self):
        """Asking about three tests that all broke must not erase them.

        Labelling that an outage drops every failure from the rates and
        reports a broken test as healthy -- a small query turning into a
        wrong answer.
        """
        by_run = {"r1": [_run("2026-08-13", "failed") for _ in range(3)]}
        assert th.mark_infrastructure_runs(by_run) == set()

    def test_an_outage_day_does_not_make_a_healthy_test_deterministic(self):
        runs = [
            _run("2026-08-12", "passed"),
            _run("2026-08-13", "failed", step="s", error="E: x", infra=True),
        ]
        assert th.classify(runs) == th.VERDICT_HEALTHY


class TestBreakDates:
    def test_first_failed_is_the_break_not_the_earliest_ever_failure(self):
        """A failure followed by a pass is not when the test broke."""
        runs = [
            _run("2026-08-11", "failed", step="s", error="E: x"),
            _run("2026-08-12", "passed"),
            _run("2026-08-14", "failed", step="s", error="E: x"),
            _run("2026-08-15", "failed", step="s", error="E: x"),
        ]
        history = th.build_history("PROJ-1", runs)
        assert history.last_passed == "2026-08-12"
        assert history.first_failed == "2026-08-14"

    def test_counts_exclude_outage_days(self):
        runs = [
            _run("2026-08-13", "failed", step="s", error="E: x", infra=True),
            _run("2026-08-14", "failed", step="s", error="E: x"),
        ]
        history = th.build_history("PROJ-1", runs)
        assert history.runs_failed == 1, "the outage day must not inflate the rate"


class TestMatrixAndStore:
    def test_the_matrix_lines_tests_up_on_shared_dates(self):
        a = th.build_history("PROJ-1", [_run("2026-08-11", "failed", step="s", error="E")])
        b = th.build_history("PROJ-2", [_run("2026-08-12", "passed")])
        grid = th.matrix([a, b])
        assert grid["dates"] == ["2026-08-11", "2026-08-12"]
        # A date a test did not run on is a gap, not a pass.
        assert grid["rows"][0]["cells"] == ["FAIL", "."]
        assert grid["rows"][1]["cells"] == [".", "PASS"]

    def test_the_store_is_keyed_by_test_and_survives_a_corrupt_file(self, tmp_path):
        path = tmp_path / "test-history.json"
        path.write_text("{not json")
        history = th.build_history("PROJ-1", [_run("2026-08-11", "passed")])

        th.merge_into_store(path, [history])

        stored = json.loads(path.read_text())
        assert stored["PROJ-1"]["verdict"] == th.VERDICT_HEALTHY

    def test_a_second_test_does_not_evict_the_first(self, tmp_path):
        path = tmp_path / "test-history.json"
        th.merge_into_store(path, [th.build_history("PROJ-1", [_run("2026-08-11", "passed")])])
        th.merge_into_store(path, [th.build_history("PROJ-2", [_run("2026-08-11", "passed")])])
        stored = json.loads(path.read_text())
        assert set(stored) == {"PROJ-1", "PROJ-2"}


class TestMatrixAggregatesADay:
    """A suite runs many times a day; a date cell must not be last-one-wins."""

    def test_a_failure_anywhere_in_the_day_shows_as_failed(self):
        history = th.build_history(
            "PROJ-1",
            [
                _run("2026-08-18", "failed", step="s", error="E: x"),
                _run("2026-08-18", "passed"),
            ],
        )
        assert th.matrix([history])["rows"][0]["cells"] == ["FAIL"]

    def test_a_day_of_passes_shows_as_passed(self):
        history = th.build_history(
            "PROJ-1", [_run("2026-08-18", "passed"), _run("2026-08-18", "passed")]
        )
        assert th.matrix([history])["rows"][0]["cells"] == ["PASS"]

    def test_an_outage_only_day_is_neither_pass_nor_fail(self):
        """Otherwise the grid says FAIL beside a rate that excludes it."""
        history = th.build_history(
            "PROJ-1",
            [_run("2026-08-18", "failed", step="s", error="E: x", infra=True)],
        )
        row = th.matrix([history])["rows"][0]
        assert row["cells"] == ["OUT"]
        assert row["fail_rate"] == "0/1"

    def test_the_grid_and_the_rate_agree(self):
        history = th.build_history(
            "PROJ-1",
            [
                _run("2026-08-17", "failed", step="s", error="E: x", infra=True),
                _run("2026-08-18", "passed"),
            ],
        )
        row = th.matrix([history])["rows"][0]
        assert row["cells"] == ["OUT", "PASS"]
        assert row["fail_rate"] == "0/2"
        assert row["verdict"] == th.VERDICT_HEALTHY
