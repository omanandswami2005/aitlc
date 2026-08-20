"""Default-feature resolution: a feature-running command with no id/path picks
the project's feature (the sole *.feature in feature_dir, or an explicit
[project].default_feature)."""

from __future__ import annotations

from pathlib import Path

from aitlc.config import AitlcConfig


def _cfg(tmp_path: Path, toml: str = "") -> AitlcConfig:
    (tmp_path / "aitlc.toml").write_text(toml or '[project]\nname = "x"\n')
    return AitlcConfig.find_and_load(tmp_path)


def test_picks_the_sole_feature_in_feature_dir(tmp_path):
    (tmp_path / "features").mkdir()
    (tmp_path / "features" / "only.feature").write_text("Feature: f\n")
    config = _cfg(tmp_path)
    assert config.resolve_default_feature() == tmp_path / "features" / "only.feature"
    assert config.default_feature_id() == "only"


def test_none_when_no_feature_exists(tmp_path):
    (tmp_path / "features").mkdir()
    config = _cfg(tmp_path)
    assert config.resolve_default_feature() is None
    assert config.default_feature_id() is None


def test_first_alphabetically_when_multiple(tmp_path):
    (tmp_path / "features").mkdir()
    (tmp_path / "features" / "b.feature").write_text("Feature: b\n")
    (tmp_path / "features" / "a.feature").write_text("Feature: a\n")
    config = _cfg(tmp_path)
    assert config.default_feature_id() == "a"


def test_explicit_default_feature_wins(tmp_path):
    (tmp_path / "features").mkdir()
    (tmp_path / "features" / "first.feature").write_text("Feature: f\n")
    (tmp_path / "custom").mkdir()
    (tmp_path / "custom" / "chosen.feature").write_text("Feature: c\n")
    config = _cfg(
        tmp_path,
        '[project]\nname = "x"\ndefault_feature = "custom/chosen.feature"\n',
    )
    assert config.resolve_default_feature() == tmp_path / "custom" / "chosen.feature"
    assert config.default_feature_id() == "chosen"


def test_recurses_when_no_top_level_feature(tmp_path):
    nested = tmp_path / "features" / "sub"
    nested.mkdir(parents=True)
    (nested / "deep.feature").write_text("Feature: d\n")
    config = _cfg(tmp_path)
    assert config.default_feature_id() == "deep"
