"""Minimal .env loader — generic, not project-specific.

A minimal .env reader. Deliberately not python-dotenv: every real run this
session depended on. Doesn't overwrite variables already set in the shell,
so an explicit `FOO=bar aitlc run ...` still wins over .env.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path) -> bool:
    """Load key=value pairs from path into os.environ.

    Returns False if the file doesn't exist (not an error — .env is optional).
    """
    if not path.exists():
        return False
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key not in os.environ:
                os.environ[key] = value
    return True
