from pathlib import Path

import pytest
from aitlc.config import AitlcConfig, ConfigError

TOML_TEXT = """
[project]
name = "myproject"
issue_key_prefix = "PROJ-"
feature_dir = "features"

[env]
lt_username = "LT_USERNAME"
lt_access_key = "LT_ACCESS_KEY"

[mobile]
mobile_feature_title_pattern = "Mobile browser:"
mobile_device_env_var = "DEVICE_NAME"
mobile_device_env_value = "MOBILE_DEVICE"

[lambdatest]
tunnel_name = "myproject-tunnel"
max_concurrent_sessions = 5
"""


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "aitlc.toml").write_text(TOML_TEXT)
    (tmp_path / "features").mkdir()
    (tmp_path / "features" / "PROJ-12345.feature").write_text("Feature: X\n")
    return tmp_path


def test_loads_project_fields(project_root: Path):
    config = AitlcConfig.load(project_root / "aitlc.toml")
    assert config.project_name == "myproject"
    assert config.issue_key_prefix == "PROJ-"
    assert config.feature_dir == "features"


def test_env_map_resolves_from_actual_env(project_root: Path, monkeypatch):
    monkeypatch.setenv("LT_USERNAME", "someuser")
    config = AitlcConfig.load(project_root / "aitlc.toml")
    assert config.env.resolve("lt_username") == "someuser"


def test_env_map_returns_none_when_unset(project_root: Path, monkeypatch):
    monkeypatch.delenv("LT_ACCESS_KEY", raising=False)
    config = AitlcConfig.load(project_root / "aitlc.toml")
    assert config.env.resolve("lt_access_key") is None


def test_require_env_raises_actionable_error(project_root: Path, monkeypatch):
    monkeypatch.delenv("LT_ACCESS_KEY", raising=False)
    config = AitlcConfig.load(project_root / "aitlc.toml")
    with pytest.raises(ConfigError, match="LT_ACCESS_KEY"):
        config.require_env("lt_access_key")


def test_require_env_raises_when_no_mapping(project_root: Path):
    config = AitlcConfig.load(project_root / "aitlc.toml")
    with pytest.raises(ConfigError, match="jira_token"):
        config.require_env("jira_token")


def test_resolve_feature_path_bare_id(project_root: Path):
    config = AitlcConfig.load(project_root / "aitlc.toml")
    result = config.resolve_feature_path("PROJ-12345")
    assert result is not None
    assert result.name == "PROJ-12345.feature"


def test_resolve_feature_path_nested_dir(project_root: Path):
    nested = project_root / "features" / "some-batch"
    nested.mkdir()
    (nested / "PROJ-99999.feature").write_text("Feature: Y\n")
    config = AitlcConfig.load(project_root / "aitlc.toml")
    result = config.resolve_feature_path("PROJ-99999")
    assert result is not None
    assert result.name == "PROJ-99999.feature"


def test_resolve_feature_path_missing_returns_none(project_root: Path):
    config = AitlcConfig.load(project_root / "aitlc.toml")
    assert config.resolve_feature_path("PROJ-00000") is None


def test_find_and_load_returns_default_when_no_config(tmp_path: Path):
    config = AitlcConfig.find_and_load(tmp_path)
    assert config.project_name == "project"  # default, not an error
