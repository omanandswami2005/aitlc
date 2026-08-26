"""`aitlc doctor` — preflight checks (FR-2).

Every check here traces to a real, previously-hit failure this project's
sessions that motivated this tool ran into, not speculative validation:
- FR-2.1: remote-run readiness (LT credentials, tunnel health, proxy vars).
- FR-2.2: the DEVICE_NAME-vs-mobile-feature-title mismatch that produced
  two false-looking failures and cost a real multi-minute device run to
  diagnose the first time.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import typer
from aitlc.adapters.lambdatest import tunnel as tunnel_adapter
from aitlc.config import AitlcConfig
from aitlc.core import behave_runner
from aitlc.core.dotenv import load_dotenv


@dataclass
class CheckResult:
    """One environment check and whether it passed."""

    name: str
    ok: bool
    detail: str


@dataclass
class DoctorReport:
    """The full set of environment checks for a run."""

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        """True when every check passed."""
        return all(c.ok for c in self.checks)

    def to_dict(self) -> dict:
        """Return a JSON-serializable form of this report."""
        return {
            "all_ok": self.all_ok,
            "checks": [
                {"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks
            ],
        }


def _check_env_var(
    config: AitlcConfig, generic_name: str, human_name: str
) -> CheckResult:
    value = config.env.resolve(generic_name)
    env_var_name = getattr(config.env, generic_name, None)
    if not env_var_name:
        return CheckResult(
            human_name, False, f"No [env].{generic_name} mapping in aitlc.toml"
        )
    if not value:
        return CheckResult(human_name, False, f"{env_var_name} is not set")
    return CheckResult(human_name, True, f"{env_var_name} is set")


def _check_tunnel_health(log_path: Path) -> CheckResult:
    """Report whether the tunnel is genuinely healthy.

    FR-2.1 / FR-9.1: process-alive is not sufficient — delegates to the
    canonical check in adapters/lambdatest/tunnel.py (shared with
    `aitlc tunnel status`) rather than duplicating the signature list.
    """
    status = tunnel_adapter.check_status(log_path)
    return CheckResult("LT tunnel", status.healthy, status.detail)


def _target_env_versions(root_dir: Path) -> dict | None:
    """Resolve behave/playwright versions from the TARGET PROJECT's own environment.

    Real bug found live: `run`/`debug` invoke behave as a subprocess in the
    TARGET project's own environment (`resolve_poetry() + ["run", "behave", ...]`
    in `behave_runner.build_command`) -- a separate Python environment from
    whatever aitlc itself happens to be installed under. On a pip-installed
    aitlc (the common case, not a project-local editable install) these two
    environments routinely differ: measured live, aitlc's own venv reported
    behave 1.3.3 while the target project's real poetry venv -- the one that
    actually runs every step -- had 1.2.7.dev2. Reporting the former as "the"
    behave version silently describes an environment that never executes a
    single step; `doctor`'s whole point (G24) is to say what will ACTUALLY
    run, so this asks the project's own `poetry run python` directly instead
    of trusting `importlib.metadata` in aitlc's own process.

    Returns `None` (not an empty dict) if the probe itself couldn't run at
    all (no poetry, no python) -- the caller falls back to aitlc's own
    versions in that case, since "unknown" is worse than "aitlc's own, which
    may differ" when there's truly nothing else to go on.
    """
    probe = (
        "import json\n"
        "from importlib.metadata import version, PackageNotFoundError\n"
        "out = {}\n"
        "for pkg in ('behave', 'playwright'):\n"
        "    try:\n"
        "        out[pkg] = version(pkg)\n"
        "    except PackageNotFoundError:\n"
        "        out[pkg] = 'not installed'\n"
        "print(json.dumps(out))\n"
    )
    cmd = behave_runner.resolve_poetry() + ["run", "python3", "-c", probe]
    try:
        proc = subprocess.run(
            cmd, cwd=root_dir, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return None


def _versions(root_dir: Path) -> dict:
    """Report versions and, where a capability has a fallback, which branch runs.

    Two capabilities here silently degrade with the installed Playwright: the
    accessibility snapshot (`aria_snapshot`, else the CDP AX tree, which ignores
    a text query) and CDP-attached emulation. Neither announces itself, so a
    flag can be accepted and ignored. One line here replaces reading the
    installed source to find out why.
    """
    info: dict = {}
    try:
        from importlib.metadata import version

        info["aitlc"] = version("aitlc")
    except Exception:  # noqa: BLE001 - a missing dist must not fail doctor
        info["aitlc"] = "unknown"

    target = _target_env_versions(root_dir)
    if target is not None:
        info["behave"] = target.get("behave", "not installed")
        info["playwright"] = target.get("playwright", "not installed")
        info["versions_from"] = "target project's own environment"
    else:
        # Fallback: aitlc's own environment, explicitly labeled as such so
        # this is never mistaken for what the suite itself actually runs.
        try:
            from importlib.metadata import version

            info["playwright"] = version("playwright")
        except Exception:  # noqa: BLE001
            info["playwright"] = "not installed"
        try:
            from importlib.metadata import version

            info["behave"] = version("behave")
        except Exception:  # noqa: BLE001
            info["behave"] = "not installed"
        info["versions_from"] = "aitlc's own environment (target project probe failed)"

    aria = False
    try:
        from playwright.sync_api import Locator

        aria = hasattr(Locator, "aria_snapshot")
    except Exception:  # noqa: BLE001
        aria = False
    info["a11y_path"] = (
        "aria_snapshot (--a11y-query filters)"
        if aria
        else "cdp-nodes fallback (--a11y-query is IGNORED on this path)"
    )
    return info


def _check_config_preconditions(config) -> list[CheckResult]:
    """Flag config a later command will need but cannot detect for itself.

    `browser_actions` and `browser_factory` are deliberately not auto-detected,
    which is defensible -- but the consequence is that `steps run --mobile
    --cdp-url` cannot work without them, and the only notice is an empty result.
    Saying so here is cheaper than discovering it mid-debug.
    """
    checks = []
    checks.append(
        CheckResult(
            "browser_actions configured",
            bool(getattr(config, "browser_actions", None)),
            (
                "set"
                if getattr(config, "browser_actions", None)
                else "unset — steps run cannot build the project driver"
            ),
        )
    )
    checks.append(
        CheckResult(
            "browser_factory configured",
            bool(getattr(config, "browser_factory", None)),
            (
                "set"
                if getattr(config, "browser_factory", None)
                else "unset — steps run --mobile with --cdp-url will refuse"
            ),
        )
    )
    return checks


def _check_device_mobile_mismatch(
    config: AitlcConfig, feature_path: Path | None
) -> CheckResult:
    """FR-2.2 — the specific, previously-real false failure."""
    if feature_path is None or not feature_path.exists():
        return CheckResult(
            "Mobile viewport config", True, "No feature file given, skipping"
        )

    text = feature_path.read_text()
    first_line = text.splitlines()[0] if text.splitlines() else ""
    is_mobile_feature = config.mobile.mobile_feature_title_pattern in first_line

    device_env_value = os.environ.get(config.mobile.mobile_device_env_var, "")
    device_matches = device_env_value == config.mobile.mobile_device_env_value

    if is_mobile_feature and not device_matches:
        return CheckResult(
            "Mobile viewport config",
            False,
            f"Feature title indicates a mobile scenario "
            f"('{config.mobile.mobile_feature_title_pattern}') but "
            f"{config.mobile.mobile_device_env_var}="
            f"{device_env_value or '<unset>'} "
            f"(expected {config.mobile.mobile_device_env_value}). "
            "This produces a false-looking failure, not a real bug — "
            "the scenario runs at desktop width instead of mobile.",
        )
    return CheckResult("Mobile viewport config", True, "No mismatch detected")


def doctor(
    feature: str | None = typer.Argument(
        None,
        help="Optional test ID/feature path to run the mobile-mismatch check against.",
    ),
    tunnel_log: str | None = typer.Option(
        "/tmp/lt_tunnel.log",  # nosec B108 - matches the LT binary's own default
        help="Path to the LT tunnel log for health checking.",
    ),
    remote: bool = typer.Option(
        False,
        "--remote",
        help="Also run remote-readiness checks (LT creds, tunnel, proxy).",
    ),
    env_file: str = typer.Option(
        ".env", "--env-file", help="Load env vars from this file before checking."
    ),
) -> None:
    """Check that the environment can run tests."""
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)
    report = DoctorReport()

    feature_path = config.resolve_feature_path(feature) if feature else None
    report.checks.append(_check_device_mobile_mismatch(config, feature_path))
    report.checks.extend(_check_config_preconditions(config))

    if remote:
        report.checks.append(_check_env_var(config, "lt_username", "LT_USERNAME"))
        report.checks.append(_check_env_var(config, "lt_access_key", "LT_ACCESS_KEY"))
        report.checks.append(_check_tunnel_health(Path(tunnel_log)))

        lt_proxy_host = config.env.resolve("lt_proxy_host")
        lt_proxy_port = config.env.resolve("lt_proxy_port")
        if not lt_proxy_host or not lt_proxy_port:
            report.checks.append(
                CheckResult(
                    "LT_PROXY_HOST/PORT",
                    False,
                    "Not set — any internal (.intra./NTLM) API call made under "
                    "TESTING_PLATFORM=LAMBDATEST will crash with InvalidProxyURL. "
                    "Only relevant if the target test provisions internal users.",
                )
            )
        else:
            report.checks.append(CheckResult("LT_PROXY_HOST/PORT", True, "Both set"))

    payload = report.to_dict()
    payload["versions"] = _versions(config.root_dir)
    typer.echo(json.dumps(payload, indent=2))
    raise typer.Exit(code=0 if report.all_ok else 1)


# Mounted by commands/_registry.py.
COMMAND = {"name": "doctor", "attr": "doctor", "order": 20}