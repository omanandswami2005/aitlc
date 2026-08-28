"""`--for-ai`: the centralized whitespace-compaction utility.

One rule, applied everywhere `--for-ai` is accepted (`debug eval`, `cdp
inspect`, `debug inspect`): strip trailing whitespace and collapse
blank-line runs, never touch leading/interior indentation, and print
compact JSON instead of `indent=2`.
"""

from __future__ import annotations

from aitlc.core import ai_compact


def test_strips_trailing_whitespace_per_line():
    text = "line one   \nline two\t\t\nline three"
    assert ai_compact.compact_text(text) == "line one\nline two\nline three"


def test_collapses_blank_line_runs_to_one():
    text = "a\n\n\n\n\nb"
    assert ai_compact.compact_text(text) == "a\n\nb"


def test_strips_leading_and_trailing_blank_space_of_the_whole_string():
    assert ai_compact.compact_text("\n\n  hello  \n\n") == "hello"


def test_never_touches_leading_indentation_a11y_tree_nesting_stays_intact():
    tree = "- banner:\n  - button \"x\":\n    - text: y"
    assert ai_compact.compact_text(tree) == tree


def test_empty_and_none_pass_through():
    assert ai_compact.compact_text("") == ""


def test_compact_value_walks_nested_dicts_and_lists():
    payload = {
        "tree": "a  \n\n\n\nb",
        "elements": [{"text": "x  \n\n\ny"}, {"checked": True}],
        "count": 2,
    }
    result = ai_compact.compact_value(payload)
    assert result == {
        "tree": "a\n\nb",
        "elements": [{"text": "x\n\ny"}, {"checked": True}],
        "count": 2,
    }


def test_dumps_for_ai_false_is_full_fidelity_indented_json():
    payload = {"a": "x  \n\n\ny"}
    out = ai_compact.dumps_for_ai(payload, for_ai=False)
    assert out == '{\n  "a": "x  \\n\\n\\ny"\n}'


def test_dumps_for_ai_true_compacts_strings_and_json_structure():
    payload = {"a": "x  \n\n\ny"}
    out = ai_compact.dumps_for_ai(payload, for_ai=True)
    assert out == '{"a":"x\\n\\ny"}'
