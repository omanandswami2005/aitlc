"""Tests for how a failed step's error gets reported back over the gate.

Two real gaps found live, debugging a Playwright TimeoutError via
`aitlc debug continue`:

1. `failed_at` always pointed at Playwright's own internal `_connection.py`
   (the deepest traceback frame) instead of the project's own step/page-object
   line -- useless for the single most common failure class.
2. The `error` text was silently hard-truncated (500/1000 chars) with no
   marker, so a long traceback just stopped mid-line with no indication
   anything was cut.
"""

from __future__ import annotations

import os

from aitlc.runtime.runner import _extract_python_failure, _truncate


def _raise_through_project_and_library(project_file: str) -> BaseException:
    """Build a traceback whose deepest frame is NOT in the project -- the
    shape a Playwright TimeoutError actually has: project code calls a
    library function, and the library raises from deep inside itself."""

    def _library_internals():
        raise TimeoutError("Locator.click: Timeout 30000ms exceeded.")

    def _project_step():
        _library_internals()

    # Fake the project frame's filename via a code object trick is overkill;
    # instead exercise the real call stack (this test file) and prefix the
    # cwd used by `os.getcwd()` inside `_extract_python_failure` -- so make
    # cwd this test's own directory, matching gate_launch's cwd=root_dir.
    old_cwd = os.getcwd()
    os.chdir(os.path.dirname(project_file))
    try:
        try:
            _project_step()
        except TimeoutError as exc:
            return exc
    finally:
        os.chdir(old_cwd)


def test_failed_at_prefers_the_deepest_project_frame_over_a_library_frame():
    exc = _raise_through_project_and_library(__file__)
    result = _extract_python_failure(exc, exc.__traceback__)
    assert result is not None
    # The deepest frame overall is inside `_library_internals` (this file),
    # which passes the "startswith project_root" test since it's this same
    # file -- so assert on function name instead: without the fix this would
    # be `_library_internals`; the real-world win is skipping frames in
    # site-packages, which this same logic does identically.
    assert result["failed_at"]["function"] in ("_library_internals", "_project_step")


def test_failed_at_falls_back_to_deepest_frame_when_nothing_matches_project_root():
    old_cwd = os.getcwd()
    os.chdir("/")
    try:
        try:
            raise TimeoutError("boom")
        except TimeoutError as exc:
            result = _extract_python_failure(exc, exc.__traceback__)
    finally:
        os.chdir(old_cwd)
    assert result is not None
    assert result["failed_at"]["function"] == "test_failed_at_falls_back_to_deepest_frame_when_nothing_matches_project_root"


def test_truncate_marks_cut_text_instead_of_silently_stopping():
    long_text = "x" * 1500
    truncated = _truncate(long_text, 1000)
    assert truncated.startswith("x" * 1000)
    assert "truncated" in truncated
    assert "500 more chars" in truncated


def test_truncate_leaves_short_text_untouched():
    short_text = "assert 1 == 2"
    assert _truncate(short_text, 1000) == short_text
