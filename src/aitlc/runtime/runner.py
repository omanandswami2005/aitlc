"""A behave Runner subclass that adds aitlc's instrumentation.

Loaded through behave's own `--runner-class` / `--runner` option, so the
target project keeps its `environment.py` exactly as it is: this class
calls `super().run_hook(...)`, meaning every project hook still runs,
unchanged and in order.

Three modes, all opt-in via environment variables. A project that sets none
of them gets a runner behaviourally identical to behave's own.

1. Observe / halt (AITLC_PAUSE_ON_FAILURE, AITLC_EVENTS)
   Emits a JSON-lines event per step and, on a failed step, halts before
   teardown so the browser stays on the failure. A dead end by design: the
   process exits, so the browser can only be inspected, never resumed. Kept
   for callers that want a plain, foreground, uninstrumented-feeling pause
   (`aitlc behave --debug`); superseded by mode 3 for `aitlc run --debug`.

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

3. Gate-on-failure (AITLC_GATE_ON_FAILURE=1, AITLC_GATE_SOCKET,
   AITLC_GATE_PROGRESS)
   Mode 2's engine, entered reactively instead of at a chosen index: every
   step runs completely normally (nothing is auto-run or counted -- a
   passing scenario behaves exactly like an uninstrumented run) until one
   fails, and at that exact point this runner takes over the SAME socket
   loop mode 2 uses, parked on the step that just failed. `retry` re-runs
   it after a fix; `next` re-runs it and advances. This is what `aitlc run
   --debug` uses: unlike mode 1, the failure is not a dead end -- there is
   nothing to restart, because the paused process IS the fix-and-retry
   session. Real, in-scenario data found this necessary: a scenario can fail
   at any step, and mode 1 threw away exactly the state (auth, run-scoped
   data, whatever the browser was mid-way through) that made the failure
   expensive to reach in the first place.

This module is imported inside the TARGET project's interpreter, not
aitlc's, so it imports nothing from aitlc and relies only on the stdlib
plus behave itself (guaranteed present: behave is what loaded it).
"""

from __future__ import annotations

import importlib
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

# Fires the instant this module is imported -- essentially "process start",
# since the gate subprocess imports it very early via the --runner attach,
# before any project code runs. Used by `_stale_project_modules` (G54) as
# the "has this file changed since the session began" reference point.
_MODULE_LOAD_TIME = time.time()

# Set by the first run_hook() call in a gate process, read by
# _aitlc_breakpointhook (which PYTHONBREAKPOINT points at -- see
# gate_launch.launch) since breakpoint() gives its hook no reference to the
# runner instance on its own.
_ACTIVE_RUNNER: Any = None


def _aitlc_breakpointhook(*args: Any, **kwargs: Any) -> None:
    """Replaces pdb.set_trace() for code running inside a gate session.

    A plain PDB prompt cannot work here: the gate subprocess has stdin
    wired to DEVNULL and stdout redirected to a log file (gate_launch.
    launch), so nothing could read a command or show a prompt. Instead,
    this parks on a SEPARATE control socket (`AITLC_GATE_SOCKET + ".bp"`)
    -- separate because a breakpoint hit mid-step fires while the MAIN
    gate socket is already busy blocking on that step's own `next`/`retry`
    reply, so it cannot accept a second, concurrent command of its own.
    `debug status`/`debug eval` work against this while paused; `debug
    resume` is the signal that makes this call return, continuing
    execution exactly where it stopped -- no restart, nothing lost.

    Falls back to a real `pdb.set_trace()` when there is no active gate
    session (a plain uninstrumented run, `aitlc behave` without --debug),
    so breakpoint() keeps working normally everywhere else.
    """
    runner = _ACTIVE_RUNNER
    # breakpoint() is a builtin with no Python frame of its own -- it calls
    # sys.breakpointhook (this function) directly, so frame 1 up from here
    # is the caller's own frame, exactly where `breakpoint()` was written.
    frame = sys._getframe(1) if hasattr(sys, "_getframe") else None
    if runner is None or not (runner._gate_active() or runner._gate_on_failure_active()):
        import pdb

        pdb.Pdb().set_trace(frame)
        return
    runner._serve_breakpoint_pause(frame)


def _status_name(obj: Any) -> str | None:
    """Return a behave status as a plain string, whatever its type."""
    status = getattr(obj, "status", None)
    return getattr(status, "name", None) or (str(status) if status else None)


def _stamp(epoch: float) -> str:
    """UTC ISO-8601 for a wall-clock instant."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


_PAGE_STATE_TREE_CHARS = 3000


def _capture_page_state(context: Any) -> dict | None:
    """URL + a compact accessibility snapshot of the live page, best-effort.

    Attached to a failed step's result so "was there an unexpected page?"
    (an onboarding wizard, a popup, a permission prompt) answers itself in
    the JSON reply instead of requiring a separate manual `cdp inspect`/
    `debug eval` round trip. Deliberately self-contained (no aitlc import --
    see this module's own docstring: it runs inside the TARGET project's
    interpreter, which has no reason to have aitlc itself installed) and
    bounded to a few thousand chars; reach for `cdp inspect --a11y` by hand
    for the full, queryable tree. Never raises: this rides along with a
    real step result and must never cost it.
    """
    page = _find_page(context)
    if page is None:
        return None
    try:
        url = page.url
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    result: dict[str, Any] = {"url": url}
    try:
        tree = page.aria_snapshot()
        result["accessibility"] = {
            "tree": tree[:_PAGE_STATE_TREE_CHARS],
            "chars": len(tree),
            "truncated": len(tree) > _PAGE_STATE_TREE_CHARS,
        }
    except Exception as exc:  # noqa: BLE001 - page may be navigating; URL alone still helps
        result["accessibility"] = {"error": f"{type(exc).__name__}: {exc}"}
    return result


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
        global _ACTIVE_RUNNER
        if _ACTIVE_RUNNER is None:
            _ACTIVE_RUNNER = self

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

        if self._gate_on_failure_active():
            self._gate_on_failure_run_hook(name, context, *payload)
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

    # ------------------------------------------------------- gate-on-failure
    def _gate_on_failure_active(self) -> bool:
        return os.environ.get("AITLC_GATE_ON_FAILURE") == "1"

    def _gate_on_failure_run_hook(self, name: str, context: Any, *args: Any) -> None:
        """Watch every real step; on the first failure, take over via `_serve`.

        Nothing is auto-run or counted here (contrast `_gate_run_hook`'s
        before_step counter) -- behave's own loop drives every step
        completely normally until this fires, so a passing scenario is
        indistinguishable from an uninstrumented run.
        """
        if name != "after_step" or not args:
            return
        step = args[0]
        if _status_name(step) != "failed":
            return

        scenario = getattr(context, "scenario", None)
        steps = self._collect_steps(scenario)
        try:
            cursor = steps.index(step)
        except ValueError:
            # Should not happen (the step came from this same scenario's own
            # list) -- park at the end rather than crash the whole run over
            # a bookkeeping mismatch.
            cursor = len(steps)
        self._aitlc_steps = steps
        # The one thing worth parking for: without this, `status`/the initial
        # `paused_on_failure` reply and every later `debug status` say WHERE
        # (index/current_step) but never WHY -- the actual assertion/exception
        # text is sitting right here on `step` and was otherwise discarded.
        self._aitlc_park_error = str(getattr(step, "error_message", "") or "")[:500] or None
        self._serve(context, start_cursor=cursor)

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

        if getattr(context, "scenario", None) is None:
            # G72, caught live: a project's before_feature/before_scenario
            # hook can inject its own ad-hoc steps via context.execute_steps()
            # (e.g. an automatic admin login) BEFORE the target scenario's
            # own steps ever start -- context.scenario is only set once
            # behave's real Scenario.run() begins, so these hook-injected
            # steps fire before_step with context.scenario still None.
            # `--at 0` was treating the FIRST such step as "the scenario's
            # step 0" and parking on it immediately, permanently
            # interrupting the hook's own execute_steps mid-flight (the
            # admin login never finished) and skipping before_scenario --
            # and everything it sets up (e.g. this project's per-scenario
            # env vars) -- entirely, since control never returned to
            # behave's own Scenario.run() to fire it. Let these steps run
            # through completely untouched; only count/park once we're
            # genuinely inside the target scenario.
            return

        if not getattr(self, "_aitlc_steps", None):
            scenario = getattr(context, "scenario", None)
            steps = self._collect_steps(scenario)
            if not steps:
                # `scenario.all_steps` can come back empty on the very first
                # before_step of a Scenario Outline row (a real behave-timing
                # gap, not a `--at 0` bug) -- G69, caught live: `debug start
                # --at 0` reported total_steps=0/current_step=null even
                # though the real scenario had many steps, and `next` then
                # ran the real first step correctly and only found the true
                # count via a full reparse. `_reparse_steps` reads the file
                # directly and is already proven robust to a None
                # `context.scenario` (falls back to file order + example
                # index), so use it here too instead of parking on a bogus
                # empty step list.
                steps = self._reparse_steps(context) or []
            self._aitlc_steps = steps
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

    def _side_run(self, step: Any, context: Any = None) -> dict:
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
            # capture=True has no effect here: the gate process is launched
            # with --no-capture (see gate_launch.launch), so behave's own
            # stdout/stderr/log interception is switched off at the config
            # level regardless of this flag -- a step's real output goes
            # straight to the gate's own stdout (captured into gate.log by
            # the launcher), not onto `step.captured`. `debug_cmd.py` reads
            # that log's own new bytes around this call instead of relying
            # on behave's capture machinery for it.
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
                "keyword": getattr(step, "keyword", None),
            }
        )
        if status == "failed" and context is not None:
            # Answers "was there an unexpected page?" (an onboarding wizard,
            # a popup, a permission prompt) right here instead of needing a
            # separate manual `cdp inspect`/`debug eval` round trip.
            page_state = _capture_page_state(context)
            if page_state is not None:
                record["page_state"] = page_state
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

    def _side_run_line(self, context: Any, line: int) -> dict:
        """Run the step at this 1-based file line, by real bound Step object.

        The point: retyping a step's exact text (quoting, escaping, table
        rows) is real friction for a human, and error-prone -- a line
        number out of the file you're already looking at is not. Reuses
        `_reparse_steps` (the same file-reparse `next`/`retry` already rely
        on for picking up a Gherkin edit), so this gets the real, fully
        Examples-bound Step object -- table, docstring, <placeholder>
        substitution all intact -- and runs it through `_side_run`, the
        exact same path `next`/`retry` use. Higher fidelity than `run-text`
        (which re-parses a typed string via execute_steps): this is a real
        Step straight from the file, not a re-derived one.

        Does not touch the debug cursor -- like `run-text`, this is for
        trying a step without disturbing where the official sequence is
        parked.
        """
        steps = self._reparse_steps(context)
        if not steps:
            return {"error": f"could not parse the feature file for line {line}"}
        match = None
        for step in steps:
            step_line = getattr(step, "line", None)
            if step_line is None:
                continue
            if step_line == line:
                match = step
                break
            if step_line <= line and (match is None or step_line > getattr(match, "line", 0)):
                match = step
        if match is None:
            return {"error": f"no step found at or before line {line}"}
        return self._side_run(match, context)

    def _side_eval(self, context: Any, expr: str) -> dict:
        """Evaluate a JS expression against the live page (Playwright's page.evaluate()).

        The breakpoint()-equivalent for the browser side of a paused session:
        read live DOM state, count/inspect elements, pull text -- without
        advancing the cursor, touching the step registry, or leaving the
        gate. Never raises into the server loop; a bad expression or a
        page that can't be found both come back as `{"error": ...}`.
        """
        self._aitlc_suspended = True
        started = time.time()
        try:
            page = _find_page(context)
            if page is None:
                return {
                    "error": "no live page found on context",
                    "duration_s": round(time.time() - started, 2),
                }
            result = page.evaluate(expr)
            return {"result": result, "duration_s": round(time.time() - started, 2)}
        except Exception as exc:  # noqa: BLE001 - report, never crash the server
            return {
                "error": f"{type(exc).__name__}: {exc}",
                "duration_s": round(time.time() - started, 2),
            }
        finally:
            self._aitlc_suspended = False

    @staticmethod
    def _side_eval_python(frame: Any, expr: str) -> dict:
        """Evaluate a Python expression in the paused frame's own scope.

        This is what actually makes `breakpoint()` support worth having:
        `_side_eval` (JS on the page) is already reachable at any gate
        pause, breakpoint or not, so routing breakpoint's `eval` there too
        (as `debug eval` does) adds no new capability -- the one thing
        only a real `breakpoint()` can see is the paused frame's own local
        variables, which is what a developer drops one in to inspect.
        Evaluated against `frame.f_locals` and `frame.f_globals` exactly
        like `pdb`'s own `p <expr>`. Never raises into the server loop.
        """
        started = time.time()
        try:
            result = eval(expr, frame.f_globals, frame.f_locals)  # noqa: S307
        except Exception as exc:  # noqa: BLE001 - report, never crash the server
            return {
                "error": f"{type(exc).__name__}: {exc}",
                "duration_s": round(time.time() - started, 2),
            }
        try:
            json.dumps(result)
        except TypeError:
            result = repr(result)
        return {"result": result, "duration_s": round(time.time() - started, 2)}

    def _serve_breakpoint_pause(self, frame: Any) -> None:
        """Park at a code-level `breakpoint()` on a socket separate from
        the main gate one.

        Why a separate socket, not the main gate one `_serve()` already
        listens on: a `breakpoint()` hit mid-step fires while that main
        socket is already busy -- blocked inside the very `next`/`retry`
        request whose step body called it. It cannot also accept a second,
        concurrent command on the same connection-oriented socket. This
        one exists only while paused here, so its mere presence on disk
        (checked by `debug status`/`debug eval`/`debug resume`) is itself
        the "is a breakpoint active" signal -- no separate marker needed.

        Blocks until a `resume` command arrives, then cleans up and
        returns, letting the original paused call continue exactly where
        `breakpoint()` was written -- nothing restarted, nothing lost.
        """
        context = getattr(self, "context", None)
        main_socket = os.environ.get("AITLC_GATE_SOCKET", "")
        if not main_socket:
            return  # no gate session at all (e.g. a bare `aitlc behave` run)
        bp_socket_path = main_socket + ".bp"

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            if os.path.exists(bp_socket_path):
                os.unlink(bp_socket_path)
            srv.bind(bp_socket_path)
            srv.listen(1)
        except OSError as exc:
            logging.error("aitlc breakpoint could not bind %s: %s", bp_socket_path, exc)
            return

        location = {
            "file": getattr(frame.f_code, "co_filename", None) if frame else None,
            "line": getattr(frame, "f_lineno", None) if frame else None,
            "function": getattr(frame.f_code, "co_name", None) if frame else None,
        }
        sys.stderr.write(
            json.dumps({"event": "breakpoint_paused", **location, "socket": bp_socket_path})
            + "\n"
        )
        sys.stderr.flush()

        try:
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    break
                with conn:
                    request = self._recv(conn)
                    cmd = (request or {}).get("cmd")
                    if cmd == "status":
                        self._send(conn, {"paused_at": "breakpoint", **location})
                    elif cmd == "eval":
                        self._send(conn, self._side_eval(context, request.get("expr", "")))
                    elif cmd == "pyeval":
                        self._send(conn, self._side_eval_python(frame, request.get("expr", "")))
                    elif cmd == "resume":
                        self._send(conn, {"resumed": True, **location})
                        break
                    else:
                        self._send(conn, {"error": f"unknown command {cmd!r}"})
        finally:
            try:
                srv.close()
            except OSError:
                pass
            try:
                os.unlink(bp_socket_path)
            except OSError:
                pass

    def _reload_steps(self, step_dir: str) -> list:
        """Re-execute the project's step files so an edit is picked up.

        `retry` means "run this again with my change"; that cannot mean
        against code loaded minutes ago. Two distinct bugs made that true
        until now, both found live, both reproducing identically under a
        plain `debug start` session (so neither was specific to
        gate-on-failure):

        1. The first version of this used `importlib.reload()` keyed off
           `sys.modules` -- which silently did nothing for real
           step-definition files, because behave never loads them through
           the import system at all. `load_step_modules()`
           (behave/runner_util.py) `exec_file()`s each `.py` file in
           step_dir directly into a throwaway globals dict; they never get
           a `sys.modules` entry of their own.

        2. Fixed that by mirroring behave's own loading mechanism instead
           (re-`exec_file()` every `.py` under step_dir, evicting each
           file's stale registry entries first so a re-registration at the
           same (pattern, file:line) isn't silently discarded as a
           duplicate) -- and STILL got the stale function back. Root cause:
           this suite calls `behave.runner_util.reset_runtime()` somewhere
           (worker/scenario isolation), which reassigns the MODULE-LEVEL
           `behave.step_registry.registry` to a brand-new, empty
           `StepRegistry()` -- but `self.step_registry` (bound once when
           this Runner was constructed) and the `given`/`when`/`then`
           decorators (bound once at `behave/__init__.py`'s own import,
           `from .step_registry import *`) both still correctly reference
           the ORIGINAL registry. Importing `behave.step_registry.registry`
           fresh, as the eviction code did, silently operated on the wrong
           (new, unrelated, empty) instance every time -- eviction always
           "succeeded" against a registry nothing was ever looked up from.

        Fixed by evicting from `self.step_registry` directly -- the one
        instance actually consulted by `Step.run()` -- instead of ever
        re-importing the module attribute, which may have been swapped out
        from under it at any point.
        """
        if not step_dir or not os.path.isdir(step_dir):
            return []
        try:
            from behave.runner_util import exec_file, PathManager
            from behave.step_registry import setup_step_decorators
        except Exception:  # noqa: BLE001 - behave absent/renamed must not kill the session
            return []

        # Real bug found live: newer behave split `use_step_matcher`/
        # `step_matcher` out of `behave.matchers` into `behave.api.step_matchers`,
        # and dropped the module-level `matchers.current_matcher` getter/setter
        # entirely in favour of a factory object (`get_step_matcher_factory()`)
        # with `use_current_step_matcher_as_default()`/`use_default_step_matcher()`
        # helpers. The old code always imported the pre-split names, so every
        # `next`/`retry` call crashed the whole gated behave process outright
        # (AttributeError, uncaught, killing the one thing worth debugging) on
        # any behave version past that split -- probe for the new module first,
        # matching `load_step_modules`'s own current implementation exactly,
        # and only fall back to the old module-level attributes for a behave
        # old enough to still have them (same "ask the tool" principle as
        # attach.py's own behave-version probing elsewhere in this project).
        try:
            from behave.api.step_matchers import step_matcher, use_step_matcher
            from behave.matchers import (
                use_current_step_matcher_as_default,
                use_default_step_matcher,
            )
            modern_matcher_api = True
        except ImportError:
            from behave import matchers

            use_step_matcher = matchers.use_step_matcher
            step_matcher = matchers.step_matcher
            modern_matcher_api = False

        step_globals_base = {
            "use_step_matcher": use_step_matcher,
            "step_matcher": step_matcher,  # -- deprecating, same as load_step_modules
        }
        setup_step_decorators(step_globals_base, registry=self.step_registry)
        if modern_matcher_api:
            use_current_step_matcher_as_default()
        else:
            default_matcher = matchers.current_matcher

        reloaded: list[str] = []
        mtimes = getattr(self, "_aitlc_step_mtimes", None)
        if mtimes is None:
            mtimes = {}
            self._aitlc_step_mtimes = mtimes
        with PathManager([step_dir]):
            all_file_paths = [
                os.path.join(step_dir, name)
                for name in sorted(os.listdir(step_dir))
                if name.endswith(".py")
            ]
            # Skip any file whose mtime hasn't moved since it was last
            # reloaded -- re-exec'ing every one of a suite's step files on
            # EVERY `next`/`retry` (this project alone has 82) was pure,
            # avoidable cost when the common case during iteration is "one
            # file changed, the rest didn't". A file new to `mtimes` (first
            # call, or a file added since) always counts as changed so it
            # gets its one real load. Safe to skip untouched files entirely
            # (no evict, no re-exec): the transient-ambiguity risk the
            # evict-all-then-exec-all ordering guards against (see the
            # docstring above) only exists between files that are BOTH
            # mid-reload at the same time -- a file that is never evicted
            # never opens that window.
            file_paths = []
            for file_path in all_file_paths:
                try:
                    current_mtime = os.stat(file_path).st_mtime
                except OSError:
                    continue
                if mtimes.get(file_path) == current_mtime:
                    continue
                file_paths.append((file_path, current_mtime))

            # Evict ALL changed files first, THEN re-exec all of them --
            # doing both per file, one at a time, leaves a transient window
            # (on the 2nd+ reload) where file B still holds its stale entry
            # from the LAST cycle while file A's is already being re-added
            # this cycle. If A and B have a genuinely ambiguous pattern
            # overlap (a specific placeholder pattern vs. a more general one
            # that can also match it -- e.g. `click on "{option}" for
            # contact name...` vs. an already-registered `click on
            # "{text}"`), that transient coexistence raises AmbiguousStep --
            # something the real one-time initial load never hits, since it
            # adds every file exactly once, in order, with nothing to evict.
            # The exception aborts the failing file's exec_file() partway
            # through, silently dropping every step defined after that
            # point in the SAME file. Evicting everything up front restores
            # the same "clean slate, add in order" shape the initial load
            # has, so this transient ambiguity can't arise at all.
            for file_path, _mtime in file_paths:
                self._evict_step_registrations_for_file(file_path)
            for file_path, mtime in file_paths:
                try:
                    exec_file(file_path, step_globals_base.copy())
                    reloaded.append(file_path)
                    mtimes[file_path] = mtime
                except Exception:  # noqa: BLE001 - a bad edit must not kill the session
                    pass
                if modern_matcher_api:
                    use_default_step_matcher()
                else:
                    matchers.current_matcher = default_matcher
        return reloaded

    def _evict_step_registrations_for_file(self, file_path: str) -> None:
        """Remove one file's step definitions from the live step registry.

        Must run BEFORE re-`exec_file()`-ing that file: behave's own
        `StepRegistry.add_step_definition` (behave/step_registry.py) treats
        a re-registration at the same (pattern, file:line) as "already
        registered" and silently discards it -- so re-executing a step file
        whose edit only changed a function BODY (not its line position)
        would run the decorators for nothing, leaving the stale, pre-edit
        function bound in the registry forever, with no error raised
        anywhere to say so. Evicting first means the re-exec's
        re-registration is never seen as a duplicate.

        Operates on `self.step_registry` -- the instance `Step.run()`
        actually consults -- never on `behave.step_registry.registry`
        freshly imported, which a project's own `reset_runtime()` call can
        silently replace with an unrelated new instance (see
        `_reload_steps`'s docstring for how this was found).
        """
        step_registry = getattr(self, "step_registry", None)
        if step_registry is None:
            return
        try:
            target = os.path.abspath(file_path)
        except (OSError, ValueError):
            return
        for step_type, definitions in step_registry.steps.items():
            step_registry.steps[step_type] = [
                defn
                for defn in definitions
                if os.path.abspath(getattr(defn.location, "filename", "") or "")
                != target
            ]

    def _reload_stale_project_modules(self, step_dir: str) -> dict:
        """Auto-reload project modules edited since the session started.

        `_reload_steps` only re-execs files directly under `step_dir`,
        mirroring behave's own step loading. Anything a step imports
        NORMALLY (a page object, a helper, `common_page_function.py`, ...)
        is a real Python `import`, cached in `sys.modules` by the
        interpreter itself; nothing about that reload touches it. Found
        live: an edit to a page-object function kept silently running the
        OLD code through any number of `retry` calls, with no error or
        warning that the fix was never actually exercised.

        `importlib.reload()` handles this correctly for the common shape in
        this kind of codebase -- a page object as a class of `@staticmethod`s
        with no persistent module-level state -- because it mutates the
        SAME module object/namespace `sys.modules` already holds in place,
        rather than creating a new one. That matters for ordering: this
        must run BEFORE `_reload_steps` re-execs the step files below, so a
        step file's own `from pages.search.search_page import SearchPage`
        (re-run fresh by that exec) resolves against the UPDATED attribute,
        not a stale one captured before this ran.

        A module that genuinely can't reload cleanly (a real error the
        module's own top-level code raises -- an open resource, a bound
        singleton, whatever) reports its own error rather than either
        silently running stale code or crashing the whole gate; the caller
        (`_serve`'s "reload" branch) surfaces that as a warning naming a
        fresh session as the fallback for exactly those files, not for
        every file that changed.

        Returns `{"reloaded": [...], "failed": {path: error}}`, both keyed
        by project-relative path, empty when nothing changed. Best-effort
        throughout: an unreadable path or a module with no real file on
        disk is skipped rather than raising into the gate's request/reply
        loop.
        """
        root = os.path.abspath(os.getcwd())
        step_dir_abs = os.path.abspath(step_dir) if step_dir else None
        reloaded: list[str] = []
        failed: dict[str, str] = {}
        for name, module in list(sys.modules.items()):
            file_path = getattr(module, "__file__", None)
            if not file_path:
                continue
            try:
                file_abs = os.path.abspath(file_path)
            except (OSError, ValueError, TypeError):
                continue
            if not file_abs.startswith(root + os.sep):
                continue  # stdlib / site-packages / aitlc itself -- not this project
            if step_dir_abs and file_abs.startswith(step_dir_abs + os.sep):
                continue  # already correctly reloaded by _reload_steps
            try:
                mtime = os.path.getmtime(file_abs)
            except OSError:
                continue
            if mtime <= _MODULE_LOAD_TIME:
                continue
            rel = os.path.relpath(file_abs, root)
            try:
                importlib.reload(sys.modules[name])
                reloaded.append(rel)
            except Exception as exc:  # noqa: BLE001 - report, never crash the session
                failed[rel] = f"{type(exc).__name__}: {exc}"
        return {"reloaded": sorted(reloaded), "failed": failed}

    def _status(self, cursor: int, steps: list) -> dict:
        return {
            "event": "status",
            "index": cursor,
            "total": len(steps),
            "current_step": self._step_text(steps[cursor]) if cursor < len(steps) else None,
            "finished": cursor >= len(steps),
            # None for a plain `debug start --at N` park (nothing failed --
            # there is nothing to explain); the captured assertion/exception
            # text for a `run --debug` gate-on-failure park.
            "error": getattr(self, "_aitlc_park_error", None),
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

    def _run_cleanup_hooks(self, context: Any) -> dict:
        """Fire the project's real after_scenario/after_feature hooks before exit.

        Opt-in ONLY, via `stop`'s `cleanup` flag -- never automatic. These
        hooks do real, sometimes slow work (this suite's own
        `cleanup_registration_func` logs out), and a debug session's whole
        value is a persistent, already-authenticated browser; running this
        unconditionally on every `stop` would silently reintroduce the exact
        repeated-login cost the gate exists to avoid. Call it only when you
        deliberately want a clean handoff.

        Calls the two hooks directly with the real live context/scenario/
        feature, the same (context, entity) shape `Scenario.run()`/
        `Feature.run()` themselves use -- not through those methods, since
        their tag dispatch, formatter callbacks and context push/pop
        bookkeeping serve a full run this doesn't need (the process exits
        right after). Never silently claims success: each hook's outcome is
        reported, including a failure, rather than assumed.
        """
        self._aitlc_suspended = True
        result: dict = {"after_scenario": None, "after_feature": None}
        try:
            scenario = getattr(context, "scenario", None)
            if scenario is not None:
                try:
                    self.run_hook("after_scenario", context, scenario)
                    result["after_scenario"] = "ok"
                except Exception as exc:  # noqa: BLE001 - report, don't crash the teardown
                    result["after_scenario"] = f"{type(exc).__name__}: {exc}"
            else:
                result["after_scenario"] = "skipped: no context.scenario"

            feature = getattr(context, "feature", None)
            if feature is not None:
                try:
                    self.run_hook("after_feature", context, feature)
                    result["after_feature"] = "ok"
                except Exception as exc:  # noqa: BLE001
                    result["after_feature"] = f"{type(exc).__name__}: {exc}"
            else:
                result["after_feature"] = "skipped: no context.feature"
        finally:
            self._aitlc_suspended = False
        return result

    def _serve(self, context: Any, *, start_cursor: int | None = None) -> None:
        """Own the scenario at the park point and advance it on request.

        A newline-delimited JSON protocol over a Unix socket, the same shape
        the step console used, so the client side is unchanged in spirit: one
        connection per command, the reply carries the outcome.

        `start_cursor` lets a reactive caller (gate-on-failure, parked on
        whichever step just failed) pick the park point at call time;
        omitted, this falls back to `_gate_at()` -- mode 2's pre-chosen
        index -- so `debug start`'s behaviour is unchanged.
        """
        steps = getattr(self, "_aitlc_steps", [])
        cursor = start_cursor if start_cursor is not None else self._gate_at()
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
                        ran_at = cursor
                        rec = self._side_run(steps[cursor], context)
                        cursor += 1
                        rec.update(
                            {
                                "index": cursor,
                                # The step actually just executed, 0-based --
                                # distinct from "index" above, which is the
                                # POST-increment cursor (the next step to
                                # run). Kept separate rather than repurposing
                                # "index" so existing consumers of that
                                # field's exact meaning aren't disturbed.
                                "step_index": ran_at,
                                "total": len(steps),
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
                        rec = self._side_run(steps[cursor], context)
                        rec.update(
                            {
                                "index": cursor,
                                "step_index": cursor,
                                "total": len(steps),
                                "finished": False,
                            }
                        )
                        self._send(conn, rec)
                    else:
                        self._send(conn, {"finished": True, "index": cursor})
                elif cmd == "run_text":
                    self._send(conn, self._side_run_text(context, request.get("text", "")))
                elif cmd == "run_line":
                    self._send(conn, self._side_run_line(context, request.get("line", 0)))
                elif cmd == "eval":
                    self._send(conn, self._side_eval(context, request.get("expr", "")))
                elif cmd == "reload":
                    # G54: reload a page-object/helper edit BEFORE the step
                    # files below, so a step's own `from pages.search... import
                    # SearchPage` (re-run fresh by _reload_steps) resolves
                    # against the just-updated attribute, not a stale one.
                    module_reload = self._reload_stale_project_modules(
                        request.get("step_dir", "")
                    )
                    reloaded = self._reload_steps(request.get("step_dir", ""))
                    # Pick up a Gherkin edit too: re-parse and follow the cursor,
                    # so the debugging loop's most common edit needs no restart.
                    feature_info = self._reload_feature(context, cursor)
                    if feature_info is not None:
                        steps = self._aitlc_steps
                        cursor = feature_info["index"]
                    reply = {"reloaded": reloaded, "feature": feature_info}
                    if module_reload["reloaded"]:
                        reply["reloaded_modules"] = module_reload["reloaded"]
                    if module_reload["failed"]:
                        reply["stale_modules"] = sorted(module_reload["failed"])
                        reply["warning"] = (
                            f"{len(module_reload['failed'])} file(s) changed since this "
                            "session started could not be reloaded automatically -- "
                            "start a fresh session to exercise these edits: "
                            + "; ".join(
                                f"{path} ({err})"
                                for path, err in sorted(module_reload["failed"].items())
                            )
                        )
                    self._send(conn, reply)
                elif cmd == "stop":
                    reply = {"stopped": True}
                    if request.get("cleanup"):
                        reply["cleanup"] = self._run_cleanup_hooks(context)
                    self._send(conn, reply)
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
