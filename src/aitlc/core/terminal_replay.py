r"""Capture + faithfully replay real colored terminal output (FR-5.1).

On record & replay: behave repaints steps in place with cursor-up
escape codes (`\\x1b[3A`) as they resolve from "running" to "passed". A
naive text parser (even one that tries to collapse `\\r`-delimited
progress lines) silently DROPS exactly the lines that matter, because it
doesn't understand cursor movement — only a real terminal emulator resolves
those redraws correctly. Built and verified live with `pyte`.

Also verified live: replaying at the WRONG column width corrupts the
output even with a real terminal emulator — a line that wrapped across N
physical rows at the real capture width won't wrap the same way replayed
at a different width, so `\\x1b[3A`'s "up 3 lines" lands on the wrong
content. 80 columns matched this project's real capture width; that's a
default here, not a hardcoded assumption — always replay at the width the
capture actually used if it's known.
"""

from __future__ import annotations

import html
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyte

FG_MAP = {
    "red": "#ff6b6b",
    "green": "#4caf50",
    "cyan": "#4dd0e1",
    "black": "#8a8a8a",
    "brightblack": "#8a8a8a",
    "brightred": "#ff6b6b",
    "brightgreen": "#4caf50",
    "brightcyan": "#4dd0e1",
    "white": "#d4d4d4",
    "default": None,
}


class CaptureError(RuntimeError):
    """Raised when a terminal recording cannot be captured."""

    pass


def capture(
    command: list[str], *, cwd: Path, log_path: Path, env: dict[str, str] | None = None
) -> int:
    """Run a command under `script` so ANSI color survives capture.

    Run `command` under `script` so real color codes survive being
    captured to a file (a plain pipe strips them — `isatty()` checks in the
    child process see a non-tty and disable color output entirely).

    macOS/BSD `script` syntax specifically (`script -q logfile cmd...`) —
    this project's actual dev environment. Linux's util-linux `script` uses
    a different argument order (`script -qc "cmd" logfile`); adapt if
    porting to Linux.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    import os

    proc_env = {**os.environ, **(env or {})} if env else None
    # nosec B607 - `script` is intentionally resolved via PATH; its path
    # differs across platforms (util-linux vs BSD) and the command it
    # wraps is one this tool constructed, not user input.
    proc = subprocess.run(  # nosec B607 - `script` via PATH is intentional
        ["script", "-q", str(log_path), *command],
        cwd=cwd,
        env=proc_env,
    )
    return proc.returncode


def _render_line(line_map: Any) -> str:
    if not line_map:
        return ""
    max_col = max(line_map.keys())
    buf: list[str] = []
    cur_fg = None
    cur_bold = None
    open_span = False
    for col in range(max_col + 1):
        ch = line_map.get(col)
        if ch is None:
            fg, bold, data = None, False, " "
        else:
            fg, bold, data = ch.fg, ch.bold, ch.data
        if fg != cur_fg or bold != cur_bold:
            if open_span:
                buf.append("</span>")
                open_span = False
            color = FG_MAP.get(fg)
            css = []
            if color:
                css.append(f"color:{color}")
            if bold:
                css.append("font-weight:700")
            if css:
                buf.append(f'<span style="{";".join(css)}">')
                open_span = True
            cur_fg, cur_bold = fg, bold
        buf.append(html.escape(data))
    if open_span:
        buf.append("</span>")
    return "".join(buf).rstrip()


def replay_to_html(log_path: Path, *, cols: int = 80, history: int = 6000) -> str:
    """Render a captured terminal log to HTML via a real emulator.

    Feed a captured log through a real terminal emulator (pyte) and
    return the resolved final screen state as HTML spans, one line per
    output line. This is what correctly resolves behave's cursor-redraw
    repaints — see module docstring.
    """
    if not log_path.exists():
        raise CaptureError(f"No captured log at {log_path}")

    raw = log_path.read_bytes()
    text = raw.decode("utf-8", errors="replace")

    screen = pyte.HistoryScreen(cols, 60, history=history)
    stream = pyte.Stream(screen)
    stream.feed(text)

    all_lines = list(screen.history.top) + [
        screen.buffer[i] for i in range(screen.lines)
    ]
    rendered = [_render_line(line) for line in all_lines]

    while rendered and rendered[0] == "":
        rendered.pop(0)
    while rendered and rendered[-1] == "":
        rendered.pop()

    return "\n".join(rendered)


@dataclass
class TestReportEntry:
    """One test's row in a captured report."""

    test_id: str
    passed: bool
    summary: str
    terminal_html: str
