"""aitlc.toml loader — resolves project-specific env var names and settings.

Core code must never hardcode a project's env var names or feature-dir layout
(ARCHITECTURE.md §4). Everything project-specific comes from this config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


DEFAULT_CONFIG_NAMES = ("aitlc.toml", ".aitlc.toml")


class ConfigError(Exception):
    """Raised when aitlc.toml is missing a value a requested operation needs."""


@dataclass
class EnvMap:
    """Maps aitlc's generic credential/setting names to a project's actual env var names."""

    lt_username: str | None = None
    lt_access_key: str | None = None
    lt_proxy_host: str | None = None
    lt_proxy_port: str | None = None
    jira_email: str | None = None
    jira_token: str | None = None
    jira_xray_client_id: str | None = None
    jira_xray_client_secret: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_session_token: str | None = None
    teams_webhook_url: str | None = None

    def resolve(self, generic_name: str) -> str | None:
        """Return the actual env var *value* for a generic credential name, or None if unset."""
        env_var_name = getattr(self, generic_name, None)
        if not env_var_name:
            return None
        return os.environ.get(env_var_name)

    def known_secret_env_var_names(self) -> list[str]:
        """Return every configured env var name that holds a secret (for redaction)."""
        secret_fields = (
            "lt_access_key",
            "jira_token",
            "jira_xray_client_secret",
            "s3_secret_access_key",
            "s3_session_token",
            "teams_webhook_url",
        )
        return [getattr(self, f) for f in secret_fields if getattr(self, f, None)]


@dataclass
class MobileConfig:
    """How this project identifies and emulates mobile runs."""

    mobile_feature_title_pattern: str = "Mobile browser:"
    mobile_device_env_var: str = "DEVICE_NAME"
    mobile_device_env_value: str = "MOBILE_DEVICE"


@dataclass
class LambdaTestConfig:
    """Remote-execution settings for the LambdaTest platform."""

    tunnel_name: str | None = None
    max_concurrent_sessions: int = 5
    # Optional shell command template, run before a --remote invocation, in
    # config.root_dir. Its LAST LINE of stdout becomes the PLATFORM_ENVIRONMENT
    # env var. Real gap found building aitlc's --remote support: setting
    # TESTING_PLATFORM/TEST_TYPE/DEVICE_NAME alone is not sufficient for
    # every suite — some build a real capabilities list from their own
    # config module, which is project-specific enough that it belongs in
    # config rather than hardcoded into aitlc's adapter.
    # Supports {feature_name}/{platform_name}/{device_name} placeholders.
    platform_environment_command: str | None = None


@dataclass
class AitlcConfig:
    """Everything aitlc needs to know about the target project."""

    project_name: str = "project"
    issue_key_prefix: str = ""
    feature_dir: str = "features"
    step_dir: str = "features/steps"
    # Where a project's existing locator definitions live — used by
    # `aitlc record --suggest-steps` to
    # diff codegen's recorded selectors against what already exists.
    locators_dir: str = "config/web_locators"
    # "module.path:function" for the project's own per-scenario setup, run
    # with behave's (context, scenario) signature. A step slice executed
    # outside a real behave run gets no before_scenario at all, so without
    # this the per-scenario generated data those steps depend on is simply
    # absent — and the resulting failures surface several steps later,
    # looking like app bugs rather than missing setup.
    scenario_setup: str | None = None
    # 'module:Class' wrapping a Playwright Page, assigned to
    # context.browser for a step slice. Which class that is differs per
    # project; without one the raw Page is used.
    browser_actions: str | None = None
    # 'module:Class' exposing launch_local_mobile_browser_via_cdp(...).
    # Only needed to combine --mobile with --cdp-url.
    browser_factory: str | None = None
    xray_graphql_url: str = "https://xray.cloud.getxray.app/api/v2/graphql"
    # Base URL of the Jira Cloud instance itself (distinct from Xray's own
    # GraphQL endpoint above) — needed for FR-7's plain Jira Task creation,
    # which goes through Jira's own REST API, not Xray's plugin API.
    jira_server_url: str | None = None
    s3_bucket: str | None = None
    s3_region: str = "us-east-2"
    # A named AWS profile (including an SSO profile). When set it wins over
    # static keys, because a profile is resolved fresh on every call and
    # static keys in a file go stale without announcing it.
    s3_profile: str = ""
    # Real confirmed key shape for this project's daily HTML report:
    # {s3_report_prefix}/{name}... (S3Utility.upload_file_and_get_presigned_url).
    # Left blank by default since the exact folder naming is project-specific.
    s3_report_prefix: str = ""
    env: EnvMap = field(default_factory=EnvMap)
    mobile: MobileConfig = field(default_factory=MobileConfig)
    lambdatest: LambdaTestConfig = field(default_factory=LambdaTestConfig)
    root_dir: Path = field(default_factory=Path.cwd)

    @classmethod
    def find_and_load(cls, start_dir: Path | None = None) -> AitlcConfig:
        """Search upward from start_dir (default cwd) for aitlc.toml, load it.

        Returns a default (mostly-empty) config if none is found, rather than
        raising — a bare `aitlc doctor`/`aitlc run` with no config should still
        run generic checks instead of hard-failing.
        """
        current = (start_dir or Path.cwd()).resolve()
        for directory in [current, *current.parents]:
            for name in DEFAULT_CONFIG_NAMES:
                candidate = directory / name
                if candidate.is_file():
                    return cls.load(candidate)
        return cls()

    @classmethod
    def load(cls, path: Path) -> AitlcConfig:
        """Load configuration from a specific file."""
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls.from_dict(data, root_dir=path.parent)

    @classmethod
    def from_dict(cls, data: dict[str, Any], root_dir: Path) -> AitlcConfig:
        """Build a config from already-parsed TOML data."""
        project = data.get("project", {})
        env_data = data.get("env", {})
        mobile_data = data.get("mobile", {})
        lt_data = data.get("lambdatest", {})
        xray_data = data.get("xray", {})
        jira_data = data.get("jira", {})
        s3_data = data.get("s3", {})

        return cls(
            project_name=project.get("name", "project"),
            issue_key_prefix=project.get("issue_key_prefix", ""),
            feature_dir=project.get("feature_dir", "features"),
            step_dir=project.get("step_dir", "features/steps"),
            locators_dir=project.get("locators_dir", "config/web_locators"),
            scenario_setup=project.get("scenario_setup"),
            browser_actions=project.get("browser_actions"),
            browser_factory=project.get("browser_factory"),
            xray_graphql_url=xray_data.get(
                "graphql_url", "https://xray.cloud.getxray.app/api/v2/graphql"
            ),
            jira_server_url=jira_data.get("server_url"),
            s3_bucket=s3_data.get("bucket"),
            s3_region=s3_data.get("region", "us-east-2"),
            s3_profile=s3_data.get("profile", ""),
            s3_report_prefix=s3_data.get("report_prefix", ""),
            env=EnvMap(**env_data),
            mobile=MobileConfig(**mobile_data),
            lambdatest=LambdaTestConfig(**lt_data),
            root_dir=root_dir,
        )

    def require_env(self, generic_name: str) -> str:
        """Resolve a required credential; raise ConfigError with an actionable message if unset."""
        value = self.env.resolve(generic_name)
        if not value:
            env_var_name = getattr(self.env, generic_name, None)
            if not env_var_name:
                raise ConfigError(
                    f"aitlc.toml has no [env] mapping for '{generic_name}' — "
                    f'add e.g. `{generic_name} = "YOUR_ENV_VAR_NAME"` under [env].'
                )
            raise ConfigError(
                f"Environment variable '{env_var_name}' (mapped from '{generic_name}') "
                f"is not set."
            )
        return value

    def resolve_feature_path(self, test_id: str) -> Path | None:
        """Bare-ID -> feature file resolution: exact path, then prefixed, then recursive."""
        candidate = Path(test_id)
        if candidate.exists():
            return candidate

        feature_root = self.root_dir / self.feature_dir
        with_prefix = feature_root / test_id
        if with_prefix.exists():
            return with_prefix

        with_ext = feature_root / f"{test_id}.feature"
        if with_ext.exists():
            return with_ext

        name_only = feature_root / f"{Path(test_id).stem}.feature"
        if name_only.exists():
            return name_only

        stem = Path(test_id).stem
        matches = sorted(feature_root.rglob(f"{stem}.feature"))
        if matches:
            return matches[0]

        return None
