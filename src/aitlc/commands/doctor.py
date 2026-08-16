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
from dataclasses import dataclass, field
from pathlib import Path

import typer
from aitlc.adapters.lambdatest import tunnel as tunnel_adapter
from aitlc.config import AitlcConfig
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

    typer.echo(json.dumps(report.to_dict(), indent=2))
    raise typer.Exit(code=0 if report.all_ok else 1)
