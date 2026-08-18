"""Timing an app condition, and refusing to report a measurement that is a lie.

Both hand-run attempts at this measurement produced numbers that had to be
withdrawn, for the same reason: the element was already in the target state
when the clock started, so the wait returned instantly and the number
described nothing. The guard against that is the point of the feature.
"""

from __future__ import annotations

from aitlc.core import cdp_attach


class _Locator:
    def __init__(self, visibility):
        self._visibility = list(visibility)
        self.first = self

    def is_visible(self):
        # Repeat the final state once exhausted, like a real page would.
        return self._visibility.pop(0) if len(self._visibility) > 1 else self._visibility[0]


class _Page:
    def __init__(self, visibility):
        self._locator = _Locator(visibility)

    def locator(self, _selector):
        return self._locator


def _wire(monkeypatch, visibility):
    page = _Page(visibility)
    context = type("C", (), {"pages": [page]})()
    browser = type("B", (), {"contexts": [context]})()
    entered = type("P", (), {"chromium": type("Ch", (), {"connect_over_cdp": staticmethod(lambda _u: browser)})()})()
    monkeypatch.setattr(
        cdp_attach,
        "sync_playwright",
        lambda: type("CM", (), {
            "__enter__": staticmethod(lambda *_a: entered),
            "__exit__": staticmethod(lambda *_a: False),
        })(),
    )
    monkeypatch.setattr(cdp_attach.time, "sleep", lambda _s: None)


class TestTheGuard:
    def test_an_element_already_hidden_is_refused_not_timed(self, monkeypatch):
        """The exact false pass that had to be withdrawn twice."""
        _wire(monkeypatch, [False])

        timing = cdp_attach.time_condition("http://x", "#banner", condition="hidden")

        assert timing.met is False
        assert timing.confirmed_start_state is False
        assert timing.waited_s == 0.0
        assert "already hidden" in timing.note

    def test_the_guard_can_be_waived_explicitly(self, monkeypatch):
        _wire(monkeypatch, [False])
        timing = cdp_attach.time_condition(
            "http://x", "#banner", condition="hidden", require_start_state=False
        )
        assert timing.met is True


class TestRealMeasurement:
    def test_a_banner_that_clears_is_timed_and_confirmed(self, monkeypatch):
        # visible, visible, then gone
        _wire(monkeypatch, [True, True, True, False])

        timing = cdp_attach.time_condition(
            "http://x", "#banner", condition="hidden", poll_s=0
        )

        assert timing.met is True
        assert timing.confirmed_start_state is True, "must prove it started visible"
        assert timing.polls >= 1
        assert timing.started_at and timing.ended_at

    def test_waiting_for_something_to_appear_works_the_same_way(self, monkeypatch):
        _wire(monkeypatch, [False, False, True])
        timing = cdp_attach.time_condition(
            "http://x", "#toast", condition="visible", poll_s=0
        )
        assert timing.met is True and timing.confirmed_start_state is True

    def test_giving_up_reports_not_met_rather_than_a_number_to_trust(self, monkeypatch):
        _wire(monkeypatch, [True])
        timing = cdp_attach.time_condition(
            "http://x", "#banner", condition="hidden", timeout_s=0, poll_s=0
        )
        assert timing.met is False
        assert "still not hidden" in timing.note

    def test_an_unknown_condition_is_rejected_early(self, monkeypatch):
        _wire(monkeypatch, [True])
        try:
            cdp_attach.time_condition("http://x", "#b", condition="sideways")
        except ValueError as exc:
            assert "hidden" in str(exc)
        else:
            raise AssertionError("an unknown condition must not be silently accepted")
