import zipfile
from pathlib import Path

import pytest
from aitlc.core.trace_evidence import (
    TraceEvidenceError,
    extract_last_frame,
    list_screencast_frames,
)


def _make_fake_trace(tmp_path: Path, frame_specs: list[tuple[str, bytes]]) -> Path:
    zip_path = tmp_path / "trace.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, data in frame_specs:
            zf.writestr(name, data)
    return zip_path


def test_lists_frames_sorted_by_timestamp(tmp_path: Path):
    trace = _make_fake_trace(
        tmp_path,
        [
            ("resources/page@abc123-1700000003000.jpeg", b"third"),
            ("resources/page@abc123-1700000001000.jpeg", b"first"),
            ("resources/page@abc123-1700000002000.jpeg", b"second"),
            ("trace.trace", b"not a frame"),
            ("resources/other.txt", b"not a frame either"),
        ],
    )
    frames = list_screencast_frames(trace)
    assert [f.epoch_ms for f in frames] == [1700000001000, 1700000002000, 1700000003000]


def test_extracts_the_last_frame_by_timestamp(tmp_path: Path):
    trace = _make_fake_trace(
        tmp_path,
        [
            ("resources/page@abc-1700000001000.jpeg", b"OLDEST"),
            ("resources/page@abc-1700000099000.jpeg", b"NEWEST"),
            ("resources/page@abc-1700000050000.jpeg", b"MIDDLE"),
        ],
    )
    out = tmp_path / "out" / "frame.jpg"
    result = extract_last_frame(trace, out)
    assert result == out
    assert out.read_bytes() == b"NEWEST"


def test_no_frames_raises(tmp_path: Path):
    trace = _make_fake_trace(tmp_path, [("trace.trace", b"nothing")])
    with pytest.raises(TraceEvidenceError):
        extract_last_frame(trace, tmp_path / "out.jpg")


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(TraceEvidenceError):
        list_screencast_frames(tmp_path / "does-not-exist.zip")
