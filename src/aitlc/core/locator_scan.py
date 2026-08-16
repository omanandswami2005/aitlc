"""Scan a directory of Python files for dict-literal locator definitions.

Generic on purpose — no knowledge of any one project's web_locators/
module shape, just "find string-keyed, string-valued dict literals
anywhere in these files." Works for any project whose locators are plain
Python dicts (the common pattern), which is what makes this usable as
`aitlc record --suggest-steps`'s diff target regardless of project.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass
class LocatorRef:
    """Where a locator value is defined in the project's source."""

    key: str
    value: str
    file: Path
    line: int


def scan_directory(directory: Path) -> dict[str, LocatorRef]:
    """Map every locator value defined under a directory to its source.

    Return {locator_value: LocatorRef} for every string:string entry in
    every dict literal found across all .py files under `directory`.

    Keyed by value (not key) since that's what a codegen-recorded selector
    string needs to match against.
    """
    results: dict[str, LocatorRef] = {}
    if not directory.exists():
        return results

    for path in sorted(directory.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:
            continue  # not our concern here — skip unparseable files, don't crash a scan

        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            # ast.Dict guarantees keys/values are the same length, so a
            # mismatch would mean a malformed tree rather than bad input.
            for key_node, value_node in zip(node.keys, node.values, strict=True):
                key = _string_const(key_node)
                value = _string_const(value_node)
                if key is not None and value is not None and value not in results:
                    results[value] = LocatorRef(
                        key=key, value=value, file=path, line=value_node.lineno
                    )
    return results


def _string_const(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None
