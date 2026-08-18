"""Position arithmetic for a debug session.

Everything here is a mistake that was made by hand before the session existed:
slices that would not parse, restarting from step 0 after each fix, and having
no record of whether a step had passed once or three times.
"""

from __future__ import annotations

import pytest

from aitlc.core.debug_session import (
    DebugSession,
    ExampleBindingError,
    examples_rows,
    clear,
    feature_steps,
    is_step_line,
    load,
    promote_leading_continuation,
    save,
)

FEATURE = """Feature: something

\t@TEST_PROJ-1 @Automation
\tScenario Outline: does a thing
\tGiven open the app
\tWhen click the button
\tAnd wait for the loader
\tThen validate the result
\t| header | row |
\t# a comment

\tExamples:
\t| a | b |
\t| 1 | 2 |
"""


def test_feature_steps_ignores_structure_tables_and_examples():
    steps = [s.strip() for s in feature_steps(FEATURE)]
    assert steps == [
        "Given open the app",
        "When click the button",
        "And wait for the loader",
        "Then validate the result",
    ]


def test_is_step_line_rejects_tags_comments_and_tables():
    assert is_step_line("\tGiven open the app")
    assert not is_step_line("\t@TEST_PROJ-1")
    assert not is_step_line("\t# note")
    assert not is_step_line("\t| a | b |")
    assert not is_step_line("Feature: x")


def test_leading_continuation_is_promoted_so_the_slice_parses():
    """A slice starting on And/But is rejected by Behave: no previous step.

    Half a real feature's lines start with And, so any position-based slicing
    must handle this or fail on most positions a user picks.
    """
    assert promote_leading_continuation(["\tAnd wait for the loader"]) == [
        "\tWhen wait for the loader"
    ]
    assert promote_leading_continuation(["  But nothing happens"]) == [
        "  When nothing happens"
    ]
    # only the first line is touched
    out = promote_leading_continuation(["Given a", "And b"])
    assert out == ["Given a", "And b"]


def _session() -> DebugSession:
    return DebugSession(
        test_id="PROJ-1",
        feature="f.feature",
        cdp_url="http://127.0.0.1:1234",
        port=1234,
        steps=[s for s in feature_steps(FEATURE)],
    )


def test_advance_stops_at_the_end_and_reports_finished():
    s = _session()
    for _ in range(10):
        s.advance()
    assert s.finished
    assert s.current is None


def test_slice_through_promotes_its_first_line():
    s = _session()
    # steps[2] is the And; slicing from it must still parse
    s.steps = s.steps[2:]
    assert s.slice_through(2)[0].strip().startswith("When wait for the loader")


def test_attempts_accumulate_per_step_so_flakiness_is_visible():
    """One pass does not prove a fix; the history is what shows that."""
    s = _session()
    s.record("failed")
    s.record("passed")
    assert [a.status for a in s.attempts_for_current()] == ["failed", "passed"]
    s.advance()
    assert s.attempts_for_current() == []


def test_session_round_trips_through_disk(tmp_path):
    s = _session()
    s.record("failed")
    s.advance()
    save(tmp_path, s)

    back = load(tmp_path, "PROJ-1")
    assert back is not None
    assert back.index == s.index
    assert back.cdp_url == s.cdp_url
    assert [a.status for a in back.attempts] == ["failed"]

    assert clear(tmp_path, "PROJ-1") is True
    assert load(tmp_path, "PROJ-1") is None
    assert clear(tmp_path, "PROJ-1") is False


OUTLINE = """Feature: outline

\t@TEST_PROJ-2 @Automation
\tScenario Outline: creates a thing
\tGiven open the app
\tWhen select category: "<category>" and filter: "<Filter>"
\tAnd go to ID: "audienceName" and type: "<audience_name>"
\tThen validate "<audience_name>" exists

\tExamples:
\t| audience_name | category | Filter |
\t| random_name | Buyer Intent | Topics and Score Range |
\t| second_name | Firmographics | Employee Size |
"""


def test_examples_row_is_bound_by_default():
    """A Scenario Outline must run the values Behave would run, not the text.

    This is the regression for the bug where a live session typed the literal
    "<category>" into a search box and reported the step as having run.
    """
    steps = [s.strip() for s in feature_steps(OUTLINE)]
    assert steps == [
        "Given open the app",
        'When select category: "Buyer Intent" and filter: "Topics and Score Range"',
        'And go to ID: "audienceName" and type: "random_name"',
        'Then validate "random_name" exists',
    ]


def test_example_index_selects_the_row():
    steps = [s.strip() for s in feature_steps(OUTLINE, example=1)]
    assert 'type: "second_name"' in steps[2]
    assert '"Firmographics"' in steps[1]


def test_example_none_keeps_raw_placeholders():
    steps = [s.strip() for s in feature_steps(OUTLINE, example=None)]
    assert "<category>" in steps[1]


def test_out_of_range_example_is_an_error_not_a_silent_first_row():
    with pytest.raises(ExampleBindingError) as exc:
        feature_steps(OUTLINE, example=7)
    assert "2 row(s)" in str(exc.value)


def test_placeholder_with_no_column_is_an_error():
    """Better to refuse than to run a step still containing <...>."""
    broken = OUTLINE.replace("| audience_name | category | Filter |", "| category | Filter |").replace(
        "| random_name | Buyer Intent | Topics and Score Range |", "| Buyer Intent | Topics and Score Range |"
    ).replace(
        "| second_name | Firmographics | Employee Size |", "| Firmographics | Employee Size |"
    )
    with pytest.raises(ExampleBindingError) as exc:
        feature_steps(broken)
    assert "<audience_name>" in str(exc.value)


def test_plain_scenario_without_examples_still_works():
    plain = "Feature: f\n\tScenario: s\n\tGiven open the app\n"
    assert [s.strip() for s in feature_steps(plain)] == ["Given open the app"]


def test_short_example_row_is_skipped_not_mis_bound():
    """A ragged row must not zip-truncate into a wrong-but-plausible binding."""
    ragged = OUTLINE.replace(
        "| second_name | Firmographics | Employee Size |", "| second_name | Firmographics |"
    )
    rows = examples_rows(ragged)
    assert len(rows) == 1
    assert rows[0]["audience_name"] == "random_name"
