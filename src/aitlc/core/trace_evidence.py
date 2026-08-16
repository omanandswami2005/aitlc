"""Playwright trace evidence extraction.

On evidence pulling: a trace .zip contains its screencast
frames as individual JPEGs under resources/, named
`page@<hash>-<epoch_ms>.jpeg`. Sorting by the numeric timestamp in the
filename and taking the LAST one gives a real screenshot of the page at
(or very near) the end of the run — a fast "just show me a screenshot"
path that doesn't need `playwright show-trace`'s interactive viewer.
Verified live against a real bucket object.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

_FRAME_RE = re.compile(r"^resources/page@[^-]+-(\d+)\.jpeg$")


class TraceEvidenceError(RuntimeError):
    """Raised when a trace archive cannot be read."""

    pass


@dataclass
class FrameInfo:
    """One screencast frame inside a trace."""

    zip_path: str
    epoch_ms: int


def list_screencast_frames(trace_zip: Path) -> list[FrameInfo]:
    """List every screencast frame in a trace zip, sorted oldest to newest."""
    if not trace_zip.exists():
        raise TraceEvidenceError(f"No trace file at {trace_zip}")

    frames: list[FrameInfo] = []
    with zipfile.ZipFile(trace_zip) as zf:
        for name in zf.namelist():
            match = _FRAME_RE.match(name)
            if match:
                frames.append(FrameInfo(zip_path=name, epoch_ms=int(match.group(1))))

    frames.sort(key=lambda f: f.epoch_ms)
    return frames


def extract_last_frame(trace_zip: Path, output_path: Path) -> Path:
    """Extract the most recent screencast frame from a trace.

    Extract the last (most recent) screencast frame — a real screenshot
    of the page at/near the end of the recorded run.
    """
    frames = list_screencast_frames(trace_zip)
    if not frames:
        raise TraceEvidenceError(f"No screencast frames found in {trace_zip}")

    last = frames[-1]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(trace_zip) as zf:
        data = zf.read(last.zip_path)
    output_path.write_bytes(data)
    return output_path
