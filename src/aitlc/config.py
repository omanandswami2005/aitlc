"""aitlc.toml loader — resolves project-specific env var names and settings.

Core code must never hardcode a project's env var names or feature-dir layout
(ARCHITECTURE.md §4). Everything project-specific comes from this config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from aitlc.core import workspace as workspace_module

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
class DebugConfig:
    """Defaults for the `debug` command group."""

    # `debug continue`'s final stdout summary can either repeat every
    # step's full record (captured_output/traceback/page_state -- already
    # shown once, live, via each step's own pretty line during the run) or
    # a compact one (step/status/duration/error only). Real complaint hit
    # live: the full form floods the terminal on a long `continue`, with
    # everything already shown once. Compact by default; the full record
    # is always in the journal (`aitlc journal list --last 1`) regardless
    # of this setting, so nothing is actually lost by defaulting compact.
    continue_output: str = "compact"

    # Caps on the LIVE pretty-printed view of a failed step (`next`/`retry`/
    # `continue`'s per-step stderr line), not the JSON reply or the journal
    # -- both always keep the complete text regardless of these. Real
    # complaint hit live: a step whose traceback chains two exceptions (a
    # caught Playwright TimeoutError re-raised as the project's own
    # AssertionError, e.g. `wait_until_audience_create_for_search`) prints
    # BOTH tracebacks end to end with no cap at all, unlike captured_output
    # which was already capped -- one failure's live view ran to hundreds
    # of lines. 0 disables the cap for a given field (print it whole).
    captured_output_pretty_chars: int = 2000
    traceback_pretty_chars: int = 1500


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
    # The env var this project's suite reads to attach Playwright to an
    # already-open Chrome over CDP instead of launching a fresh browser. When a
    # live debug Chrome exists (`aitlc cdp launch`, or a prior `run --debug`),
    # aitlc sets this var for run/paver/behave so the suite REUSES that browser
    # rather than paying full setup on every run. Named here, not hardcoded, so
    # a suite that reads a differently-named variable is one config line away.
    # The default is Playwright's common convention, PLAYWRIGHT_CDP_URL.
    playwright_cdp_env: str = "PLAYWRIGHT_CDP_URL"
    # Feature to run when a feature-running command (run / debug / steps /
    # preflight …) is given no test id or path. An explicit path (relative to
    # the project root, or absolute) wins; otherwise the first *.feature in
    # feature_dir is used. Lets a single-feature project just run `aitlc run`,
    # the way `paver run parallel` defaults to the folder.
    default_feature: str = ""
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
    # Directory under the project root that every artifact goes into. Empty
    # keeps the historical `reports/`. See core/workspace.py for precedence.
    workspace: str = ""
    # Real confirmed key shape for this project's daily HTML report:
    # {s3_report_prefix}/{name}... (S3Utility.upload_file_and_get_presigned_url).
    # Left blank by default since the exact folder naming is project-specific.
    s3_report_prefix: str = ""
    env: EnvMap = field(default_factory=EnvMap)
    mobile: MobileConfig = field(default_factory=MobileConfig)
    lambdatest: LambdaTestConfig = field(default_factory=LambdaTestConfig)
    debug: DebugConfig = field(default_factory=DebugConfig)
    root_dir: Path = field(default_factory=Path.cwd)
    # None means find_and_load() never found an aitlc.toml -- root_dir then
    # silently defaulted to plain cwd, which is NOT the project root a real
    # config would have resolved to. Real confusion hit live: `cdp list`/
    # `debug list` run from one directory up (a monorepo root, say) reported
    # "0 instances"/"0 sessions" with no hint that this was the wrong
    # directory rather than genuinely nothing tracked -- state tracked under
    # the REAL project's aitlc.toml was invisible from there. Distinct from
    # a legitimately no-config project (`aitlc doctor` etc. still work; this
    # field just lets a caller that cares warn instead of trusting an empty
    # result silently).
    config_path: Path | None = None

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
        # Real gap found live: this used to be a bare `cls()`, whose
        # `root_dir` default (`Path.cwd()`) ignores `start_dir` entirely --
        # an explicit `find_and_load(some_dir)` that finds nothing silently
        # reported the PROCESS's cwd as root_dir instead of the directory it
        # was actually told to search from.
        return cls(root_dir=current)

    def no_config_warning(self) -> str | None:
        """A caller-facing warning when this config is the bare fallback --
        no aitlc.toml was found anywhere above the directory it looked from,
        so `root_dir` is just wherever the command happened to run, not a
        real project root. None when a real config was loaded."""
        if self.config_path is not None:
            return None
        return (
            f"no aitlc.toml found above {self.root_dir} -- showing state for "
            "this bare directory, not necessarily your project (run from "
            "inside the project, or check you're not one level too high)"
        )

    @classmethod
    def load(cls, path: Path) -> AitlcConfig:
        """Load configuration from a specific file."""
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls.from_dict(data, root_dir=path.parent, config_path=path)

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], root_dir: Path, config_path: Path | None = None
    ) -> AitlcConfig:
        """Build a config from already-parsed TOML data."""
        project = data.get("project", {})
        workspace_module.set_config_default(project.get("workspace", ""))
        env_data = data.get("env", {})
        mobile_data = data.get("mobile", {})
        lt_data = data.get("lambdatest", {})
        debug_data = data.get("debug", {})
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
            playwright_cdp_env=project.get("playwright_cdp_env", "PLAYWRIGHT_CDP_URL"),
            default_feature=project.get("default_feature", ""),
            xray_graphql_url=xray_data.get(
                "graphql_url", "https://xray.cloud.getxray.app/api/v2/graphql"
            ),
            jira_server_url=jira_data.get("server_url"),
            s3_bucket=s3_data.get("bucket"),
            s3_region=s3_data.get("region", "us-east-2"),
            s3_profile=s3_data.get("profile", ""),
            workspace=project.get("workspace", ""),
            s3_report_prefix=s3_data.get("report_prefix", ""),
            env=EnvMap(**env_data),
            mobile=MobileConfig(**mobile_data),
            lambdatest=LambdaTestConfig(**lt_data),
            debug=DebugConfig(**debug_data),
            root_dir=root_dir,
            config_path=config_path,
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

    def resolve_default_feature(self) -> Path | None:
        """The feature to use when a command is given no test id / path.

        An explicit `[project].default_feature` (relative to the project root, or
        absolute) wins. Otherwise the first `*.feature` in `feature_dir` is used
        — directly-contained files first, then a recursive search — so a
        single-feature project needs no argument at all.
        """
        if self.default_feature:
            candidate = Path(self.default_feature)
            if not candidate.is_absolute():
                candidate = self.root_dir / self.default_feature
            return candidate if candidate.exists() else None

        feature_root = self.root_dir / self.feature_dir
        if not feature_root.exists():
            return None
        matches = sorted(feature_root.glob("*.feature")) or sorted(
            feature_root.rglob("*.feature")
        )
        return matches[0] if matches else None

    def default_feature_id(self) -> str | None:
        """The stem of the default feature, usable as a test id, or None.

        Returning the stem (not the path) means every downstream label — the
        session key, the lock, the workspace, `resolve_feature_path` — keeps
        working exactly as it does for an explicitly-named test.
        """
        feature = self.resolve_default_feature()
        return feature.stem if feature else None
