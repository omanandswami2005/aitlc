"""Tests for project detection behind `aitlc init`."""

from __future__ import annotations

from aitlc.core import init_config


def _mk(root, rel, text=""):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class TestFeatureDir:
    def test_finds_top_level_dir(self, tmp_path):
        _mk(tmp_path, "features/a.feature", "Feature: A\n")
        _mk(tmp_path, "features/suite/b.feature", "Feature: B\n")
        d = init_config.detect_feature_dir(tmp_path)
        # The shallowest hit is the suite root; deeper ones are subfolders.
        assert d.value == "features"
        assert d.confidence == "high"

    def test_none_when_absent(self, tmp_path):
        assert init_config.detect_feature_dir(tmp_path).value is None

    def test_ignores_vendored_trees(self, tmp_path):
        _mk(tmp_path, ".venv/lib/pkg/x.feature", "Feature: X\n")
        assert init_config.detect_feature_dir(tmp_path).value is None


class TestStepDir:
    def test_finds_dir_with_decorators(self, tmp_path):
        for name in ("a", "b", "c"):
            _mk(tmp_path, f"features/steps/{name}.py", "@given('x')\ndef f(): pass\n")
        d = init_config.detect_step_dir(tmp_path)
        assert d.value == "features/steps"
        assert d.confidence == "high"

    def test_single_module_is_only_medium_confidence(self, tmp_path):
        _mk(tmp_path, "steps/one.py", "@when('x')\ndef f(): pass\n")
        assert init_config.detect_step_dir(tmp_path).confidence == "medium"

    def test_plain_python_is_not_a_step_dir(self, tmp_path):
        _mk(tmp_path, "src/util.py", "def helper(): pass\n")
        assert init_config.detect_step_dir(tmp_path).value is None


class TestIssueKeyPrefix:
    def test_infers_from_filenames(self, tmp_path):
        _mk(tmp_path, "features/ABC-1.feature", "Feature: 1\n")
        _mk(tmp_path, "features/ABC-2.feature", "Feature: 2\n")
        assert init_config.detect_issue_key_prefix(tmp_path).value == "ABC-"

    def test_picks_the_dominant_prefix(self, tmp_path):
        for i in range(3):
            _mk(tmp_path, f"features/AAA-{i}.feature", "Feature: x\n")
        _mk(tmp_path, "features/ZZ-9.feature", "Feature: x\n")
        assert init_config.detect_issue_key_prefix(tmp_path).value == "AAA-"

    def test_plain_names_yield_nothing(self, tmp_path):
        _mk(tmp_path, "features/login.feature", "Feature: login\n")
        assert init_config.detect_issue_key_prefix(tmp_path).value is None


class TestScenarioSetup:
    def test_finds_hook_call(self, tmp_path):
        _mk(
            tmp_path,
            "features/environment.py",
            "from myproj.data import populate\n\n"
            "def before_scenario(context, scenario):\n"
            "    populate(context, scenario)\n",
        )
        d = init_config.detect_scenario_setup(tmp_path, "features")
        assert d.value == "myproj.data:populate"
        assert d.confidence == "high"

    def test_ignores_calls_with_too_few_args(self, tmp_path):
        # Behave's hook signature is (context, scenario); a one-arg call is
        # something else and would fail if named as the setup hook.
        _mk(
            tmp_path,
            "features/environment.py",
            "from myproj.data import reset\n\n"
            "def before_scenario(context, scenario):\n"
            "    reset(context)\n",
        )
        assert init_config.detect_scenario_setup(tmp_path, "features").value is None

    def test_ignores_locally_defined_functions(self, tmp_path):
        # Not importable as "module:function", so not usable as a spec.
        _mk(
            tmp_path,
            "features/environment.py",
            "def helper(context, scenario):\n    pass\n\n"
            "def before_scenario(context, scenario):\n    helper(context, scenario)\n",
        )
        assert init_config.detect_scenario_setup(tmp_path, "features").value is None

    def test_none_without_hook(self, tmp_path):
        _mk(tmp_path, "features/environment.py", "def before_all(context):\n    pass\n")
        assert init_config.detect_scenario_setup(tmp_path, "features").value is None


class TestEnvMap:
    def test_maps_names_only(self, tmp_path):
        _mk(
            tmp_path,
            ".env",
            "JIRA_TEST_TOKEN=supersecret\nLT_ACCESS_KEY=alsosecret\nUNRELATED=1\n",
        )
        mapping = init_config.detect_env_map(tmp_path)
        assert mapping["jira_token"] == "JIRA_TEST_TOKEN"
        assert mapping["lt_access_key"] == "LT_ACCESS_KEY"
        # Values must never travel with the mapping.
        assert "supersecret" not in str(mapping)
        assert "alsosecret" not in str(mapping)

    def test_missing_env_file_is_empty(self, tmp_path):
        assert init_config.detect_env_map(tmp_path) == {}

    def test_comments_and_blanks_ignored(self, tmp_path):
        _mk(tmp_path, ".env", "# JIRA_TEST_TOKEN=x\n\nLT_USERNAME=me\n")
        mapping = init_config.detect_env_map(tmp_path)
        assert "jira_token" not in mapping
        assert mapping["lt_username"] == "LT_USERNAME"


class TestRenderToml:
    def test_detected_values_are_written(self, tmp_path):
        _mk(tmp_path, "features/ABC-1.feature", "Feature: x\n")
        _mk(tmp_path, "features/steps/s.py", "@given('x')\ndef f(): pass\n")
        toml = init_config.render_toml(init_config.profile_project(tmp_path))
        assert 'feature_dir = "features"' in toml
        assert 'issue_key_prefix = "ABC-"' in toml

    def test_undetected_values_are_commented_placeholders(self, tmp_path):
        _mk(tmp_path, "features/plain.feature", "Feature: x\n")
        toml = init_config.render_toml(init_config.profile_project(tmp_path))
        # Visible as a gap to fill, never guessed into a wrong value.
        assert "# issue_key_prefix" in toml
        assert "# scenario_setup" in toml

    def test_output_is_valid_toml(self, tmp_path):
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib

        _mk(tmp_path, "features/ABC-1.feature", "Feature: x\n")
        _mk(tmp_path, "features/steps/s.py", "@then('x')\ndef f(): pass\n")
        toml = init_config.render_toml(init_config.profile_project(tmp_path))
        parsed = tomllib.loads(toml)
        assert parsed["project"]["feature_dir"] == "features"
        assert "graphql_url" in parsed["xray"]

    def test_unresolved_is_reported(self, tmp_path):
        _mk(tmp_path, "features/plain.feature", "Feature: x\n")
        profile = init_config.profile_project(tmp_path)
        assert "issue_key_prefix" in profile.unresolved
