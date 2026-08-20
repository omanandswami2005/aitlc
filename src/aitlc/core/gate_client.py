"""Client for the gated behave runner's control socket.

The gate lives inside a real, paused behave process (`aitlc.runtime.runner`).
This is the aitlc-side counterpart: one connection per command, a single JSON
reply, newline-framed. Kept tiny and dependency-free so the protocol has one
definition both ends agree on.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path


class GateUnavailable(Exception):
    """Raised when the gate socket is not there or not answering."""


def _connect(socket_path: str | Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(socket_path))
    except OSError as exc:
        sock.close()
        raise GateUnavailable(f"gate socket {socket_path} not reachable: {exc}") from exc
    return sock


def request(socket_path: str | Path, cmd: str, **params) -> dict:
    """Send one command, return the parsed reply. Raises GateUnavailable if down."""
    sock = _connect(socket_path)
    try:
        sock.sendall((json.dumps({"cmd": cmd, **params}) + "\n").encode())
        payload = b""
        while not payload.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            payload += chunk
        if not payload.strip():
            raise GateUnavailable("gate closed the connection without replying")
        return json.loads(payload.decode())
    finally:
        sock.close()


def is_alive(socket_path: str | Path) -> bool:
    """True when the gate answers a status request."""
    try:
        request(socket_path, "status")
        return True
    except (GateUnavailable, OSError, json.JSONDecodeError):
        return False


def wait_until_parked(socket_path: str | Path, timeout_s: float = 120.0) -> dict:
    """Block until the gate is listening (setup finished), or time out.

    Setup for a real scenario runs logins and multi-second waits, so the socket
    does not appear immediately. Returns the first status reply.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            return request(socket_path, "status")
        except (GateUnavailable, OSError, json.JSONDecodeError):
            time.sleep(0.25)
    raise GateUnavailable(f"gate did not begin listening within {timeout_s}s")
