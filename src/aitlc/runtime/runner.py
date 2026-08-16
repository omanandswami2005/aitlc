"""A behave Runner subclass that adds aitlc's instrumentation.

Loaded through behave's own `--runner-class` / `--runner` option, so the
target project keeps its `environment.py` exactly as it is: this class
calls `super().run_hook(...)`, meaning every project hook still runs,
unchanged and in order. aitlc only observes, and — when explicitly asked —
halts.

This module is imported inside the TARGET project's interpreter, not
aitlc's, so it must import nothing from aitlc and rely only on the stdlib
plus behave itself (which is guaranteed present: behave is what loaded it).

Behaviour is opt-in via environment variables, so a project that sets none
of them gets a runner that is behaviourally identical to behave's own:

  AITLC_PAUSE_ON_FAILURE=1  halt at the first failed step, before any
                            teardown, leaving the browser open
  AITLC_EVENTS=<path>       append a JSON-lines event per step
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

try:
    from behave.runner import Runner as _BaseRunner
except ImportError:  # pragma: no cover - only when behave is absent
    _BaseRunner = object


def _status_name(obj: Any) -> str | None:
    """Return a behave status as a plain string, whatever its type."""
    status = getattr(obj, "status", None)
    return getattr(status, "name", None) or (str(status) if status else None)


def _find_page(context: Any) -> Any:
    """Best-effort lookup of a Playwright Page on the behave context.

    Projects name this differently, and a generic tool cannot assume one
    shape. These are the conventional names, tried in order of how
    directly they identify a real page object; anything not found simply
    yields None, and the caller degrades rather than failing.
    """
    candidates = ("page", "primary_page", "current_page")
    for name in candidates:
        page = getattr(context, name, None)
        if page is not None and hasattr(page, "url"):
            return page

    # Some suites keep the page behind a driver/wrapper object.
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
    """behave Runner that observes hooks and can halt on failure."""

    def run_hook(self, name: str, context: Any, *args: Any) -> Any:
        """Run the project's hook, then apply aitlc's own instrumentation.

        The project's hook is always invoked first and unmodified, so
        anything it does (screenshots, tracing, cleanup registration) has
        already happened before aitlc looks at the result.
        """
        result = super().run_hook(name, context, *args)

        if name != "after_step" or not args:
            return result

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

        return result

    def _halt_for_inspection(self, context: Any, step: Any) -> None:
        """Stop the process immediately, leaving the browser usable.

        `os._exit` is deliberate and is the whole point: a normal exception
        or `sys.exit` would unwind into behave's `after_scenario`/
        `after_all` hooks, which is where a suite closes its browser. That
        teardown destroys the one thing worth inspecting. Exiting hard is
        the only way to keep the failed page alive for a CDP attach.
        """
        page = _find_page(context)
        url = None
        if page is not None:
            try:
                url = page.url
            except Exception:  # noqa: BLE001 - a dead page must not mask the failure
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

        # behave captures stdout, and os._exit skips the flush that would
        # normally release that buffer — a plain print() here is written and
        # then thrown away, so the one message explaining why the run stopped
        # never reaches the terminal. Found live: the pause fired, the browser
        # stayed open, and nothing said so. logging and stderr both survive,
        # so use them rather than relying on either alone.
        logging.warning("aitlc paused on failure: %s", json.dumps(payload))
        try:
            sys.stderr.write(json.dumps(payload) + "\n")
            sys.stderr.flush()
        except (OSError, ValueError):
            pass

        os._exit(1)
