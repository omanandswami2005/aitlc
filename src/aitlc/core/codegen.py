"""Wraps `playwright codegen` and extracts selector strings from its output.

On record & replay: instead of hand-writing a first-draft step
definition from scratch, record the flow once via Playwright's own
recorder, then map the generated selectors onto EXISTING step
definitions/locators (Idea 3's "max existing step reuse" rule) rather than
committing the raw generated code. This module handles the recording +
selector-extraction half; core/locator_scan.py handles the "does this
already exist" half; commands/record_cmd.py ties them together.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path


class CodegenError(RuntimeError):
    """Raised when the Playwright recorder cannot be run."""

    pass


def run_codegen(url: str, output_path: Path, *, cwd: Path | None = None) -> None:
    """Launch Playwright's codegen recorder.

    Launch `playwright codegen <url> -o <output_path>`, inheriting stdio
    so the user sees the real recorder window and terminal instructions.
    Blocks until the user closes the recorder browser window.
    """
    playwright_bin = shutil.which("playwright")
    if not playwright_bin:
        raise CodegenError(
            "`playwright` CLI not found on PATH — install it "
            "(`pip install playwright && playwright install`) first."
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [playwright_bin, "codegen", url, "-o", str(output_path)],
        cwd=cwd,
    )
    if proc.returncode != 0:
        raise CodegenError(f"playwright codegen exited with code {proc.returncode}")


def extract_string_literals(source: str) -> list[tuple[str, int]]:
    """Return every string literal in generated code, with line numbers.

    Every string literal in the generated code, with its line number —
    codegen output isn't consistent about which method holds the selector
    (`.locator(...)`, `.get_by_role(...)`, `.click(...)`, `.fill(...)`
    all take different positions), so this deliberately doesn't try to
    parse call semantics — every string constant is a candidate, and
    core/locator_scan.py's exact-match diff naturally filters out the
    non-selector ones (values being typed into fields, etc.) by simply not
    matching anything in the known-locators map.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise CodegenError(f"Could not parse generated code: {exc}") from exc

    literals: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.strip()
        ):
            literals.append((node.value, node.lineno))
    return literals
