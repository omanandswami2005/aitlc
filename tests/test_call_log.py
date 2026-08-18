"""The call-log lines that explain a failure, not just the ones that name it.

The sample below is the shape of a real Playwright timeout: the exception
line says a click timed out, and every line that says *why* sits underneath
it. Triage used to print the exception and the `waiting for` line, which are
the two least informative lines in the block.
"""

from __future__ import annotations

from aitlc.core import triage

BLOCKED_BY_OVERLAY = """TimeoutError: Locator.click: Timeout 30000ms exceeded.
Call log:
  - waiting for locator("[id=\\"saveItem\\"]").first
    - locator resolved to <button id="saveItem" class="btn primary">Save</button>
  - attempting click action
    2 × waiting for element to be visible, enabled and stable
      - element is visible, enabled and stable
      - scrolling into view if needed
      - <div role="presentation" class="Dialog-container">…</div> from <div id="saveDialog">…</div> subtree intercepts pointer events
    - retrying click action
    58 × waiting for element to be visible, enabled and stable

Captured logging:
noise that must never be mistaken for the failure
"""

DISABLED_BUTTON = """TimeoutError: Locator.click: Timeout 30000ms exceeded.
Call log:
  - waiting for locator("#saveItem")
    - locator resolved to <button id="saveItem" disabled>Save</button>
"""

NEVER_APPEARED = """TimeoutError: Locator.click: Timeout 30000ms exceeded.
Call log:
  - waiting for locator("#missing")
"""


class TestHighlights:
    def test_the_overlay_that_ate_the_click_is_named(self):
        """The culprit element is the fix; it used to be dropped."""
        found = triage.call_log_highlights(BLOCKED_BY_OVERLAY)
        assert "intercepts pointer events" in found["intercepted_by"]
        assert "saveDialog" in found["intercepted_by"]

    def test_the_resolved_element_is_kept(self):
        found = triage.call_log_highlights(DISABLED_BUTTON)
        assert found["resolved"].startswith("locator resolved to")
        # A disabled button is not a waiting problem. Reading this is the
        # difference between fixing a timeout and fixing what disabled it.
        assert "disabled" in found["resolved"]

    def test_retry_count_separates_blocked_from_absent(self):
        assert triage.call_log_highlights(BLOCKED_BY_OVERLAY)["retries"] == 58
        assert triage.call_log_highlights(NEVER_APPEARED)["retries"] == 0

    def test_an_element_that_never_resolved_reports_empty_not_invented(self):
        found = triage.call_log_highlights(NEVER_APPEARED)
        assert found["resolved"] == ""
        assert found["intercepted_by"] == ""
        assert found["waiting_for"].startswith("waiting for")

    def test_captured_streams_are_cut_before_anything_is_read(self):
        for value in triage.call_log_highlights(BLOCKED_BY_OVERLAY).values():
            assert "noise that must never" not in str(value)

    def test_nothing_usable_is_empty_not_an_error(self):
        assert triage.call_log_highlights(None)["resolved"] == ""
        assert triage.call_log_highlights([])["retries"] == 0

    def test_a_list_error_message_is_joined_like_a_string(self):
        found = triage.call_log_highlights(DISABLED_BUTTON.splitlines())
        assert "disabled" in found["resolved"]


def test_triage_rows_carry_the_highlights():
    """The point of the change: they reach the table, not just the parser."""
    document = [
        {
            "name": "f",
            "tags": [{"name": "TEST_PROJ-1"}],
            "elements": [
                {
                    "type": "scenario",
                    "name": "s",
                    "tags": [{"name": "TEST_PROJ-1"}],
                    "steps": [
                        {
                            "keyword": "Then ",
                            "name": "click save",
                            "result": {
                                "status": "failed",
                                "error_message": BLOCKED_BY_OVERLAY,
                            },
                        }
                    ],
                }
            ],
        }
    ]
    result = triage.triage_documents([("behave_PROJ-1_2026-01-01T00-00-00.json", document)])
    failure = result.failures[0]
    assert "intercepts pointer events" in failure.intercepted_by
    assert failure.retries == 58
    assert "saveItem" in failure.resolved


COUNTER_PREFIXED = """AssertionError: Locator expected to be hidden
Call log:
  - Expect "to_be_hidden" with timeout 120000ms
  - waiting for locator("//span[text()='under maintenance']")
  123 × locator resolved to <span>under maintenance</span>
  - unexpected value "visible"
"""


def test_a_retry_counter_prefix_does_not_hide_the_resolved_line():
    """Real logs write "123 x locator resolved to ..."; fixtures wrote it bare."""
    found = triage.call_log_highlights(COUNTER_PREFIXED)
    assert "under maintenance" in found["resolved"]
    assert found["retries"] == 123


def test_step_keyword_spacing_survives_exports_without_a_trailing_space():
    document = [
        {
            "name": "f",
            "tags": [{"name": "TEST_PROJ-1"}],
            "elements": [
                {
                    "type": "scenario",
                    "name": "s",
                    "tags": [],
                    "steps": [
                        {
                            "keyword": "Then",
                            "name": "validate the sheet",
                            "result": {"status": "failed", "error_message": "E: x"},
                        }
                    ],
                }
            ],
        }
    ]
    result = triage.triage_documents([("behave_PROJ-1_2026-01-01T00-00-00.json", document)])
    assert result.failures[0].step == "Then validate the sheet"
