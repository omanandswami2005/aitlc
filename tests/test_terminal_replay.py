from pathlib import Path

from aitlc.core.terminal_replay import replay_to_html


def _write_log(tmp_path: Path, raw: bytes) -> Path:
    path = tmp_path / "capture.log"
    path.write_bytes(raw)
    return path


def test_resolves_cursor_up_redraw_keeps_only_final_state(tmp_path: Path):
    # Real pattern behave's pretty formatter uses: print a step in gray
    # ("running"), then move the cursor up and repaint it in green
    # ("passed") once the result is known. A naive text/line parser sees
    # both lines; a real terminal only ever shows the final, green one.
    # Real captures go through a pty, which translates \n to \r\n (resets
    # the column) — bare \n here would leave the cursor mid-row and produce
    # unrealistic test data (caught by this test's first draft failing).
    raw = (
        b"\x1b[90mWhen do a thing\x1b[0m\r\n"
        b"\x1b[1A\x1b[32mWhen do a thing\x1b[0m\r\n"
    )
    log = _write_log(tmp_path, raw)
    html = replay_to_html(log, cols=80)

    assert html.count("do a thing") == 1  # not duplicated
    assert "#4caf50" in html  # green (passed) survives
    assert "#8a8a8a" not in html  # the gray (running) line does not


def test_plain_text_with_no_redraw_passes_through(tmp_path: Path):
    raw = b"Plain line one\nPlain line two\n"
    log = _write_log(tmp_path, raw)
    html = replay_to_html(log, cols=80)
    assert "Plain line one" in html
    assert "Plain line two" in html


def test_missing_log_raises_capture_error(tmp_path: Path):
    import pytest
    from aitlc.core.terminal_replay import CaptureError

    with pytest.raises(CaptureError):
        replay_to_html(tmp_path / "nope.log")


def test_bold_text_gets_font_weight(tmp_path: Path):
    raw = b"\x1b[1mBold text\x1b[0m\n"
    log = _write_log(tmp_path, raw)
    html = replay_to_html(log, cols=80)
    assert "font-weight:700" in html
    assert "Bold text" in html


def test_html_escapes_special_characters(tmp_path: Path):
    raw = b'validate xpath: "<foo>"\n'
    log = _write_log(tmp_path, raw)
    html = replay_to_html(log, cols=80)
    assert "&lt;foo&gt;" in html
    assert "&quot;" in html
