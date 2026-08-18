"""Will this feature run here the way it runs in CI?

Each case below is a difference that produced a real wasted investigation.
"""

from __future__ import annotations

from aitlc.core import fidelity

COMMENTED_LOGIN = """@Automation
Feature: intent

  Background:
    # When logout if logged in
    # Given open the app
    # Then login to the app with "someone@example.com" and "valid_password"

  @TEST_PROJ-1
  Scenario: search inside a saved audience
    When search for "term"
    Then see results
"""

SELF_CONTAINED = """@skip_login
Feature: intent

  @TEST_PROJ-1
  Scenario: sign in and search
    Given open the app
    When login to the app with "someone@example.com" and "valid_password"
    Then see results
"""

EXECUTION = """Feature: the whole execution

  @TEST_PROJ-1
  Scenario: first
    Given open the app
    When login to the app with "a@example.com" and "valid_password"

  @TEST_PROJ-2
  Scenario: second
    When search inside what the first scenario built
"""

TAGS_ON_SCENARIO = """Feature: intent

  @skip_login @TEST_PROJ-1
  Scenario: relies on a hook
    When search for "term"
"""


class TestMissingSession:
    def test_a_commented_out_login_is_a_blocker(self):
        """The exact trap: correct in CI, broken-looking alone."""
        report = fidelity.analyze(COMMENTED_LOGIN)
        assert report.creates_session is False
        assert report.faithful is False
        kinds = [f.kind for f in report.findings]
        assert "session_commented_out" in kinds

    def test_the_quoted_step_is_the_informative_one(self):
        """Quoting 'click Sign in' sends the reader to look at a button."""
        report = fidelity.analyze(COMMENTED_LOGIN)
        finding = next(f for f in report.findings if f.kind == "session_commented_out")
        assert "login to the app" in finding.detail

    def test_a_feature_that_signs_in_is_not_flagged(self):
        report = fidelity.analyze(SELF_CONTAINED)
        assert report.creates_session is True
        assert "no_session_of_its_own" not in [f.kind for f in report.findings]

    def test_no_login_at_all_is_still_reported(self):
        report = fidelity.analyze("Feature: f\n\n  Scenario: s\n    When search\n")
        assert "no_session_of_its_own" in [f.kind for f in report.findings]


class TestTagPlacement:
    def test_a_hook_tag_on_the_scenario_is_a_blocker(self):
        """Hooks read feature tags; an export puts labels on the scenario."""
        report = fidelity.analyze(TAGS_ON_SCENARIO, hook_tags=["skip_login"])
        assert "@skip_login" in report.scenario_tags
        assert "tags_at_scenario_level" in [f.kind for f in report.findings]
        assert report.faithful is False

    def test_the_same_tag_on_the_feature_is_fine(self):
        report = fidelity.analyze(SELF_CONTAINED, hook_tags=["skip_login"])
        assert "@skip_login" in report.feature_tags
        assert "tags_at_scenario_level" not in [f.kind for f in report.findings]

    def test_tags_the_hooks_do_not_read_are_not_flagged(self):
        report = fidelity.analyze(TAGS_ON_SCENARIO, hook_tags=["something_else"])
        assert "tags_at_scenario_level" not in [f.kind for f in report.findings]


class TestExecutionShape:
    def test_a_multi_scenario_execution_says_order_matters(self):
        report = fidelity.analyze(EXECUTION)
        assert report.scenarios == 2
        assert "shared_session" in [f.kind for f in report.findings]

    def test_a_lone_scenario_is_flagged_as_possibly_split_out(self):
        report = fidelity.analyze(SELF_CONTAINED)
        assert "single_scenario" in [f.kind for f in report.findings]

    def test_info_findings_do_not_make_it_unfaithful(self):
        """Order-dependence is worth saying; it is not a reason to refuse."""
        assert fidelity.analyze(SELF_CONTAINED).faithful is True
