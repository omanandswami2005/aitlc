"""Own the lifecycle of a long-lived CDP debug Chrome.

Why this exists — three failure modes hit live while debugging a real
suite, none of which produce a useful error on their own:

1. **The browser dies with the shell that launched it.** Backgrounding
   Chrome from a shell (`chrome ... &`) ties it to that shell's process
   group; when the shell exits, Chrome goes with it. A later
   `connect_over_cdp` then fails with a bare `ECONNREFUSED`, which reads
   like a port/config problem rather than "the browser you started is
   gone". `launch()` fully detaches (`start_new_session=True`) so the
   instance genuinely outlives the command that started it.

2. **A pause-on-failure flag silently does nothing without a CDP URL.**
   A suite that implements its own pause typically gates it on both a
   pause flag AND `PLAYWRIGHT_CDP_URL` — set only the first and the run
   tears down normally, taking the failed page with it. Callers get
   `debug_env()` so both are always set together.

3. **Desktop-sized window for mobile runs.** A suite whose pre-scenario
   login runs *before* per-scenario device emulation applies needs its
   debug Chrome already mobile-sized at launch, or that login renders
   desktop and the mobile sign-in control is never found. `launch()`
   defaults to a mobile window size for exactly that reason.

State lives in a small JSON file so `status`/`stop` work from any shell,
not just the one that ran `launch`.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from aitlc.core import workspace

DEFAULT_PORT = 9333
# Matches the mobile window the debugging cycle for this project standardized
# on; see module docstring point 3 for why it is set at launch time.
DEFAULT_WINDOW_SIZE = "375,812"
# A real desktop run maximises the window; the debug browser must match it or
# the app renders its mobile layout and every desktop-nav step fails to find
# an element that is real but collapsed behind a hamburger. Confirmed live: a
# session parked correctly, signed in, on the right page, still could not
# click a nav item because the window was 500px wide.
DESKTOP_WINDOW_SIZE = "1920,1080"

_MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
_CHROME_CANDIDATES = (
    _MAC_CHROME,
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)


class ChromeCdpError(RuntimeError):
    """Raised for actionable Chrome-lifecycle problems."""


@dataclass
class CdpInstance:
    """A launched debug-Chrome instance, as persisted to the state file."""

    pid: int
    port: int
    user_data_dir: str
    started_at: float
    # What last drove this browser. A CDP attach reuses an existing browser
    # context -- the opposite of isolation -- so a profile accumulates sessions
    # from everything that has touched it. Once it holds a stale session, a run
    # fails at the project's own login and reads as a test bug rather than a
    # dirty profile. Recording this makes "used by something else" visible
    # before it costs a run.
    last_driven_by: str = ""
    driven_count: int = 0

    @property
    def cdp_url(self) -> str:
        """The URL a client uses to attach to this instance."""
        return f"http://127.0.0.1:{self.port}"


def state_path(root_dir: Path, port: int = DEFAULT_PORT) -> Path:
    """Where this port's instance state is recorded."""
    return workspace.output_path(root_dir, ".cdp", f"chrome-{port}.json")


def free_port() -> int:
    """Ask the OS for an unused TCP port.

    Binding to port 0 and reading back the assignment avoids the race a
    "scan for a closed port, then launch" loop has, where two concurrent
    launches can pick the same number before either binds it. There is
    still a small window between closing this socket and Chrome binding
    it, so callers should treat a launch failure as retryable rather than
    assuming the port is permanently taken.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def list_instances(root_dir: Path) -> list[dict]:
    """Every tracked instance with its live status.

    Reports dead-but-tracked entries too (`running: false`) — a stale state
    file is precisely what turns a later attach into an unexplained
    ECONNREFUSED, so it should be visible, not filtered out.
    """
    state_dir = workspace.output_path(root_dir, ".cdp")
    if not state_dir.exists():
        return []

    instances: list[dict] = []
    for path in sorted(state_dir.glob("chrome-*.json")):
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        port = int(raw.get("port", 0))
        if not port:
            continue
        version = probe(port)
        instances.append(
            {
                "port": port,
                "pid": raw.get("pid"),
                "running": version is not None,
                "cdp_url": f"http://127.0.0.1:{port}",
                "browser": version.get("Browser") if version else None,
                "user_data_dir": raw.get("user_data_dir"),
            }
        )
    return instances


def _looks_like_our_chrome(instance: CdpInstance) -> bool:
    """Check whether `instance.pid` is still our Chrome for this port.

    Guards against PID reuse: the OS recycles process IDs, so a state file left behind
    by a Chrome that exited hours ago can name a PID now owned by something unrelated.
    Checking the command line for both a browser binary and this instance's own
    `--remote-debugging-port` makes the match specific to the process we started.
    Returns False when the check cannot be performed at all, so an unverifiable PID is
    never killed.
    """
    try:
        # nosec B607 - `ps` is resolved via PATH deliberately: hardcoding
        # /bin/ps breaks on distros that place it elsewhere, and the
        # arguments are fully controlled (a integer PID we recorded).
        proc = subprocess.run(  # nosec B607 - `ps` via PATH is intentional
            ["ps", "-p", str(instance.pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    if proc.returncode != 0:
        return False  # no such process

    cmdline = proc.stdout.lower()
    looks_like_browser = "chrome" in cmdline or "chromium" in cmdline
    return looks_like_browser and f"--remote-debugging-port={instance.port}" in cmdline


def stop_all(root_dir: Path) -> list[int]:
    """Stop every tracked instance. Returns the ports acted on.

    Reconciles against reality first: the registry can go empty while browsers
    are still alive (a state file removed by hand, a crash mid-write), and
    reporting `count: 0` at someone who can see Chrome windows on screen is
    worse than useless.
    """
    stopped: list[int] = []
    for instance in list_instances(root_dir):
        port = instance["port"]
        if stop(root_dir, port=port):
            stopped.append(port)
    return stopped


def untracked_ports(root_dir: Path, candidate_ports: list[int]) -> list[int]:
    """Ports answering CDP that the registry does not know about."""
    tracked = {i["port"] for i in list_instances(root_dir)}
    return [p for p in candidate_ports if p not in tracked and probe(p) is not None]


def mark_driven(root_dir: Path, port: int, driver: str) -> CdpInstance | None:
    """Note that `driver` drove this browser, for the warning in `is_dirty_for`."""
    instance = load_state(root_dir, port=port)
    if instance is None:
        return None
    instance.last_driven_by = driver
    instance.driven_count += 1
    save_state(root_dir, instance)
    return instance


def is_dirty_for(root_dir: Path, port: int, driver: str) -> tuple[bool, str]:
    """Whether attaching `driver` to this profile is risky, and why.

    Not an error: a shared profile is fine when you know what is in it. The
    failure mode worth preventing is silent -- the run dies at the login step
    and the profile is the last thing anyone suspects.
    """
    instance = load_state(root_dir, port=port)
    if instance is None:
        return False, ""
    if instance.last_driven_by and instance.last_driven_by != driver:
        return True, (
            f"port {port} was last driven by {instance.last_driven_by!r} "
            f"({instance.driven_count} run(s)); a reused profile keeps its "
            "sessions and can fail this run at its own login. Use "
            "`aitlc cdp launch --new` for an isolated browser."
        )
    return False, ""


def find_chrome(explicit: str | None = None) -> str:
    """Locate a Chrome/Chromium binary, or raise with what was tried."""
    if explicit:
        if Path(explicit).exists() or shutil.which(explicit):
            return explicit
        raise ChromeCdpError(f"Chrome binary not found at '{explicit}'")

    for candidate in _CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    raise ChromeCdpError(
        "No Chrome/Chromium binary found. Tried: " + ", ".join(_CHROME_CANDIDATES)
    )


def probe(port: int = DEFAULT_PORT, timeout: float = 1.0) -> dict | None:
    """Return Chrome's /json/version payload, or None if nothing answers.

    This — not "is the PID alive" — is the real liveness test: a Chrome
    process can exist while its debugging port is not (yet) accepting
    connections, and that gap is exactly when an attach appears to fail
    for no reason.
    """
    try:
        # nosec B310 - the scheme is a literal "http://" against loopback,
        # not caller-supplied, so no file:/custom-scheme path exists here.
        with urllib.request.urlopen(  # nosec B310 - literal http:// loopback URL
            f"http://127.0.0.1:{port}/json/version", timeout=timeout
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


def load_state(root_dir: Path, port: int = DEFAULT_PORT) -> CdpInstance | None:
    """Load a tracked instance, or None if nothing is recorded."""
    path = state_path(root_dir, port)
    if not path.exists():
        return None
    try:
        return CdpInstance(**json.loads(path.read_text()))
    except (json.JSONDecodeError, TypeError, OSError):
        return None


def save_state(root_dir: Path, instance: CdpInstance) -> None:
    """Record a launched instance so other shells can find it."""
    path = state_path(root_dir, instance.port)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(instance), indent=2))


def clear_state(root_dir: Path, port: int = DEFAULT_PORT) -> None:
    """Forget a tracked instance."""
    state_path(root_dir, port).unlink(missing_ok=True)


def launch(
    root_dir: Path,
    *,
    port: int | None = DEFAULT_PORT,
    window_size: str | None = DEFAULT_WINDOW_SIZE,
    user_data_dir: Path | None = None,
    chrome_binary: str | None = None,
    wait_timeout: float = 20.0,
    reuse: bool = True,
) -> tuple[CdpInstance, bool]:
    """Start a detached debug Chrome, or reuse one already on `port`.

    Returns `(instance, reused)`. `reused=True` means a live instance was
    already answering on that port — the common case for a daily "always
    on" browser, and the reason this is safe to call before every run.

    `port=None` requests a genuinely **new, isolated** instance on an
    OS-assigned free port with its own profile directory. That is the mode
    for running several browsers at once (parallel suites, or multiple
    agents each needing their own browser): sharing one Chrome across
    concurrent scenarios interleaves their navigations and produces
    failures that look like app bugs. Isolated instances never reuse, so
    each caller gets a browser nobody else is driving.
    """
    if port is None:
        port = free_port()
        reuse = False
        if user_data_dir is None:
            # A distinct profile per instance — sharing one profile
            # directory across concurrent Chromes corrupts it, and shared
            # cookies would leak session state between parallel tests.
            user_data_dir = workspace.output_path(root_dir, ".cdp", f"profile-{port}")

    existing = probe(port)
    if existing is not None:
        if not reuse:
            raise ChromeCdpError(
                f"Something is already listening on port {port} "
                f"({existing.get('Browser', 'unknown')}). Stop it first or pick "
                f"another --port."
            )
        known = load_state(root_dir, port)
        if known is not None:
            return known, True
        # Answering but not ours (e.g. started by hand, or state file lost).
        # Still usable — record what we can rather than refusing to attach.
        instance = CdpInstance(
            pid=-1,
            port=port,
            user_data_dir="<external>",
            started_at=time.time(),
        )
        save_state(root_dir, instance)
        return instance, True

    binary = find_chrome(chrome_binary)
    data_dir = user_data_dir or workspace.output_path(root_dir, ".cdp", f"profile-{port}")
    data_dir.mkdir(parents=True, exist_ok=True)

    argv = [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={data_dir}",
        # This project's app calls a local service that Chrome's Local
        # Network Access checks block in a debug profile; without this the
        # app loads but key calls fail in a way that looks like an app bug.
        "--disable-features=LocalNetworkAccessChecks",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if window_size:
        argv.append(f"--window-size={window_size}")

    log_path = workspace.output_path(root_dir, ".cdp", f"chrome-{port}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab")

    popen_kwargs: dict = {
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "cwd": str(root_dir),
    }
    if platform.system() == "Windows":
        # CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
        popen_kwargs["creationflags"] = 0x00000200 | 0x00000008
    else:
        # The detach that makes this survive its parent shell (point 1).
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(argv, **popen_kwargs)

    deadline = time.time() + wait_timeout
    while time.time() < deadline:
        if probe(port) is not None:
            instance = CdpInstance(
                pid=proc.pid,
                port=port,
                user_data_dir=str(data_dir),
                started_at=time.time(),
            )
            save_state(root_dir, instance)
            return instance, False
        if proc.poll() is not None:
            raise ChromeCdpError(
                f"Chrome exited immediately (code {proc.returncode}). See {log_path}"
            )
        time.sleep(0.25)

    raise ChromeCdpError(
        f"Chrome did not open its debugging port within {wait_timeout:.0f}s. "
        f"See {log_path}"
    )


def stop(root_dir: Path, *, port: int = DEFAULT_PORT) -> bool:
    """Terminate the tracked instance. Returns True if something was stopped.

    Only kills when the recorded PID still looks like the Chrome we
    launched. PIDs are recycled by the OS, and a stale state file naming a
    long-dead Chrome could otherwise have us `killpg` an unrelated process
    group that inherited the number — killing a whole group of someone
    else's processes. Verifying the process identity first makes a stale
    entry a no-op instead of a destructive mistake.
    """
    instance = load_state(root_dir, port)
    stopped = False

    if instance is not None and instance.pid > 0 and _looks_like_our_chrome(instance):
        try:
            os.killpg(os.getpgid(instance.pid), signal.SIGTERM)
            stopped = True
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(instance.pid, signal.SIGTERM)
                stopped = True
            except (ProcessLookupError, PermissionError):
                stopped = False

    # Give it a moment, then confirm via the port rather than the PID —
    # the port is what callers actually depend on.
    deadline = time.time() + 5.0
    while time.time() < deadline and probe(port) is not None:
        time.sleep(0.2)

    clear_state(root_dir, port)
    return stopped or probe(port) is None


def debug_env(cdp_url: str, *, pause_on_failure: bool = True) -> dict[str, str]:
    """Env vars that make a run attach to `cdp_url` and freeze on failure.

    Both keys matter together: a suite's own pause hook requires the flag
    *and* PLAYWRIGHT_CDP_URL before it will halt, so setting one without
    the other silently loses the failed page (module docstring point 2).
    """
    env = {"PLAYWRIGHT_CDP_URL": cdp_url}
    if pause_on_failure:
        env["DEBUG_PAUSE_ON_FAILURE"] = "1"
    return env
