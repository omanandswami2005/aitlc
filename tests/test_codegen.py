import pytest
from aitlc.core.codegen import CodegenError, extract_string_literals


def test_extracts_string_literals_with_line_numbers():
    source = (
        "page.goto('https://example.com')\n"
        "page.get_by_role('button', name='Sign In').click()\n"
    )
    literals = extract_string_literals(source)
    values = [v for v, _ in literals]
    assert "https://example.com" in values
    assert "button" in values
    assert "Sign In" in values


def test_ignores_whitespace_only_strings():
    source = "x = '   '\ny = 'real'\n"
    literals = extract_string_literals(source)
    values = [v for v, _ in literals]
    assert "real" in values
    assert "   " not in values


def test_invalid_python_raises_codegen_error():
    with pytest.raises(CodegenError):
        extract_string_literals("this is not : valid python {{{")


def test_line_numbers_are_correct():
    source = "a = 1\nb = 'target'\n"
    literals = extract_string_literals(source)
    line_for_target = next(line for v, line in literals if v == "target")
    assert line_for_target == 2
