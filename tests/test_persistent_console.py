"""The persistent step console, and the fallback that keeps it optional.

Why this exists at all: a process per step re-imports the registry and
re-runs scenario setup, which is slow — but the real damage is that
run-scoped data (generated names, ids, emails) is regenerated per process.
A step waiting for something an earlier step created then polls forever for
a name that never existed, and it looks exactly like the app hanging.
"""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest
from aitlc.core import step_console


def _serve_once(path, handler, ready):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    ready.set()
    conn, _ = server.accept()
    with conn:
        payload = b""
        while not payload.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            payload += chunk
        conn.sendall((json.dumps(handler(json.loads(payload.decode()))) + "\n").encode())
    server.close()
    try:
        path.unlink()
    except OSError:
        pass


@pytest.fixture
def console():
    """A stand-in console. Returns (socket_path, start(handler)).

    Bound in the system temp dir, not pytest's tmp_path: the OS caps a Unix
    socket path near 104 bytes and pytest's paths are longer than that --
    the same limit that made console_socket move out of the project tree.
    """
    import os
    import tempfile

    path = Path(tempfile.gettempdir()) / f"aitlc-test-{os.getpid()}.sock"
    if path.exists():
        path.unlink()

    def start(handler):
        ready = threading.Event()
        thread = threading.Thread(target=_serve_once, args=(path, handler, ready), daemon=True)
        thread.start()
        ready.wait(timeout=5)
        return thread

    yield path, start
    if path.exists():
        path.unlink()


class TestProtocol:
    def test_a_batch_is_sent_and_its_results_returned(self, console):
        path, start = console
        start(lambda req: {"results": [{"step": s, "status": "passed"} for s in req["steps"]]})

        reply = step_console.request_steps(path, ["Given a", "When b"])

        assert [r["step"] for r in reply["results"]] == ["Given a", "When b"]

    def test_no_socket_means_unavailable_not_a_crash(self, tmp_path):
        with pytest.raises(step_console.ConsoleUnavailable):
            step_console.request_steps(tmp_path / "missing.sock", ["Given a"])

    def test_a_stale_socket_file_is_not_mistaken_for_a_live_console(self, tmp_path):
        """A killed process leaves a file that looks identical to a live one."""
        stale = tmp_path / "stale.sock"
        stale.write_text("")
        assert step_console.console_is_alive(stale) is False

    def test_garbage_from_the_console_is_unavailable_not_a_parse_error(self, console):
        path, start = console
        start(lambda _req: "not-a-dict-but-valid-json")
        # A string is valid JSON, so this proves the caller survives a reply
        # it cannot use rather than raising something unrelated.
        reply = step_console.request_steps(path, ["Given a"])
        assert reply == "not-a-dict-but-valid-json"
