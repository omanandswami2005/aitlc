"""Attributing a parallel failure without paying for a re-run.

`--verify-failures` re-runs each failure serially, which is correct and
correctly opt-in. The problem it left was that declining it gave you nothing,
while two cheap signals were already sitting in the results.
"""

from __future__ import annotations

from aitlc.core import interference


def _row(name, start, end):
    return {"feature": name, "started_at": start, "ended_at": end}


class TestOverlap:
    def test_runs_that_never_overlapped_are_not_suspects(self):
        """The cheapest thing this does: rule siblings out for free."""
        failure = _row("a.feature", 0, 10)
        others = [failure, _row("b.feature", 20, 30)]
        assert interference.suspects_for(failure, others, {}) == []

    def test_overlapping_runs_are_reported_with_how_long(self):
        failure = _row("a.feature", 0, 10)
        others = [failure, _row("b.feature", 5, 15)]
        suspects = interference.suspects_for(failure, others, {})
        assert len(suspects) == 1
        assert suspects[0].seconds == 5

    def test_a_run_never_suspects_itself(self):
        failure = _row("a.feature", 0, 10)
        assert interference.suspects_for(failure, [failure], {}) == []

    def test_touching_but_not_overlapping_is_not_overlap(self):
        assert interference.overlap_seconds(_row("a", 0, 10), _row("b", 10, 20)) == 0


class TestAccountCollision:
    ACCOUNTS = {
        "a.feature": {"shared@example.com"},
        "b.feature": {"shared@example.com"},
        "c.feature": {"other@example.com"},
    }

    def test_a_shared_account_is_surfaced(self):
        """Near-certain explanation in a suite that shares accounts."""
        failure = _row("a.feature", 0, 10)
        others = [failure, _row("b.feature", 5, 15)]
        suspects = interference.suspects_for(failure, others, self.ACCOUNTS)
        assert suspects[0].same_account == "shared@example.com"

    def test_a_shared_account_outranks_a_longer_unrelated_overlap(self):
        """Otherwise the strong signal is buried under a weaker one."""
        failure = _row("a.feature", 0, 100)
        others = [failure, _row("c.feature", 0, 100), _row("b.feature", 90, 95)]
        suspects = interference.suspects_for(failure, others, self.ACCOUNTS)
        assert suspects[0].other == "b.feature"

    def test_accounts_are_found_in_feature_text(self):
        found = interference.accounts_in(
            'When search for "someone@example.com" then "other@example.co.uk"'
        )
        assert found == {"someone@example.com", "other@example.co.uk"}


class TestTheNote:
    def test_no_overlap_says_a_sibling_cannot_explain_it(self):
        note = interference.interference_note([])
        assert "cannot explain" in note

    def test_a_shared_account_is_called_out_as_the_likely_cause(self):
        failure = _row("a.feature", 0, 10)
        others = [failure, _row("b.feature", 5, 15)]
        suspects = interference.suspects_for(
            failure, others, {"a.feature": {"s@e.com"}, "b.feature": {"s@e.com"}}
        )
        note = interference.interference_note(suspects)
        assert "same account" in note and "s@e.com" in note

    def test_overlap_without_a_shared_account_is_called_weak(self):
        """Stated as suspicion, not verdict -- concurrency alone proves nothing."""
        failure = _row("a.feature", 0, 10)
        others = [failure, _row("c.feature", 5, 15)]
        note = interference.interference_note(
            interference.suspects_for(failure, others, {"a.feature": {"x@e.com"}})
        )
        assert "weak evidence" in note
