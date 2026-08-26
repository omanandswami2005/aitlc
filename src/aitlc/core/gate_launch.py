"""Shared machinery for launching a detached, gated behave process.

`debug start` parks at a step index chosen up front; `run --debug` parks
reactively, wherever a real failure actually happens (see
`runtime/runner.py`'s `AITLC_GATE_ON_FAILURE` mode). The two differ only in
which `AITLC_GATE_*` variables select that behaviour -- launching, logging
and polling for the park are identical, so that part lives here once rather
than drifting between two command modules.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
from pathlib import Path

from aitlc.core import behave_runner, gate_client, workspace
from aitlc.runtime import attach


def socket_path(root_dir: Path, test_id: str) -> Path:
    """A short, collision-free Unix socket path for this project + test.

    A Unix socket path is capped near 104 bytes by the OS, so it goes in the
    system temp dir with a hashed name rather than under a deeply nested
    checkout (which fails at bind()).
    """
    digest = hashlib.sha256(
        f"{Path(root_dir).resolve()}::{test_id}".encode()
    ).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"aitlc-gate-{digest}.sock"


def aitlc_src() -> Path:
    """The directory to put on PYTHONPATH so behave can import the runner."""
    return Path(attach.__file__).resolve().parent.parent.parent


def launch(
    config,
    *,
    feature: Path,
    line: int | None,
    cdp_url: str,
    socket_path_: Path,
    progress_path: Path,
    gate_env: dict,
    log_name: str,
    report_path: Path | None = None,
    tags: str | None = None,
    name_pattern: str | None = None,
    dry_run: bool = False,
) -> subprocess.Popen:
    """Launch a detached, gated behave process; return the Popen handle.

    `gate_env` carries whichever `AITLC_GATE_*` variables select the park
    behaviour for this call -- everything else about invoking behave is
    identical regardless of which mode is asked for. `report_path` is None
    for `debug start` (its result comes from the socket protocol, not a
    report file) and a real path for `run --debug` (still needs a
    structured JSON result on the branch where nothing fails -- that path
    also needs the real exit code once the process ends, hence returning
    the Popen handle rather than a bare pid: it's still our child even
    detached via `start_new_session`, so `.wait()` reaps it correctly).
    """
    behave_cmd = behave_runner.resolve_poetry() + ["run", "behave"]
    work_dir = workspace.output_path(config.root_dir, ".aitlc", "debug", "_gate_site")
    full_gate_env = {
        **gate_env,
        "AITLC_GATE_SOCKET": str(socket_path_),
        "AITLC_GATE_PROGRESS": str(progress_path),
        config.playwright_cdp_env: cdp_url,
    }
    plan = attach.plan(
        behave_cmd,
        config.root_dir,
        work_dir,
        aitlc_src=aitlc_src(),
        gate_env=full_gate_env,
    )
    cmd = behave_runner.build_command(
        feature,
        report_path,
        tags=tags,
        name_pattern=name_pattern,
        dry_run=dry_run,
        no_capture=True,
        stop=True,
        line=line,
        extra_args=plan.extra_args,
    )

    if socket_path_.exists():
        socket_path_.unlink()
    log_path = workspace.ensure(config.root_dir, ".aitlc", "debug", log_name)
    with log_path.open("ab") as handle:
        proc = subprocess.Popen(
            cmd,
            cwd=config.root_dir,
            env={**os.environ, **plan.env},
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    return proc


def await_park_or_exit(
    socket_path_: Path, proc: subprocess.Popen, timeout_s: float
) -> dict | str:
    """Wait for the gate to park, or the process to exit first.

    Returns the status dict once parked; `"exited"` if the process ended
    before parking (finished normally with no failure, a setup step failed
    and `behave --stop` aborted, or it crashed); `"timeout"` otherwise.

    Takes the `Popen` object, not a bare pid, and polls it with `.poll()`
    -- NOT an `os.kill(pid, 0)` liveness check. This process is the child's
    real parent (`start_new_session=True` puts it in a new session, but
    does not reparent it), so until something calls `.wait()`/`.poll()` on
    it, a child that has already exited sits as a zombie: `os.kill(pid, 0)`
    still succeeds against a zombie's PID (the kernel keeps the entry until
    reaped), so a pid-based check can never observe the exit at all. Found
    live: a real behave run that finished cleanly in under two minutes
    (0 failures, no park -- nothing to serve) was reported as
    `{"error": "did not finish or fail within 1800.0s"}` a full 30 minutes
    later, purely because nothing ever reaped it. `.poll()` performs the
    real non-blocking `waitpid` and returns the exit code the moment it is
    available, which both reaps the child and gives an accurate answer.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            return "exited"
        try:
            return gate_client.request(socket_path_, "status")
        except (gate_client.GateUnavailable, OSError):
            time.sleep(0.25)
    return "timeout"
