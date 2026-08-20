"""A behave Runner subclass that adds aitlc's instrumentation.

Loaded through behave's own `--runner-class` / `--runner` option, so the
target project keeps its `environment.py` exactly as it is: this class
calls `super().run_hook(...)`, meaning every project hook still runs,
unchanged and in order.

Two modes, both opt-in via environment variables. A project that sets none
of them gets a runner behaviourally identical to behave's own.

1. Observe / halt (AITLC_PAUSE_ON_FAILURE, AITLC_EVENTS)
   Emits a JSON-lines event per step and, on a failed step, halts before
   teardown so the browser stays on the failure. This is what `aitlc run
   --debug` uses.

2. Gate (AITLC_GATE=1, AITLC_GATE_SOCKET, AITLC_GATE_AT, AITLC_GATE_PROGRESS)
   Turns a real behave run into a paused, single-steppable session. behave's
   own loop runs before_all/before_feature/before_scenario and the setup
   steps [0, AITLC_GATE_AT) -- real hooks, real Context, real run-scoped
   data, real browser -- then, at the park step, this runner TAKES OVER:
   it listens on a Unix socket and advances the scenario one real step at a
   time on request, re-running the exact behave Step objects (so tables,
   docstrings and Examples binding are behave's, never reconstructed).

   The point of mode 2 is that there is nothing to reconstruct and therefore
   nothing to diverge: the fast loop IS behave, merely paused. See
   ROADMAP/`aitlc-gaps.md` for the class of bug this removes.

This module is imported inside the TARGET project's interpreter, not
aitlc's, so it imports nothing from aitlc and relies only on the stdlib
plus behave itself (guaranteed present: behave is what loaded it).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time
from datetime import datetime, timezone
from typing import Any

try:
    from behave.runner import Runner as _BaseRunner
except ImportError:  # pragma: no cover - only when behave is absent
    _BaseRunner = object


def _status_name(obj: Any) -> str | None:
    """Return a behave status as a plain string, whatever its type."""
    status = getattr(obj, "status", None)
    return getattr(status, "name", None) or (str(status) if status else None)


def _stamp(epoch: float) -> str:
    """UTC ISO-8601 for a wall-clock instant."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _find_page(context: Any) -> Any:
    """Best-effort lookup of a Playwright Page on the behave context."""
    for name in ("page", "primary_page", "current_page"):
        page = getattr(context, name, None)
        if page is not None and hasattr(page, "url"):
            return page
    for holder_name in ("browser", "driver", "primary_browser"):
        holder = getattr(context, holder_name, None)
        if holder is None:
            continue
        for attr in ("page", "_page"):
            page = getattr(holder, attr, None)
            if page is not None and hasattr(page, "url"):
                return page
    return None


def _emit(path: str, record: dict) -> None:
    """Append one JSON-lines event, never raising into the test run."""
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        pass


class AitlcRunner(_BaseRunner):
    """behave Runner that observes hooks, can halt on failure, or gate steps."""

    # ------------------------------------------------------------------ hooks
    def run_hook(self, name: str, *args: Any) -> Any:
        """Run the project's hook first, then apply aitlc's instrumentation.

        behave changed run_hook's signature across versions: older builds pass
        the context as the first argument (`run_hook(name, context, *args)`),
        1.3+ passes only the hook args and keeps context on the runner
        (`run_hook(name, *args)`). To work on both, *args is forwarded to super
        untouched, and aitlc derives context from `self.context`, stripping a
        leading context arg if an older behave supplied one.
        """
        result = super().run_hook(name, *args)

        # A side-run (retry/next) re-enters run_hook via Step.run; the project
        # hooks above must still fire, but the gate/observe logic below must
        # not, or it would recurse. This flag is the guard.
        if getattr(self, "_aitlc_suspended", False):
            return result

        context = getattr(self, "context", None)
        payload = list(args)
        if payload and context is not None and payload[0] is context:
            payload = payload[1:]

        if self._gate_active():
            self._gate_run_hook(name, context, *payload)
            return result

        self._observe_run_hook(name, context, *payload)
        return result

    # ------------------------------------------------------------ observe mode
    def _observe_run_hook(self, name: str, context: Any, *args: Any) -> None:
        if name != "after_step" or not args:
            return
        step = args[0]
        status = _status_name(step)

        events_path = os.environ.get("AITLC_EVENTS")
        if events_path:
            _emit(
                events_path,
                {
                    "event": "step",
                    "name": getattr(step, "name", None),
                    "keyword": getattr(step, "keyword", None),
                    "status": status,
                    "duration_s": round(getattr(step, "duration", 0.0) or 0.0, 3),
                    "location": str(getattr(step, "location", "")),
                    "ts": time.time(),
                },
            )

        if status == "failed" and os.environ.get("AITLC_PAUSE_ON_FAILURE"):
            self._halt_for_inspection(context, step)

    def _halt_for_inspection(self, context: Any, step: Any) -> None:
        """Stop the process immediately, leaving the browser usable.

        `os._exit` is deliberate: a normal exception or `sys.exit` would unwind
        into behave's after_scenario/after_all hooks, where a suite closes its
        browser -- destroying the one thing worth inspecting.
        """
        page = _find_page(context)
        url = None
        if page is not None:
            try:
                url = page.url
            except Exception:  # noqa: BLE001
                url = None

        payload = {
            "event": "paused_on_failure",
            "step": getattr(step, "name", None),
            "error": str(getattr(step, "error_message", "") or "")[:500],
            "page_url": url,
            "hint": (
                "Process halted before teardown; the browser is still open. "
                "Attach with: aitlc cdp inspect --cdp-url $PLAYWRIGHT_CDP_URL"
            ),
        }
        events_path = os.environ.get("AITLC_EVENTS")
        if events_path:
            _emit(events_path, payload)
        logging.warning("aitlc paused on failure: %s", json.dumps(payload))
        try:
            sys.stderr.write(json.dumps(payload) + "\n")
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
        os._exit(1)

    # --------------------------------------------------------------- gate mode
    def _gate_active(self) -> bool:
        return os.environ.get("AITLC_GATE") == "1"

    def _gate_at(self) -> int:
        try:
            return int(os.environ.get("AITLC_GATE_AT", "0") or "0")
        except ValueError:
            return 0

    def _gate_example(self) -> int:
        try:
            return int(os.environ.get("AITLC_GATE_EXAMPLE", "0") or "0")
        except ValueError:
            return 0

    def _gate_run_hook(self, name: str, context: Any, *args: Any) -> None:
        # before_step is dispatched through run_hook unconditionally
        # (behave/model.py Step.run). before_scenario is NOT, unless the project
        # defines it (run_hook_with_capture short-circuits a missing hook), so
        # the steps are collected here, lazily, from the live context.scenario.
        if name != "before_step":
            return

        if not getattr(self, "_aitlc_steps", None):
            scenario = getattr(context, "scenario", None)
            self._aitlc_steps = self._collect_steps(scenario)
            if not hasattr(self, "_aitlc_auto"):
                self._aitlc_auto = 0
                self._write_progress(state="running", done=0)

        # Let behave auto-run the setup steps [0, park) through its OWN loop,
        # so before/after_step hooks and capture are exactly a real run's.
        auto = getattr(self, "_aitlc_auto", 0)
        if auto < self._gate_at():
            self._aitlc_auto = auto + 1
            self._write_progress(state="running", done=auto + 1)
            return

        # Reached the park step. Take over: this call never returns to behave's
        # loop (the loop stays suspended here, holding the live Context and the
        # open browser); stepping from now on is driven over the socket.
        self._serve(context)

    def _collect_steps(self, scenario: Any) -> list:
        """The scenario's steps in run order (background first), as behave objects."""
        if scenario is None:
            return []
        steps = getattr(scenario, "all_steps", None)
        if steps is None:
            steps = getattr(scenario, "steps", [])
        return list(steps)

    def _feature_path(self, context: Any) -> str | None:
        feature = getattr(context, "feature", None)
        return getattr(feature, "filename", None) or getattr(
            getattr(context, "scenario", None), "filename", None
        )

    def _reparse_steps(self, context: Any) -> list | None:
        """Re-parse the feature file; return the target scenario's bound steps.

        Uses behave's own parser and example binding (`walk_scenarios`), so a
        Gherkin edit is picked up with full fidelity -- the new Step objects
        carry behave's own tables, docstrings and example-substituted text. This
        is what lets `retry`/`next` reflect a feature-file edit without a fresh
        `debug start`; the debugging loop is mostly Gherkin edits, so paying a
        restart per edit would defeat the point.
        """
        try:
            from behave.parser import parse_file
        except Exception:  # noqa: BLE001
            return None
        filename = self._feature_path(context)
        if not filename:
            return None
        try:
            parsed = parse_file(str(filename))
            scenarios = list(parsed.walk_scenarios()) if parsed else []
        except Exception:  # noqa: BLE001 - a mid-edit parse error must not kill the session
            return None
        if not scenarios:
            return None
        running_name = getattr(getattr(context, "scenario", None), "name", "") or ""
        exact = [s for s in scenarios if getattr(s, "name", "") == running_name]
        if exact:
            candidates = exact
        else:
            base = running_name.split(" -- ")[0]
            candidates = [
                s for s in scenarios if getattr(s, "name", "").split(" -- ")[0] == base
            ] or scenarios
        example = self._gate_example()
        match = candidates[example] if len(candidates) > example else candidates[0]
        try:
            return list(match.all_steps)
        except Exception:  # noqa: BLE001
            return None

    def _reload_feature(self, context: Any, cursor: int) -> dict | None:
        """Swap in freshly-parsed steps, following the cursor by text.

        Cost control: the feature is only re-parsed when its mtime changed since
        the last check, so a `next`/`retry` with no edit pays nothing (a stat,
        not a parse). The one step after an edit pays the parse -- single-digit
        ms for a normal feature, ~90 ms for a very large Scenario Outline, both
        negligible next to the step's own work.

        Returns None when there is nothing to change (so the caller reports no
        noise); otherwise updates ``self._aitlc_steps`` and reports what moved.
        The cursor follows the step it was on by TEXT, not index.
        """
        filename = self._feature_path(context)
        if filename:
            try:
                mtime = os.stat(filename).st_mtime
            except OSError:
                mtime = None
            if mtime is not None and mtime == getattr(self, "_aitlc_feature_mtime", None):
                return None  # unchanged since last check -- no parse, no cost
            self._aitlc_feature_mtime = mtime

        new_steps = self._reparse_steps(context)
        if new_steps is None:
            return None
        old_steps = getattr(self, "_aitlc_steps", [])
        old_texts = [self._step_text(s) for s in old_steps]
        new_texts = [self._step_text(s) for s in new_steps]
        if new_texts == old_texts:
            self._aitlc_steps = new_steps  # refresh objects; no cursor change
            return None
        old_current = old_texts[cursor] if 0 <= cursor < len(old_texts) else None
        if old_current is not None and old_current in new_texts:
            new_cursor = new_texts.index(old_current)
            follow = "followed"
        else:
            new_cursor = min(cursor, len(new_steps))
            follow = "clamped"
        before = len(old_steps)
        self._aitlc_steps = new_steps
        return {
            "steps_before": before,
            "steps_after": len(new_steps),
            "cursor": follow,
            "index": new_cursor,
        }

    def _step_text(self, step: Any) -> str:
        if step is None:
            return ""
        return f"{getattr(step, 'keyword', '')} {getattr(step, 'name', '')}".strip()

    def _side_run(self, step: Any) -> dict:
        """Re-run one real behave Step object against the live Context.

        This is the whole fidelity story: the Step carries its own table, text
        and (for a Scenario Outline) its example-bound name, and Step.run goes
        through behave's own registry and fires the project's before/after_step
        hooks. Nothing is reconstructed.
        """
        self._aitlc_suspended = True
        started = time.time()
        record: dict = {"step": self._step_text(step), "started_at": _stamp(started)}
        try:
            passed = step.run(self, quiet=True, capture=True)
            status = "passed" if passed else "failed"
            error = None
            if not passed:
                error = str(getattr(step, "error_message", "") or "")[:1000] or (
                    f"step did not pass (status={_status_name(step)})"
                )
        except Exception as exc:  # noqa: BLE001 - report, never crash the server
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
        finally:
            self._aitlc_suspended = False
        ended = time.time()
        record.update(
            {
                "status": status,
                "error": error,
                "duration_s": round(ended - started, 2),
                "ended_at": _stamp(ended),
            }
        )
        return record

    def _side_run_text(self, context: Any, text: str) -> dict:
        """Run ad-hoc Gherkin against the live Context via behave's execute_steps."""
        self._aitlc_suspended = True
        started = time.time()
        try:
            context.execute_steps(text)
            status, error = "passed", None
        except Exception as exc:  # noqa: BLE001
            status, error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            self._aitlc_suspended = False
        return {
            "step": text.strip().splitlines()[0] if text.strip() else "",
            "status": status,
            "error": error,
            "duration_s": round(time.time() - started, 2),
        }

    def _reload_steps(self, step_dir: str) -> list:
        """Re-import the project's step modules so an edit is picked up.

        `retry` means "run this again with my change"; that cannot mean against
        code imported minutes ago. Reloading re-populates behave's global step
        registry, which `Step.run` consults on every call.
        """
        import importlib

        reloaded: list[str] = []
        if not step_dir:
            return reloaded
        package = str(step_dir).replace("/", ".").replace("\\", ".")
        for mod_name in list(sys.modules):
            if mod_name == package or mod_name.startswith(package + "."):
                try:
                    importlib.reload(sys.modules[mod_name])
                    reloaded.append(mod_name)
                except Exception:  # noqa: BLE001 - a bad reload must not kill the session
                    pass
        return reloaded

    def _status(self, cursor: int, steps: list) -> dict:
        return {
            "event": "status",
            "index": cursor,
            "total": len(steps),
            "current_step": self._step_text(steps[cursor]) if cursor < len(steps) else None,
            "finished": cursor >= len(steps),
        }

    def _write_progress(self, *, state: str, done: int = 0, **extra: Any) -> None:
        path = os.environ.get("AITLC_GATE_PROGRESS")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {"state": state, "done": done, "updated_at": time.time(), **extra},
                    handle,
                )
        except OSError:
            pass

    def _serve(self, context: Any) -> None:
        """Own the scenario at the park point and advance it on request.

        A newline-delimited JSON protocol over a Unix socket, the same shape
        the step console used, so the client side is unchanged in spirit: one
        connection per command, the reply carries the outcome.
        """
        steps = getattr(self, "_aitlc_steps", [])
        cursor = self._gate_at()
        socket_path = os.environ.get("AITLC_GATE_SOCKET", "")

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            if socket_path and os.path.exists(socket_path):
                os.unlink(socket_path)
            srv.bind(socket_path)
            srv.listen(1)
        except OSError as exc:
            logging.error("aitlc gate could not bind %s: %s", socket_path, exc)
            os._exit(3)

        self._write_progress(
            state="parked",
            done=cursor,
            total=len(steps),
            current_step=self._step_text(steps[cursor]) if cursor < len(steps) else None,
            socket=socket_path,
        )
        # Announced on stderr too, so a launcher watching output knows the
        # session is live even before the first command arrives.
        sys.stderr.write(
            json.dumps({"event": "gate_parked", "index": cursor, "total": len(steps),
                        "socket": socket_path}) + "\n"
        )
        sys.stderr.flush()

        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            with conn:
                request = self._recv(conn)
                cmd = (request or {}).get("cmd")
                if cmd == "status":
                    self._send(conn, self._status(cursor, steps))
                elif cmd == "next":
                    if cursor < len(steps):
                        rec = self._side_run(steps[cursor])
                        cursor += 1
                        rec.update(
                            {
                                "index": cursor,
                                "finished": cursor >= len(steps),
                                "current_step": (
                                    self._step_text(steps[cursor])
                                    if cursor < len(steps)
                                    else None
                                ),
                            }
                        )
                        self._write_progress(state="parked", done=cursor, total=len(steps))
                        self._send(conn, rec)
                    else:
                        self._send(conn, {"finished": True, "index": cursor})
                elif cmd == "retry":
                    if cursor < len(steps):
                        rec = self._side_run(steps[cursor])
                        rec.update({"index": cursor, "finished": False})
                        self._send(conn, rec)
                    else:
                        self._send(conn, {"finished": True, "index": cursor})
                elif cmd == "run_text":
                    self._send(conn, self._side_run_text(context, request.get("text", "")))
                elif cmd == "reload":
                    reloaded = self._reload_steps(request.get("step_dir", ""))
                    # Pick up a Gherkin edit too: re-parse and follow the cursor,
                    # so the debugging loop's most common edit needs no restart.
                    feature_info = self._reload_feature(context, cursor)
                    if feature_info is not None:
                        steps = self._aitlc_steps
                        cursor = feature_info["index"]
                    self._send(conn, {"reloaded": reloaded, "feature": feature_info})
                elif cmd == "stop":
                    self._send(conn, {"stopped": True})
                    break
                else:
                    self._send(conn, {"error": f"unknown command {cmd!r}"})

        try:
            srv.close()
            if socket_path and os.path.exists(socket_path):
                os.unlink(socket_path)
        except OSError:
            pass
        self._write_progress(state="stopped", done=cursor, total=len(steps))
        # Hard exit so behave's teardown never runs: the browser stays open for
        # a final inspection, exactly as the pause-on-failure path intends.
        os._exit(0)

    @staticmethod
    def _recv(conn: Any) -> dict | None:
        payload = b""
        while not payload.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            payload += chunk
        if not payload.strip():
            return None
        try:
            return json.loads(payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    @staticmethod
    def _send(conn: Any, obj: dict) -> None:
        try:
            conn.sendall((json.dumps(obj) + "\n").encode())
        except OSError:
            pass
