"""`debug eval`'s async-IIFE wrapping: top-level `await` support.

Real gap hit live: `page.evaluate("await x; y")` raises "await is only
valid in async functions and the top level bodies of modules" -- a bare
top-level `await` is invalid JS outside a module/async-function context,
no matter what Playwright does with the eventual result. `_to_async_iife`
wraps the expression so `await` works, while keeping the existing
"value of the last statement is the result" REPL convention intact for
every existing single-expression call.
"""

from __future__ import annotations

from aitlc.runtime.runner import _split_top_level_js_statements, _to_async_iife


class TestSplitTopLevelJsStatements:
    def test_single_expression_is_one_statement(self):
        assert _split_top_level_js_statements("1 + 1") == ["1 + 1"]

    def test_splits_on_top_level_semicolons(self):
        assert _split_top_level_js_statements("a; b; c") == ["a", "b", "c"]

    def test_semicolon_inside_parens_is_not_a_split_point(self):
        # A for-loop header's semicolons must stay glued to their statement.
        expr = "for (let i = 0; i < 3; i++) { x.push(i) }; x"
        assert _split_top_level_js_statements(expr) == [
            "for (let i = 0; i < 3; i++) { x.push(i) }",
            "x",
        ]

    def test_semicolon_inside_a_string_literal_is_not_a_split_point(self):
        expr = "let s = 'a; b'; s"
        assert _split_top_level_js_statements(expr) == ["let s = 'a; b'", "s"]

    def test_semicolon_inside_a_double_quoted_string_is_not_a_split_point(self):
        expr = 'let s = "a; b"; s'
        assert _split_top_level_js_statements(expr) == ['let s = "a; b"', "s"]

    def test_escaped_quote_inside_a_string_does_not_end_it_early(self):
        expr = r"let s = 'it\'s; still one string'; s"
        assert _split_top_level_js_statements(expr) == [
            r"let s = 'it\'s; still one string'",
            "s",
        ]

    def test_semicolon_inside_a_template_literal_is_not_a_split_point(self):
        expr = "let s = `a; b`; s"
        assert _split_top_level_js_statements(expr) == ["let s = `a; b`", "s"]

    def test_trailing_semicolon_does_not_produce_an_empty_trailing_statement(self):
        assert _split_top_level_js_statements("a; b;") == ["a", "b"]

    def test_empty_expression_is_no_statements(self):
        assert _split_top_level_js_statements("") == []
        assert _split_top_level_js_statements("   ") == []


class TestToAsyncIife:
    def test_wraps_a_single_expression_as_the_return_value(self):
        out = _to_async_iife("document.title")
        assert out == "(async () => {\nreturn (\ndocument.title\n);\n})()"

    def test_a_bare_top_level_await_is_now_inside_an_async_function(self):
        out = _to_async_iife("await somePromise()")
        assert "async ()" in out
        assert "await somePromise()" in out

    def test_only_the_last_statement_becomes_the_return_value(self):
        out = _to_async_iife("await new Promise(r => setTimeout(r, 10)); window.location.href")
        assert "await new Promise(r => setTimeout(r, 10));" in out
        assert "return (\nwindow.location.href\n);" in out

    def test_intermediate_declarations_are_left_as_real_statements_not_commafied(self):
        # A `let`/`const` declaration is not itself a valid expression --
        # it must stay a real statement, never get jammed into a
        # comma-expression (which would be a SyntaxError).
        out = _to_async_iife("let x = 5; x + 1")
        assert "let x = 5;" in out
        assert "return (\nx + 1\n);" in out

    def test_empty_expression_still_produces_valid_wrapper_syntax(self):
        assert _to_async_iife("") == "(async () => {})()"
