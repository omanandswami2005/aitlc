"""`cdp inspect --interactive` / `debug inspect --interactive`.

`accessibility` (Playwright's `aria_snapshot()`) answers "what's on screen"
cheaply but carries no DOM identity -- no id/name/value, since it's a pure
role/name/state tree. Turning a spotted control into a real locator has, in
practice, meant a second hand-written `debug eval` JS round trip. This is
that lookup folded into the same call `cdp_attach._interactive_elements`
drives.

The browser-side JS itself (`_INTERACTIVE_ELEMENTS_JS`) is exercised
end-to-end by hand against a live CDP browser -- these unit tests fake
`page.evaluate` to return what that JS would, and check the Python-side
contract (query filtering, limit/truncation, selector scoping, error
handling) that wraps it.
"""

from __future__ import annotations

from aitlc.core import cdp_attach

ELEMENTS = [
    {"tag": "input", "id": "AMAZING_MAIL", "name": "fulfillmentProviderOption",
     "type": "radio", "value": "AMAZING_MAIL", "checked": True},
    {"tag": "input", "id": "CUSTOM", "name": "fulfillmentProviderOption",
     "type": "radio", "value": "CUSTOM", "checked": False},
    {"tag": "input", "id": "allowCurrentResidentOption", "type": "checkbox", "checked": True},
    {"tag": "button", "text": "Save"},
]


class _Locator:
    def __init__(self, handle):
        self._handle = handle

    def element_handle(self):
        return self._handle


class _Page:
    def __init__(self, elements=ELEMENTS, raises=False):
        self._elements = elements
        self._raises = raises
        self.calls = []

    def evaluate(self, script, arg=None):
        self.calls.append((script, arg))
        if self._raises:
            raise RuntimeError("page closed")
        return self._elements

    def locator(self, selector):
        return _Locator(handle=f"handle:{selector}")


def test_returns_the_elements_with_counts():
    result = cdp_attach._interactive_elements(_Page())
    assert result["total_visible_interactive"] == 4
    assert result["matched"] == 4
    assert result["truncated"] is False
    assert result["elements"] == ELEMENTS


def test_query_filters_by_substring_across_any_field():
    result = cdp_attach._interactive_elements(_Page(), query="amazing")
    assert result["matched"] == 1
    assert result["elements"][0]["id"] == "AMAZING_MAIL"
    assert result["query"] == "amazing"


def test_query_is_case_insensitive_and_checks_every_field():
    # "CUSTOM" only appears in the value/id of the second element, not in
    # any obviously-named field a naive "search the name" filter would check.
    result = cdp_attach._interactive_elements(_Page(), query="custom")
    assert result["matched"] == 1
    assert result["elements"][0]["id"] == "CUSTOM"


def test_limit_caps_the_list_but_reports_the_true_count():
    result = cdp_attach._interactive_elements(_Page(), limit=2)
    assert result["matched"] == 4
    assert result["truncated"] is True
    assert len(result["elements"]) == 2


def test_selector_scopes_the_walk_and_passes_the_element_handle():
    page = _Page()
    result = cdp_attach._interactive_elements(page, selector="#panel")
    assert result["scoped_to"] == "#panel"
    script, arg = page.calls[0]
    assert arg == "handle:#panel"


def test_unscoped_walk_passes_none_so_the_js_defaults_to_document():
    page = _Page()
    cdp_attach._interactive_elements(page)
    script, arg = page.calls[0]
    assert arg is None
    assert "root = root || document" in script


def test_missing_selector_target_reports_an_error_not_a_crash():
    class _NoElementLocator:
        def element_handle(self):
            return None

    class _PageMissingSelector(_Page):
        def locator(self, selector):
            return _NoElementLocator()

    result = cdp_attach._interactive_elements(_PageMissingSelector(), selector="#gone")
    assert "error" in result
    assert "#gone" in result["error"]


def test_a_closed_page_reports_the_error_and_does_not_raise():
    result = cdp_attach._interactive_elements(_Page(raises=True))
    assert "error" in result
    assert "RuntimeError" in result["error"]


def test_inspection_result_only_carries_interactive_when_requested():
    empty = cdp_attach.InspectionResult(
        url="https://example.test", viewport=None, screenshot_path=None, checks=[]
    )
    assert "interactive" not in empty.to_dict()

    populated = cdp_attach.InspectionResult(
        url="https://example.test",
        viewport=None,
        screenshot_path=None,
        checks=[],
        interactive={"matched": 1, "elements": [ELEMENTS[0]]},
    )
    assert populated.to_dict()["interactive"]["matched"] == 1
