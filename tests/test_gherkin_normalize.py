from aitlc.adapters.xray.gherkin_normalize import (
    diff_lines,
    normalize_gherkin_body,
    normalize_local_feature,
)


def test_strips_feature_tags_and_scenario_header():
    text = """Feature: Some feature

\t@TEST_PROJ-12345 @Automation @red
\tScenario Outline: Some feature
\t\tWhen do a thing
\t\tThen check a result

\tExamples:
\t| a | b |
\t| 1 | 2 |
"""
    normalized = normalize_local_feature(text)
    assert "Feature:" not in normalized
    assert "@TEST_PROJ-12345" not in normalized
    assert "Scenario Outline:" not in normalized
    assert "When do a thing" in normalized
    assert "Examples:" in normalized


def test_strips_precondition_injected_background():
    text = """Feature: Some feature

\tBackground:
\t\t#@PRECOND_PROJ-3783
\t\tGiven open the app
\t\tWhen wait until load with timeout: "100"

\t@TEST_PROJ-19017
\tScenario Outline: Some feature
\t\tWhen select database: "<database>"
"""
    normalized = normalize_local_feature(text)
    assert "open the app" not in normalized
    assert "wait until load" not in normalized
    assert "select database" in normalized


def test_strips_comment_lines():
    text = """Feature: X

\t@TEST_PROJ-1
\tScenario: X
\t\t#Tests As a user I expect...
\t\t#
\t\tWhen do a thing
"""
    normalized = normalize_local_feature(text)
    assert "#" not in normalized
    assert "do a thing" in normalized


def test_matches_live_gherkin_shape_when_identical():
    local_text = """Feature: X

\t@TEST_PROJ-1
\tScenario: X
\t\tWhen do a thing
\t\tThen check a result
"""
    live = "When do a thing\nThen check a result"
    normalized = normalize_local_feature(local_text)
    assert normalized == live


def test_diff_lines_empty_when_identical():
    assert diff_lines("a\nb", "a\nb") == []


def test_diff_lines_reports_drift():
    diff = diff_lines("When new step\nThen check", "When old step\nThen check")
    assert diff  # non-empty
    assert any("old step" in line for line in diff)
    assert any("new step" in line for line in diff)


def test_normalize_gherkin_body_accepts_a_full_feature_file():
    """The natural command is `--file <the .feature you just compared>`.

    Sending that verbatim wrote the tags and Feature: line into a live Test and
    left it invalid, so the write path must normalize exactly as compare does.
    """
    body = normalize_gherkin_body(
        "@skip_login\n"
        "Feature: Something\n"
        "\n"
        "\t@TEST_PROJ-1 @Automation\n"
        "\tScenario: does a thing\n"
        "\tGiven open the app\n"
        "\tWhen click on it\n"
    )
    assert body == "Given open the app\nWhen click on it"


def test_normalize_gherkin_body_is_idempotent():
    """A body fetched from Xray and written straight back must not change."""
    already = "Given open the app\nWhen click on it"
    assert normalize_gherkin_body(already) == already
    assert normalize_gherkin_body(normalize_gherkin_body(already)) == already


def test_normalize_gherkin_body_refuses_a_payload_with_a_feature_line():
    """Fail loudly rather than corrupt the Test.

    Corruption here is invisible until the next fetch, so a hard error beats a
    write that "succeeded".
    """
    import pytest

    with pytest.raises(ValueError, match="header lines"):
        # No Scenario: header, so the Feature: line survives normalization.
        normalize_gherkin_body("Feature: Something\nGiven open the app")
