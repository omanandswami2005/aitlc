"""Feature-file selection helpers: line specs, discovery, and tag filtering.

Two real gaps this closes:

1. **`FILE:LINE` was silently dropped.** behave natively supports
   `path/to/x.feature:42` (`behave --help`: `[DIR|FILE|FILE:LINE]`), but
   `AitlcConfig.resolve_feature_path` runs the id through `Path(...).stem`,
   which strips a trailing `:42` and resolves to the bare feature file. The
   run then executed the WHOLE file while the user believed they had
   targeted one scenario — wrong behavior with no error. `split_line_spec`
   separates the line before resolution so it can be re-attached for behave.

2. **Selecting what to run required editing tags.** A common runner shape
   globs `features/*.feature` and runs every top-level feature, so
   narrowing a run means physically adding a skip tag to every other file
   and reverting it afterwards. `discover_features` + `feature_tags`
   reproduce that selection in-process, honoring the same skip-tag
   semantics, without touching a file.

Note `glob.glob("features/*.feature")` is NOT recursive, so such a runner
never sees nested files like `features/PROJ-1234/*.feature`.
`discover_features` defaults to recursive so those are reachable, and takes
`recursive=False` when byte-for-byte parity with that behavior matters.

Pure path/text logic — no behave import, no network — so it is unit-testable
without a project environment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# A trailing ":<digits>" — but only when it really is a line spec. A bare
# Windows drive prefix ("C:\...") or a colon inside a directory name must
# not be mistaken for one, so the digits are anchored to end-of-string.
_LINE_SPEC_RE = re.compile(r"^(?P<base>.+?):(?P<line>\d+)$")

_TAG_LINE_RE = re.compile(r"^\s*@")
_FEATURE_LINE_RE = re.compile(r"^\s*Feature\s*:")

DEFAULT_SKIP_TAG = "skip_xray_test"


@dataclass(frozen=True)
class FeatureSelection:
    """One resolved feature file plus why it was (or wasn't) selected."""

    path: Path
    tags: frozenset[str]
    skipped_by: str | None = None

    @property
    def selected(self) -> bool:
        """True when nothing excluded this feature."""
        return self.skipped_by is None


def split_line_spec(test_id: str) -> tuple[str, int | None]:
    """Split a trailing behave ``:LINE`` off a test id / feature path.

    ``"PROJ-24026.feature:30" -> ("PROJ-24026.feature", 30)``
    ``"PROJ-24026"            -> ("PROJ-24026", None)``

    Only a trailing all-digits segment counts, so an id that merely
    contains a colon is returned untouched.
    """
    match = _LINE_SPEC_RE.match(test_id)
    if not match:
        return test_id, None
    return match.group("base"), int(match.group("line"))


def attach_line_spec(path: Path, line: int | None) -> str:
    """Render a resolved path back into behave's ``FILE:LINE`` argument."""
    return f"{path}:{line}" if line is not None else str(path)


def feature_tags(path: Path) -> frozenset[str]:
    """Read the ``@tag`` names that apply to a feature file's ``Feature:``.

    Only tag lines ABOVE the ``Feature:`` line are feature-level tags —
    scenario tags further down do not gate the whole file, and
    A suite's own hooks often read ``feature.tags`` specifically (a real gotcha in one
    project: Xray exports Jira labels as *scenario* tags, so a file pulled
    from Jira can look tagged while ``feature.tags`` is empty).

    Returns bare names without the ``@`` so callers compare against
    ``"skip_xray_test"``, matching ``skip_checks.py``.
    """
    tags: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset()

    for raw_line in text.splitlines():
        if _FEATURE_LINE_RE.match(raw_line):
            break
        if _TAG_LINE_RE.match(raw_line):
            for token in raw_line.split():
                if token.startswith("@"):
                    tags.add(token[1:])
    return frozenset(tags)


def discover_features(feature_root: Path, *, recursive: bool = True) -> list[Path]:
    """All ``.feature`` files under ``feature_root``, sorted for stable order.

    Sorted so a parallel run's feature->slot assignment is reproducible
    between invocations; unsorted filesystem order would make a flaky
    parallel failure much harder to reproduce.
    """
    pattern = "**/*.feature" if recursive else "*.feature"
    return sorted(feature_root.glob(pattern))


def scenario_tags(path: Path) -> frozenset[str]:
    """Tag names appearing below the ``Feature:`` line.

    Kept separate from ``feature_tags`` because the distinction is load-
    bearing for hooks -- but a *skip* tag means "do not run this" wherever it
    is written, and a suite's own skip check applies the base tag at scenario
    level too. Reading only feature-level tags meant every such file cost a
    spawned behave process to discover a skip that was visible in the text.
    """
    tags: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset()

    seen_feature = False
    for raw_line in text.splitlines():
        if _FEATURE_LINE_RE.match(raw_line):
            seen_feature = True
            continue
        if seen_feature and _TAG_LINE_RE.match(raw_line):
            for token in raw_line.split():
                if token.startswith("@"):
                    tags.add(token[1:])
    return frozenset(tags)


def skip_tag_variants(skip_tag: str, environment: str | None = None) -> list[str]:
    """The base skip tag plus the environment-specific forms of it.

    A suite's skip check honours `<tag>_prod` / `_stage` / `_dev` as well as
    the bare tag. Matching only the bare one let an environment-tagged file
    launch a process that immediately skipped itself.

    With no environment named, every known variant is treated as a skip: the
    pre-filter's job is to avoid paying for a process that will skip anyway,
    and being wrong in that direction only costs a file being listed as
    skipped when it would have skipped itself.
    """
    known = ("prod", "stage", "dev")
    if environment:
        suffix = environment.strip().lower()
        return [skip_tag, f"{skip_tag}_{suffix}"]
    return [skip_tag, *(f"{skip_tag}_{name}" for name in known)]


def select_features(
    paths: list[Path],
    *,
    skip_tag: str | None = DEFAULT_SKIP_TAG,
    environment: str | None = None,
    extra_skip_tags: list[str] | None = None,
) -> list[FeatureSelection]:
    """Annotate each path with its feature-level tags and skip decision.

    Returns every path — including skipped ones — so a caller can report
    what was excluded and why. Silently dropping them would reproduce the
    exact ambiguity this replaces, where a mis-tagged file looked identical
    to a file that simply wasn't picked up.
    """
    wanted: list[str] = []
    if skip_tag:
        wanted.extend(skip_tag_variants(skip_tag, environment))
    wanted.extend(extra_skip_tags or [])

    selections: list[FeatureSelection] = []
    for path in paths:
        tags = feature_tags(path)
        # A skip applies wherever it is written; hooks care about placement,
        # a skip decision does not.
        all_tags = tags | scenario_tags(path)
        skipped_by = next((tag for tag in wanted if tag in all_tags), None)
        selections.append(FeatureSelection(path=path, tags=tags, skipped_by=skipped_by))
    return selections
