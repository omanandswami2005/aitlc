"""The pre-filter should know everything the suite's own skip check knows.

Nothing that should be skipped was ever executed -- the project's hooks fire
inside every behave process. The cost was that each of these cases spawned a
process to discover a skip that was written in the file.
"""

from __future__ import annotations

from aitlc.core import feature_select

FEATURE_LEVEL = "@skip_xray_test\nFeature: f\n\n  Scenario: s\n    When x\n"
SCENARIO_LEVEL = "Feature: f\n\n  @skip_xray_test\n  Scenario: s\n    When x\n"
ENV_SPECIFIC = "@skip_xray_test_stage\nFeature: f\n\n  Scenario: s\n    When x\n"
NOT_SKIPPED = "@Automation\nFeature: f\n\n  @TEST_PROJ-1\n  Scenario: s\n    When x\n"


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return path


class TestPlacement:
    def test_a_feature_level_skip_is_caught(self, tmp_path):
        [choice] = feature_select.select_features([_write(tmp_path, "a.feature", FEATURE_LEVEL)])
        assert choice.skipped_by == "skip_xray_test"

    def test_a_scenario_level_skip_is_caught_too(self, tmp_path):
        """A skip means 'do not run this' wherever it is written."""
        [choice] = feature_select.select_features([_write(tmp_path, "b.feature", SCENARIO_LEVEL)])
        assert choice.skipped_by == "skip_xray_test"

    def test_an_ordinary_file_is_not_skipped(self, tmp_path):
        [choice] = feature_select.select_features([_write(tmp_path, "c.feature", NOT_SKIPPED)])
        assert choice.skipped_by is None

    def test_feature_tags_still_exclude_scenario_tags(self, tmp_path):
        """Hooks read feature tags; that distinction must not blur."""
        [choice] = feature_select.select_features([_write(tmp_path, "d.feature", NOT_SKIPPED)])
        assert "Automation" in choice.tags
        assert "TEST_PROJ-1" not in choice.tags


class TestEnvironmentVariants:
    def test_an_environment_tag_is_caught(self, tmp_path):
        [choice] = feature_select.select_features([_write(tmp_path, "e.feature", ENV_SPECIFIC)])
        assert choice.skipped_by == "skip_xray_test_stage"

    def test_naming_the_environment_narrows_to_it(self, tmp_path):
        [choice] = feature_select.select_features(
            [_write(tmp_path, "f.feature", ENV_SPECIFIC)], environment="prod"
        )
        assert choice.skipped_by is None, "a stage-only skip must not apply on prod"

    def test_variants_cover_the_known_environments(self):
        variants = feature_select.skip_tag_variants("skip_xray_test")
        assert set(variants) >= {
            "skip_xray_test",
            "skip_xray_test_prod",
            "skip_xray_test_stage",
            "skip_xray_test_dev",
        }

    def test_a_named_environment_yields_only_its_own_variant(self):
        assert feature_select.skip_tag_variants("skip_xray_test", "stage") == [
            "skip_xray_test",
            "skip_xray_test_stage",
        ]


def test_extra_tags_are_honoured(tmp_path):
    path = _write(tmp_path, "g.feature", "@wip\nFeature: f\n\n  Scenario: s\n    When x\n")
    [choice] = feature_select.select_features([path], extra_skip_tags=["wip"])
    assert choice.skipped_by == "wip"


def test_skipped_files_are_still_returned_not_dropped(tmp_path):
    """Dropping them makes a mis-tagged file look like one never picked up."""
    paths = [
        _write(tmp_path, "h.feature", FEATURE_LEVEL),
        _write(tmp_path, "i.feature", NOT_SKIPPED),
    ]
    assert len(feature_select.select_features(paths)) == 2
