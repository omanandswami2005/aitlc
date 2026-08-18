"""Keep what was fetched, so re-examining a run is free.

Reports are downloaded to wherever `--out` said and then forgotten, so looking
at the same CI run twice downloads it twice -- and these are large files. The
project already caches remote bundles per job; this is the same idea keyed by
the object's own path, so a cache hit is provable rather than hopeful.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from aitlc.core import workspace


@dataclass
class CacheEntry:
    """What a cached artifact is and where it came from."""

    key: str
    path: str
    size_bytes: int
    fetched_at: float
    source: str


def cache_dir(root_dir: Path) -> Path:
    """Root of the artifact cache."""
    return workspace.output_path(root_dir, ".aitlc", "artifacts")


def _safe_name(key: str) -> str:
    """A filesystem-safe name that still hints at the original key.

    Hashed rather than sanitised because S3 keys are long, nested and can
    collide once slashes are flattened; the tail is kept so a human can tell
    what a cached file is without opening the index.
    """
    digest = hashlib.sha256(key.encode()).hexdigest()[:12]
    tail = key.rsplit("/", 1)[-1][-60:]
    return f"{digest}-{tail}"


def path_for(root_dir: Path, key: str) -> Path:
    """Where this key would live, whether or not it is cached yet."""
    return cache_dir(root_dir) / _safe_name(key)


def get(root_dir: Path, key: str) -> Path | None:
    """The cached file for this key, or None."""
    path = path_for(root_dir, key)
    return path if path.exists() else None


def put(root_dir: Path, key: str, source_file: Path, *, source: str = "") -> Path:
    """Copy a fetched file into the cache and index it."""
    target = path_for(root_dir, key)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_file, target)
    entry = CacheEntry(
        key=key,
        path=str(target),
        size_bytes=target.stat().st_size,
        fetched_at=time.time(),
        source=source,
    )
    index_path = cache_dir(root_dir) / "index.json"
    index = {}
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text())
        except json.JSONDecodeError:
            index = {}
    index[key] = asdict(entry)
    index_path.write_text(json.dumps(index, indent=2))
    return target


def stats(root_dir: Path) -> dict:
    """Size and count, for deciding whether to prune."""
    directory = cache_dir(root_dir)
    if not directory.exists():
        return {"entries": 0, "bytes": 0}
    files = [p for p in directory.glob("*") if p.is_file() and p.name != "index.json"]
    return {"entries": len(files), "bytes": sum(p.stat().st_size for p in files)}


def clear(root_dir: Path) -> int:
    """Empty the cache. Returns how many files went."""
    directory = cache_dir(root_dir)
    if not directory.exists():
        return 0
    removed = 0
    for path in directory.glob("*"):
        if path.is_file():
            path.unlink()
            removed += 1
    return removed
