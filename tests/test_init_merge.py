"""Re-running `init` must not discard the edits that made the file correct."""

from __future__ import annotations

import json

from aitlc.commands import init_cmd
from aitlc.core import init_config
from typer.testing import CliRunner

runner = CliRunner()


def _first_json(output: str) -> dict:
    """The first JSON document in captured output.

    The test runner merges stderr into stdout, and `init` writes an advisory
    note about undetected settings to stderr. Real stdout is a single clean
    document -- verified by running the command with stderr discarded -- so
    the test reads the first document rather than the code moving a warning
    that is correctly on stderr.
    """
    return json.JSONDecoder().raw_decode(output.lstrip())[0]

EXISTING = """[project]
feature_dir = "features/mine"
step_dir = "features/steps"

[s3]
bucket = "my-bucket"
"""

GENERATED = """[project]
feature_dir = "features/detected"
step_dir = "features/steps"
issue_prefix = "PROJ"

[s3]
bucket = "detected-bucket"
region = "us-east-2"
"""


class TestMerge:
    def test_hand_edited_values_are_never_replaced(self):
        merged, added = init_config.merge_toml(EXISTING, GENERATED)
        assert 'feature_dir = "features/mine"' in merged
        assert "features/detected" not in merged
        assert 'bucket = "my-bucket"' in merged

    def test_newly_detected_keys_are_added(self):
        merged, added = init_config.merge_toml(EXISTING, GENERATED)
        assert 'issue_prefix = "PROJ"' in merged
        assert 'region = "us-east-2"' in merged
        assert any("issue_prefix" in key for key in added)

    def test_an_added_key_lands_in_its_own_section(self):
        """Appending at the end would file it under whichever section is last."""
        merged, _ = init_config.merge_toml(EXISTING, GENERATED)
        lines = [line.strip() for line in merged.splitlines()]
        project_at = lines.index("[project]")
        s3_at = lines.index("[s3]")
        issue_at = next(i for i, line in enumerate(lines) if line.startswith("issue_prefix"))
        region_at = next(i for i, line in enumerate(lines) if line.startswith("region"))
        assert project_at < issue_at < s3_at, "issue_prefix belongs to [project]"
        assert region_at > s3_at, "region belongs to [s3]"

    def test_a_commented_placeholder_counts_as_unset(self):
        """`init` writes placeholders when it cannot detect; a later run should fill them."""
        existing = '[project]\n# issue_prefix = ""\n'
        merged, added = init_config.merge_toml(existing, '[project]\nissue_prefix = "PROJ"\n')
        assert 'issue_prefix = "PROJ"' in merged
        assert added

    def test_nothing_new_means_no_change(self):
        merged, added = init_config.merge_toml(EXISTING, EXISTING)
        assert added == []
        assert merged == EXISTING


class TestCommand:
    @staticmethod
    def _app():
        from typer import Typer

        app = Typer()
        app.command()(init_cmd.init)
        return app

    def test_existing_file_is_not_clobbered_without_a_flag(self, tmp_path):
        (tmp_path / "aitlc.toml").write_text(EXISTING)

        result = runner.invoke(self._app(), ["--root", str(tmp_path)])

        assert result.exit_code == 2, result.output
        assert (tmp_path / "aitlc.toml").read_text() == EXISTING
        assert "--merge" in _first_json(result.output)["hint"]

    def test_merge_keeps_edits_and_reports_what_it_added(self, tmp_path):
        (tmp_path / "aitlc.toml").write_text(EXISTING)

        result = runner.invoke(self._app(), ["--root", str(tmp_path), "--merge"])

        assert result.exit_code == 0, result.output
        written = (tmp_path / "aitlc.toml").read_text()
        assert 'feature_dir = "features/mine"' in written, "the hand edit survived"
        payload = _first_json(result.output)
        assert payload["merged"] is True

    def test_merge_and_force_together_are_refused(self, tmp_path):
        """They mean opposite things; silently picking one would be a trap."""
        (tmp_path / "aitlc.toml").write_text(EXISTING)

        result = runner.invoke(self._app(), ["--root", str(tmp_path), "--merge", "--force"])

        assert result.exit_code == 2
        assert "mutually exclusive" in _first_json(result.output)["error"]
