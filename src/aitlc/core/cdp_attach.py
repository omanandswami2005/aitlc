"""CDP-attach inspection utility (FR-4).

Generalizes a proven CDP-attach technique: attach to an
already-running Chromium instance over the DevTools Protocol and inspect it
— screenshot, locator presence/visibility checks — without launching a new
browser. This is the primitive `aitlc cdp inspect` is built on, and the same
one an evidence-bundle auto-capture (FR-4.3) would use on any step failure.

Generic on purpose: no project-specific locator or config knowledge here —
callers pass in whatever selectors they care about.
"""

from __future__ import annotations

from datetime import datetime

import time

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright


def _iso(epoch: float) -> str:
    """Local-time ISO stamp, so a measurement lines up with a human-read log."""
    return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")


@dataclass
class LocatorCheck:
    """Whether one selector exists and is visible on the page."""

    selector: str
    exists: bool
    visible: bool
    count: int
    bounding_box: dict[str, float] | None = None
    error: str | None = None


def _fingerprint(value: str) -> str:
    """A value's shape, without the value.

    "which account is this browser signed in as" is answered by a token's
    *identity*, not its contents, and printing a live session token into a
    terminal, a log, or an agent transcript is how it ends up somewhere it
    should not be. Length plus a short digest is enough to tell two sessions
    apart and to spot the all-zeros identity a rejected token produces.
    """
    import hashlib

    if value is None:
        return "<none>"
    text = str(value)
    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
    return f"<{len(text)} chars, sha256:{digest}>"


def collect_storage(context, page, *, reveal: bool = False) -> dict:
    """Cookies and localStorage for the live page.

    The question a wrong-account failure turns on is *which* account the
    browser is signed in as, and it lives in a cookie or a localStorage key --
    neither of which the tool could read, so it was done with hand-written
    scripts against the same browser.

    Values are fingerprinted rather than printed unless `reveal` is set: a
    session cookie is a working credential.
    """
    def present(value):
        return value if reveal else _fingerprint(value)

    cookies = []
    try:
        for cookie in context.cookies():
            cookies.append(
                {
                    "name": cookie.get("name", ""),
                    "domain": cookie.get("domain", ""),
                    "path": cookie.get("path", ""),
                    "expires": cookie.get("expires"),
                    "http_only": cookie.get("httpOnly"),
                    "secure": cookie.get("secure"),
                    "value": present(cookie.get("value", "")),
                }
            )
    except Exception as exc:  # noqa: BLE001 - a snapshot must not fail the run
        cookies = [{"error": f"{type(exc).__name__}: {exc}"}]

    local_storage: dict = {}
    session_storage: dict = {}
    try:
        raw = page.evaluate(
            "() => ({local: Object.assign({}, window.localStorage),"
            " session: Object.assign({}, window.sessionStorage)})"
        )
        local_storage = {k: present(v) for k, v in (raw.get("local") or {}).items()}
        session_storage = {k: present(v) for k, v in (raw.get("session") or {}).items()}
    except Exception as exc:  # noqa: BLE001
        local_storage = {"error": f"{type(exc).__name__}: {exc}"}

    return {
        "revealed": reveal,
        "cookies": cookies,
        "local_storage": local_storage,
        "session_storage": session_storage,
    }


@dataclass
class InspectionResult:
    """A snapshot of a live page: URL, viewport and checks."""

    url: str
    viewport: dict[str, int] | None
    screenshot_path: Path | None
    checks: list[LocatorCheck]
    # The page's accessibility tree — what a screen reader perceives.
    # Present only when explicitly requested, since it is much larger than
    # the rest of the payload.
    accessibility: dict[str, Any] | None = None
    storage: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable form of this inspection."""
        return {
            "url": self.url,
            "viewport": self.viewport,
            **({"storage": self.storage} if self.storage is not None else {}),
            "screenshot_path": (
                str(self.screenshot_path) if self.screenshot_path else None
            ),
            "checks": [
                {
                    "selector": c.selector,
                    "exists": c.exists,
                    "visible": c.visible,
                    "count": c.count,
                    "bounding_box": c.bounding_box,
                    "error": c.error,
                }
                for c in self.checks
            ],
            "accessibility": self.accessibility,
        }


def _shape_yaml(tree: str, *, query: str | None, selector: str | None) -> dict:
    """Trim an aria YAML tree to what the caller actually asked about.

    A whole-page tree is far cheaper than a screenshot but is still mostly
    irrelevant to a specific question. Answering "is the upgrade button
    on screen" needs one line, not the page. `chars` is reported so a
    caller can see what a query saved and tighten it if it did not.

    Matching is a case-insensitive substring over rendered lines, which is
    what a role/name query realistically is; matched lines keep their
    original indentation so nesting stays readable.
    """
    result: dict[str, Any] = {"format": "aria-yaml"}
    if selector:
        result["scoped_to"] = selector

    if not query:
        result["tree"] = tree
        result["chars"] = len(tree)
        return result

    needle = query.lower()
    matches = [line for line in tree.splitlines() if needle in line.lower()]
    joined = "\n".join(matches)
    result.update(
        {
            "query": query,
            "matches": len(matches),
            "tree": joined,
            "chars": len(joined),
            "full_chars": len(tree),
        }
    )
    return result


def _accessibility_tree(
    context: Any,
    page: Any,
    *,
    interesting_only: bool = True,
    selector: str | None = None,
    query: str | None = None,
) -> dict:
    """Return what a screen reader would perceive on this page.

    Prefers Playwright's `aria_snapshot()`, which yields a hierarchical
    YAML tree — it keeps nesting, control state (`[expanded]`) and field
    values, all of which a flat node list loses. `page.accessibility` was
    deprecated for three years and then removed in favour of it, so
    reaching for the old API would break on current Playwright.

    Falls back to the CDP `Accessibility` domain on Playwright versions
    predating `aria_snapshot`, so the command still works there rather
    than reporting nothing.
    """
    target = page.locator(selector) if selector else page
    snapshot = getattr(target, "aria_snapshot", None)
    if callable(snapshot):
        try:
            tree = snapshot()
        except Exception as exc:  # noqa: BLE001 - page may be navigating
            return {"error": f"{type(exc).__name__}: {exc}"}
        return _shape_yaml(tree, query=query, selector=selector)

    try:
        session = context.new_cdp_session(page)
        session.send("Accessibility.enable")
        payload = session.send("Accessibility.getFullAXTree")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}

    nodes = []
    for node in payload.get("nodes", []):
        role = (node.get("role") or {}).get("value")
        name = (node.get("name") or {}).get("value") or ""
        if interesting_only and (role in (None, "none", "generic") or not name.strip()):
            continue
        entry = {"role": role, "name": name.strip()}
        value = (node.get("value") or {}).get("value")
        if value:
            entry["value"] = value
        nodes.append(entry)

    return {
        "format": "cdp-nodes",
        "node_count": len(nodes),
        "raw_node_count": len(payload.get("nodes", [])),
        "nodes": nodes,
    }


def inspect(
    cdp_url: str,
    *,
    screenshot_path: Path | None = None,
    check_selectors: list[str] | None = None,
    full_page: bool = False,
    accessibility: bool = False,
    interesting_only: bool = True,
    a11y_selector: str | None = None,
    a11y_query: str | None = None,
    storage: bool = False,
    reveal_values: bool = False,
) -> InspectionResult:
    """Attach to a live browser over CDP and report page state + locator checks.

    check_selectors accepts plain CSS/text selectors or xpath (prefix with
    `//` or `(` per this project's existing convention for xpath detection).

    `accessibility` adds the page's accessibility tree: the roles and names
    a screen reader would announce. For answering "is the upgrade button
    on screen", that tree is both far smaller than a screenshot and
    directly assertable, where a PNG has to be looked at by a human or a
    vision model to yield the same answer. `interesting_only` keeps
    Playwright's own filtering, which drops nodes that carry no semantic
    information.
    """
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()

        tree: dict | None = None
        if accessibility:
            tree = _accessibility_tree(
                context,
                page,
                interesting_only=interesting_only,
                selector=a11y_selector,
                query=a11y_query,
            )

        checks: list[LocatorCheck] = []
        for selector in check_selectors or []:
            try:
                locator = page.locator(selector).first
                count = page.locator(selector).count()
                exists = count > 0
                visible = locator.is_visible() if exists else False
                box = locator.bounding_box() if exists else None
                checks.append(
                    LocatorCheck(
                        selector=selector,
                        exists=exists,
                        visible=visible,
                        count=count,
                        bounding_box=box,
                    )
                )
            except Exception as exc:
                checks.append(
                    LocatorCheck(
                        selector=selector,
                        exists=False,
                        visible=False,
                        count=0,
                        error=str(exc),
                    )
                )

        saved_path = None
        if screenshot_path:
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_path), full_page=full_page)
            saved_path = screenshot_path

        viewport = page.viewport_size

        result = InspectionResult(
            url=page.url,
            viewport=viewport,
            screenshot_path=saved_path,
            checks=checks,
            accessibility=tree,
            storage=(
                collect_storage(context, page, reveal=reveal_values)
                if storage
                else None
            ),
        )
        # Deliberately do NOT close the browser — this attaches to a live
        # session (e.g. one frozen by DEBUG_PAUSE_ON_FAILURE) that the
        # caller/test process still owns.
        return result


@dataclass
class ConditionTiming:
    """How long a page condition actually took to come true."""

    selector: str
    condition: str
    met: bool
    waited_s: float
    started_at: str
    ended_at: str
    confirmed_start_state: bool
    polls: int
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "selector": self.selector,
            "condition": self.condition,
            "met": self.met,
            "waited_s": round(self.waited_s, 1),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "confirmed_start_state": self.confirmed_start_state,
            "polls": self.polls,
            "note": self.note,
        }


def time_condition(
    cdp_url: str,
    selector: str,
    *,
    condition: str = "hidden",
    timeout_s: float = 900.0,
    poll_s: float = 2.0,
    require_start_state: bool = True,
) -> ConditionTiming:
    """Measure how long a page takes to satisfy a condition, in wall-clock time.

    Written after two failed attempts to measure the same thing by hand. The
    trap both times was the same: if the element is *already* in the target
    state when measuring begins -- because the page never loaded, or the app
    logged itself out -- the condition is instantly true and the run reports
    a triumphant "cleared in 0.4s" for something that never happened.

    So `require_start_state` insists the element is first observed in the
    opposite state, and says plainly when it was not. A measurement that
    cannot be trusted is reported as untrusted rather than as a number,
    because a wrong number here gets written into a timeout and quietly
    breaks a suite.
    """
    if condition not in ("hidden", "visible"):
        raise ValueError("condition must be 'hidden' or 'visible'")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        locator = page.locator(selector)

        def satisfied() -> bool:
            try:
                visible = locator.first.is_visible()
            except Exception:
                visible = False
            return (not visible) if condition == "hidden" else visible

        started = time.time()
        confirmed_start = not satisfied()
        if require_start_state and not confirmed_start:
            return ConditionTiming(
                selector=selector,
                condition=condition,
                met=False,
                waited_s=0.0,
                started_at=_iso(started),
                ended_at=_iso(started),
                confirmed_start_state=False,
                polls=0,
                note=(
                    f"the element was already {condition} before waiting began, so "
                    "nothing was measured. Re-run once the opposite state is on "
                    "screen, or pass require_start_state=False if that is expected."
                ),
            )

        polls = 0
        deadline = started + timeout_s
        while time.time() < deadline:
            polls += 1
            if satisfied():
                ended = time.time()
                return ConditionTiming(
                    selector=selector,
                    condition=condition,
                    met=True,
                    waited_s=ended - started,
                    started_at=_iso(started),
                    ended_at=_iso(ended),
                    confirmed_start_state=confirmed_start,
                    polls=polls,
                )
            time.sleep(poll_s)

        ended = time.time()
        return ConditionTiming(
            selector=selector,
            condition=condition,
            met=False,
            waited_s=ended - started,
            started_at=_iso(started),
            ended_at=_iso(ended),
            confirmed_start_state=confirmed_start,
            polls=polls,
            note=f"still not {condition} after {timeout_s:.0f}s",
        )
