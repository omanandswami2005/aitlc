"""Locator shapes that silently match the wrong element.

Each rule is taken from a defect, and each test names it, so nobody later
"tidies away" a rule without knowing what it caught.
"""

from __future__ import annotations

from aitlc.core import locator_lint

MODULE = """
class PageLocators:
    locators = {
        "account_row": "//div[@aria-rowindex='2']//div[@data-field='title']//a",
        "credits_cell": "//*[@data-rowindex='0']//div[@data-field='AVAILABLE_CREDITS']",
        "scoped_cell": "//div[@role='row'][.//div[@role='cell' and @data-field='USER']]"
                       "//div[@role='cell' and @data-field='AVAILABLE']",
        "by_test_id": "//div[@data-testid='navUsers']",
        "plain_id": "searchAccount",
        "classy": "//div[contains(@class,'MuiChip')]//span",
    }
"""


def _by_name(findings, name):
    return [f for f in findings if f.name == name]


def test_positional_selectors_are_high_severity():
    """`aria-rowindex='2'` always opened the first result -- an orphan record."""
    findings = locator_lint.lint_text(MODULE)
    hit = _by_name(findings, "account_row")
    assert any(f.rule == "positional-index" and f.severity == "high" for f in hit)


def test_grid_field_without_role_cell_is_flagged():
    """A grid header carries data-field too, so this can return the column title."""
    findings = locator_lint.lint_text(MODULE)
    hit = _by_name(findings, "credits_cell")
    assert any(f.rule == "grid-cell-without-role" for f in hit)


def test_a_properly_scoped_grid_selector_is_not_flagged():
    findings = locator_lint.lint_text(MODULE)
    assert _by_name(findings, "scoped_cell") == []


def test_a_test_id_anchor_is_not_noise():
    """An early version flagged every //div[@data-testid=...] and produced 763
    findings, which is the same as producing none."""
    findings = locator_lint.lint_text(MODULE)
    assert _by_name(findings, "by_test_id") == []


def test_plain_ids_are_ignored_entirely():
    findings = locator_lint.lint_text(MODULE)
    assert _by_name(findings, "plain_id") == []


def test_class_only_xpath_is_reported_but_only_as_low():
    findings = locator_lint.lint_text(MODULE)
    hit = _by_name(findings, "classy")
    assert hit and all(f.severity == "low" for f in hit)


def test_findings_are_sorted_by_severity(tmp_path):
    path = tmp_path / "loc.py"
    path.write_text(MODULE)
    findings = locator_lint.lint_paths([path])
    severities = [f.severity for f in findings]
    assert severities == sorted(
        severities, key=lambda s: {"high": 0, "medium": 1, "low": 2}[s]
    )


def test_every_finding_carries_a_concrete_rewrite():
    """Naming 190 risky locators without a fix is a backlog nobody starts.

    Upstream guidance is specific -- scope with a relative predicate (the xpath
    analogue of locator.filter(has=...)) or select by role -- so the finding
    should carry that, not just the diagnosis.
    """
    findings = locator_lint.lint_text(MODULE)
    assert findings
    assert all(f.suggestion for f in findings)
    positional = [f for f in findings if f.rule == "positional-index"][0]
    assert "filter(has=" in positional.suggestion
