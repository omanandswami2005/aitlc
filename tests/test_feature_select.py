"""Tests for line specs, tag reading and feature discovery."""

from __future__ import annotations

from pathlib import Path

from aitlc.core.feature_select import (
    attach_line_spec,
    discover_features,
    feature_tags,
    select_features,
    split_line_spec,
)


class TestSplitLineSpec:
    def test_plain_id_has_no_line(self):
        assert split_line_spec("PROJ-24026") == ("PROJ-24026", None)

    def test_trailing_line_is_split(self):
        assert split_line_spec("PROJ-24026:30") == ("PROJ-24026", 30)

    def test_path_with_line_is_split(self):
        assert split_line_spec("features/a/b.feature:47") == (
            "features/a/b.feature",
            47,
        )

    def test_non_numeric_suffix_is_not_a_line(self):
        # Regression guard: a colon in a name must not be eaten as a line
        # spec, or the path would silently fail to resolve.
        assert split_line_spec("weird:name") == ("weird:name", None)

    def test_windows_drive_letter_survives(self):
        assert split_line_spec("C:/x/y.feature") == ("C:/x/y.feature", None)

    def test_windows_drive_with_real_line(self):
        assert split_line_spec("C:/x/y.feature:12") == ("C:/x/y.feature", 12)


class TestAttachLineSpec:
    def test_none_line_leaves_path_bare(self):
        assert attach_line_spec(Path("/a/b.feature"), None) == "/a/b.feature"

    def test_line_is_appended_in_behave_form(self):
        assert attach_line_spec(Path("/a/b.feature"), 30) == "/a/b.feature:30"

    def test_round_trips_with_split(self):
        rendered = attach_line_spec(Path("/a/b.feature"), 30)
        assert split_line_spec(rendered) == ("/a/b.feature", 30)


class TestFeatureTags:
    def _write(self, tmp_path: Path, text: str) -> Path:
        path = tmp_path / "x.feature"
        path.write_text(text)
        return path

    def test_reads_feature_level_tags(self, tmp_path):
        path = self._write(tmp_path, "@skip_xray_test @red\nFeature: X\n")
        assert feature_tags(path) == frozenset({"skip_xray_test", "red"})

    def test_ignores_scenario_level_tags(self, tmp_path):
        # Such hooks read feature.tags specifically; scenario tags must not
        # be mistaken for them (Xray exports Jira labels as scenario tags).
        path = self._write(tmp_path, "Feature: X\n\n  @skip_xray_test\n  Scenario: s\n")
        assert feature_tags(path) == frozenset()

    def test_no_tags_is_empty(self, tmp_path):
        assert feature_tags(self._write(tmp_path, "Feature: X\n")) == frozenset()

    def test_missing_file_is_empty_not_error(self, tmp_path):
        assert feature_tags(tmp_path / "nope.feature") == frozenset()


class TestDiscoverAndSelect:
    def _mk(self, root: Path, rel: str, text: str = "Feature: X\n") -> Path:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_recursive_finds_nested(self, tmp_path):
        self._mk(tmp_path, "top.feature")
        self._mk(tmp_path, "sub/nested.feature")
        found = discover_features(tmp_path, recursive=True)
        assert {p.name for p in found} == {"top.feature", "nested.feature"}

    def test_non_recursive_matches_paver_glob(self, tmp_path):
        self._mk(tmp_path, "top.feature")
        self._mk(tmp_path, "sub/nested.feature")
        found = discover_features(tmp_path, recursive=False)
        assert {p.name for p in found} == {"top.feature"}

    def test_results_are_sorted_for_reproducibility(self, tmp_path):
        for name in ("c.feature", "a.feature", "b.feature"):
            self._mk(tmp_path, name)
        found = discover_features(tmp_path)
        assert [p.name for p in found] == ["a.feature", "b.feature", "c.feature"]

    def test_skip_tag_excludes_but_still_reports(self, tmp_path):
        self._mk(tmp_path, "keep.feature")
        self._mk(tmp_path, "drop.feature", "@skip_xray_test\nFeature: X\n")
        selections = select_features(discover_features(tmp_path))

        selected = [s for s in selections if s.selected]
        skipped = [s for s in selections if not s.selected]

        assert [s.path.name for s in selected] == ["keep.feature"]
        # Reported, not silently dropped — otherwise "skipped by tag" is
        # indistinguishable from "never discovered".
        assert [s.path.name for s in skipped] == ["drop.feature"]
        assert skipped[0].skipped_by == "skip_xray_test"

    def test_skip_tag_can_be_disabled(self, tmp_path):
        self._mk(tmp_path, "drop.feature", "@skip_xray_test\nFeature: X\n")
        selections = select_features(discover_features(tmp_path), skip_tag=None)
        assert all(s.selected for s in selections)
