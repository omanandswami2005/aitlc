"""LT tunnel health check + restart (FR-9).

Extracted from real, repeated manual usage: the tunnel's
control connection silently degrades (`ERR::WS::CTRL::CONN::DWN`), and the
fix each time was the same three steps — kill, wait, relaunch with the same
--tunnelName. This automates exactly that sequence, plus the log-signature
health check already shared with `aitlc doctor` (core/doctor's tunnel check
and this module read the same signatures — kept here as the canonical
implementation, doctor imports from here rather than duplicating).
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

DEGRADATION_SIGNATURES = (
    "ERR::WS::CTRL::CONN::DWN",
    "ERR::CTRL::CONN::MAX::ATTEMPT",
    "tunnel is not running or disconnected",
)
HEALTHY_LAUNCH_MARKER = "You can start testing now"


@dataclass
class TunnelStatus:
    """Health of the local tunnel, with the evidence behind the verdict."""

    healthy: bool
    detail: str
    log_path: Path


def check_status(log_path: Path, tail_bytes: int = 4000) -> TunnelStatus:
    """Read the tunnel log for known degradation signatures.

    Process-alive is deliberately NOT checked here — a live process with a
    degraded control connection is exactly the confusing case this exists
    to catch (found live: several failures in a tight cluster,
    the process was still running the whole time).
    """
    if not log_path.exists():
        return TunnelStatus(False, f"No tunnel log at {log_path}", log_path)

    tail = log_path.read_text()[-tail_bytes:]
    for sig in DEGRADATION_SIGNATURES:
        if sig in tail:
            return TunnelStatus(False, f"Degradation signature found: {sig}", log_path)
    if HEALTHY_LAUNCH_MARKER in tail:
        return TunnelStatus(
            True, "Healthy — launch marker present, no degradation signature", log_path
        )
    return TunnelStatus(
        False,
        "No degradation signature, but also no healthy-launch marker — unclear state",
        log_path,
    )


def restart(
    binary_path: Path,
    username: str,
    access_key: str,
    tunnel_name: str,
    log_path: Path,
    *,
    poll_timeout_s: float = 20.0,
    poll_interval_s: float = 1.0,
) -> TunnelStatus:
    """Kill any running tunnel process, relaunch, poll until healthy or timeout.

    Matches the manual sequence this replaces: pkill -f the
    binary path, launch fresh with the same --tunnelName (must match what
    the project's LT config requests, or sessions won't route through it).
    """
    # nosec B607 - `pkill` via PATH is deliberate (location varies by
    # platform); the pattern is the configured binary path, not input.
    subprocess.run(  # nosec B607 - `pkill` via PATH is intentional
        ["pkill", "-f", str(binary_path)], check=False, capture_output=True
    )
    time.sleep(2)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w") as log_file:
        subprocess.Popen(
            [
                str(binary_path),
                "--user",
                username,
                "--key",
                access_key,
                "--tunnelName",
                tunnel_name,
            ],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    deadline = time.monotonic() + poll_timeout_s
    while time.monotonic() < deadline:
        status = check_status(log_path)
        if status.healthy:
            return status
        time.sleep(poll_interval_s)

    return check_status(log_path)
