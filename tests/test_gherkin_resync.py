"""Editing the Gherkin mid-session.

The session holds the step list parsed when it started. Without re-reading,
`retry` re-runs text that may no longer be in the file and reports a result
for a step that no longer exists -- the same class of lie as running stale
Python.
"""

from __future__ import annotations

from aitlc.core import debug_session


def _feature(*steps):
    body = "".join(f"    {s}\n" for s in steps)
    return f"Feature: f\n\n  @TEST_PROJ-1\n  Scenario: s\n{body}"


def _session(steps, index):
    return debug_session.DebugSession(
        test_id="PROJ-1",
        feature="f.feature",
        cdp_url="http://127.0.0.1:9999",
        port=9999,
        steps=[f"    {s}" for s in steps],
        index=index,
    )


class TestNoChange:
    def test_an_unchanged_file_is_not_reported_as_reloaded(self):
        session = _session(["Given a", "When b"], 1)
        assert debug_session.resync(session, _feature("Given a", "When b")) == {
            "feature_reloaded": False
        }

    def test_the_cursor_is_untouched(self):
        session = _session(["Given a", "When b"], 1)
        debug_session.resync(session, _feature("Given a", "When b"))
        assert session.index == 1


class TestTheCursorFollowsTheStep:
    def test_inserting_a_step_above_moves_the_cursor_with_its_step(self):
        """Keeping the index would silently park you on a different step."""
        session = _session(["Given a", "When b"], 1)

        report = debug_session.resync(
            session, _feature("Given a", "And a wait", "When b")
        )

        assert report["feature_reloaded"] is True
        assert report["cursor"] == "followed"
        assert session.index == 2
        assert session.steps[session.index].strip() == "When b"

    def test_editing_a_later_step_keeps_the_cursor_where_it_was(self):
        session = _session(["Given a", "When b", "Then c"], 1)

        report = debug_session.resync(
            session, _feature("Given a", "When b", "Then c edited")
        )

        assert report["cursor"] == "kept"
        assert session.index == 1

    def test_deleting_the_step_under_the_cursor_is_reported_not_guessed(self):
        session = _session(["Given a", "When b", "Then c"], 1)

        report = debug_session.resync(session, _feature("Given a", "Then c"))

        assert report["cursor"] == "clamped"
        assert session.index <= len(session.steps) - 1

    def test_step_counts_are_reported_so_the_change_is_visible(self):
        session = _session(["Given a", "When b"], 0)
        report = debug_session.resync(session, _feature("Given a", "When b", "Then c"))
        assert report["steps_before"] == 2
        assert report["steps_after"] == 3


class TestBinding:
    def test_the_same_examples_row_is_bound_again(self):
        """A refresh must not silently switch rows."""
        text = (
            "Feature: f\n\n  @TEST_PROJ-1\n  Scenario Outline: s\n"
            '    When search for "<term>"\n\n'
            "    Examples:\n      | term |\n      | one  |\n      | two  |\n"
        )
        session = _session(['When search for "two"'], 0)
        session.example = 1

        debug_session.resync(session, text)

        assert 'search for "two"' in session.steps[0]

    def test_an_unbindable_edit_reports_the_error_rather_than_half_applying(self):
        text = (
            "Feature: f\n\n  @TEST_PROJ-1\n  Scenario Outline: s\n"
            '    When search for "<missing>"\n\n'
            "    Examples:\n      | term |\n      | one  |\n"
        )
        session = _session(["When search"], 0)
        before = list(session.steps)

        report = debug_session.resync(session, text)

        assert report["feature_reloaded"] is False
        assert "missing" in report["error"]
        assert session.steps == before, "a bad edit must not half-apply"
