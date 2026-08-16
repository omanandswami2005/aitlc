"""Build the same tool under both distribution names.

aitlc is published twice — as `aitlc` and as `dax-aitlc` — so an org that
prefers a namespaced package gets one, while the generic name stays
available. Both install the identical code and both provide the `aitlc`
command; only the distribution name on the index differs.

Doing this by hand means editing pyproject, building, editing it back, and
remembering to revert if the build fails. This does it in one step and
always restores the original file, including on error.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
PRIMARY = "aitlc"
ALTERNATE = "dax-aitlc"


def _set_name(text: str, name: str) -> str:
    """Return pyproject text with the distribution name replaced."""
    updated, count = re.subn(
        r'(?m)^name\s*=\s*"[^"]+"', f'name = "{name}"', text, count=1
    )
    if count != 1:
        raise SystemExit('could not find a single `name = "..."` in pyproject.toml')
    return updated


def build(name: str, outdir: Path) -> None:
    """Build sdist + wheel under `name`, always restoring pyproject."""
    original = PYPROJECT.read_text()
    try:
        PYPROJECT.write_text(_set_name(original, name))
        subprocess.run(
            [sys.executable, "-m", "build", "--outdir", str(outdir), str(ROOT)],
            check=True,
        )
    finally:
        # Restored even when the build raises, so a failed run never leaves
        # the repo holding a name nobody chose.
        PYPROJECT.write_text(original)


def main() -> None:
    """Build both distribution names into one output directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", default="dist", type=Path)
    parser.add_argument(
        "--only",
        choices=[PRIMARY, ALTERNATE],
        help="Build just one of the two names.",
    )
    args = parser.parse_args()

    outdir = (ROOT / args.outdir).resolve()
    if outdir.exists():
        shutil.rmtree(outdir)
    outdir.mkdir(parents=True)

    for name in [args.only] if args.only else [PRIMARY, ALTERNATE]:
        print(f"--- building {name} ---", flush=True)
        build(name, outdir)

    print("\nbuilt:")
    for path in sorted(outdir.iterdir()):
        print(f"  {path.name}")


if __name__ == "__main__":
    main()
