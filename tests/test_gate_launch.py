"""`await_park_or_exit` must detect a clean process exit promptly.

Real-bug regression (found live debugging a real suite): a behave process
that finished cleanly in under two minutes with zero failures — nothing to
park on — was reported by `run --debug` as `{"error": "did not finish or
fail within 1800.0s"}` a full 30 minutes later. Root cause: the old
`_pid_alive(pid)` check used `os.kill(pid, 0)`, which still succeeds against
a zombie process (the kernel keeps its PID entry until something reaps it
via `wait`/`waitpid`) — and nothing in the polling loop ever called
`.wait()`/`.poll()` on the child, since it only had a bare pid, not the
owning `Popen` object. So a process that had *already exited* was reported
"still alive" for the entire timeout window.

A real subprocess proves the fix — no fake/mock stands in for the process
being reaped, which is the exact mechanism the bug lived in.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from aitlc.core import gate_launch


def _short_socket() -> Path:
    return Path(tempfile.gettempdir()) / f"aitlc-gate-test-{uuid.uuid4().hex[:12]}.sock"


def test_await_park_or_exit_detects_a_clean_exit_promptly():
    """A process that exits immediately must be reported "exited" fast.

    Before the fix this looped silently until the full timeout because the
    exited child was never reaped, so `os.kill(pid, 0)` kept reporting it
    alive. A generous 20s timeout with a near-instant real assertion is what
    catches a regression back to that behavior.
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    socket_path = _short_socket()  # never created -- no gate to answer it

    started = time.time()
    outcome = gate_launch.await_park_or_exit(socket_path, proc, timeout_s=20.0)
    elapsed = time.time() - started

    assert outcome == "exited"
    assert elapsed < 5.0, (
        f"took {elapsed:.1f}s to notice the process had already exited -- "
        "the zombie-reaping fix regressed"
    )


def test_await_park_or_exit_times_out_on_a_genuinely_running_process():
    """A process still running past the deadline must report "timeout"."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    socket_path = _short_socket()

    try:
        outcome = gate_launch.await_park_or_exit(socket_path, proc, timeout_s=1.0)
        assert outcome == "timeout"
    finally:
        proc.kill()
        proc.wait()
