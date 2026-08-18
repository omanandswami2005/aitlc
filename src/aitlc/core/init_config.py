"""Detect a project's layout so `aitlc init` can write a working config.

Asking someone to hand-write `aitlc.toml` before the tool does anything
useful is friction the tool can remove: every value in it is discoverable
from the repo. Feature and step directories are where the `.feature` files
and `@given/@when/@then` decorators actually are; the issue-key prefix is
visible in feature filenames; the per-scenario setup hook is a call inside
the project's own `before_scenario`.

Two rules keep this honest:

**Report confidence, never guess silently.** Each detection carries how it
was reached and how sure it is. A wrong value written confidently is worse
than a missing one, because the failure surfaces later and somewhere else.

**Never invent credentials.** `[env]` maps aitlc's generic names to the
project's real variable names, so it is populated only from variables the
project demonstrably uses. Values are never read or stored.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

# Directories that are never part of a project's own source.
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "site-packages",
    "aitlc",
    "dist",
    "build",
}

_STEP_DECORATORS = ("@given", "@when", "@then", "@step")

# "ABC-1234.feature" -> "ABC-". Two or more letters avoids matching a
# filename that merely happens to contain a hyphen.
_ISSUE_KEY_RE = re.compile(r"^([A-Z][A-Z0-9]{1,9})-\d+")

# Generic credential name -> (words that must all appear, words that must not).
# Matched on WORDS rather than a contiguous substring: real projects insert
# their own qualifiers, and a name like JIRA_TEST_TOKEN would never match a
# literal "jira_token" even though it obviously is one.
_ENV_HINTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "jira_email": (("jira", "email"), ("xray",)),
    "jira_token": (("jira", "token"), ("xray",)),
    "jira_xray_client_id": (("xray", "client", "id"), ()),
    "jira_xray_client_secret": (("xray", "client", "secret"), ()),
    # LambdaTest keys are qualified: a repo can carry several vendors'
    # credentials, and matching a bare "access key" would pick whichever
    # happened to be listed first.
    "lt_username": (("username",), ("jira", "aws")),
    "lt_access_key": (("access", "key"), ("aws", "secret", "id")),
    "s3_access_key_id": (("access", "key", "id"), ()),
    "s3_secret_access_key": (("secret", "access", "key"), ()),
    "s3_session_token": (("session", "token"), ()),
    "teams_webhook_url": (("webhook",), ()),
}


# Words that identify a specific vendor, so its keys win over a
# same-shaped variable belonging to a different service.
_VENDOR_WORDS = {
    "lt": ("lt", "lambdatest"),
    "s3": ("aws", "s3"),
    "jira": ("jira",),
}


@dataclass
class Detection:
    """One detected setting, with how it was found and how certain that is."""

    key: str
    value: str | None
    confidence: str  # "high" | "medium" | "none"
    evidence: str

    def to_dict(self) -> dict:
        """Return a JSON-serializable form of this detection."""
        return {
            "key": self.key,
            "value": self.value,
            "confidence": self.confidence,
            "evidence": self.evidence,
        }


@dataclass
class ProjectProfile:
    """Everything detected about a project, ready to render as config."""

    root: Path
    detections: list[Detection] = field(default_factory=list)
    env_map: dict[str, str] = field(default_factory=dict)

    def get(self, key: str) -> str | None:
        """Return the detected value for `key`, if any."""
        for detection in self.detections:
            if detection.key == key:
                return detection.value
        return None

    @property
    def unresolved(self) -> list[str]:
        """Keys nothing could be detected for."""
        return [d.key for d in self.detections if d.value is None]

    def to_dict(self) -> dict:
        """Return a JSON-serializable form of this profile."""
        return {
            "root": str(self.root),
            "detections": [d.to_dict() for d in self.detections],
            "env_map": self.env_map,
            "unresolved": self.unresolved,
        }


def _walk(root: Path) -> Iterator[Path]:
    """Yield project files, skipping vendored and generated trees."""
    for path in root.rglob("*"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            yield path


def detect_feature_dir(root: Path) -> Detection:
    """Find the directory that holds the project's feature files."""
    features = [p for p in _walk(root) if p.suffix == ".feature"]
    if not features:
        return Detection("feature_dir", None, "none", "no .feature files found")

    # The shallowest directory containing feature files is the root of the
    # suite; deeper hits are per-suite subfolders inside it.
    rel_dirs = [p.parent.relative_to(root) for p in features]
    shallowest = min(rel_dirs, key=lambda d: len(d.parts))
    top = shallowest.parts[0] if shallowest.parts else "."
    return Detection(
        "feature_dir",
        top,
        "high",
        f"{len(features)} .feature file(s) under '{top}/'",
    )


def detect_step_dir(root: Path) -> Detection:
    """Find the directory holding behave step definitions."""
    counts: Counter[str] = Counter()
    for path in _walk(root):
        if path.suffix != ".py":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(dec in text for dec in _STEP_DECORATORS):
            counts[str(path.parent.relative_to(root))] += 1

    if not counts:
        return Detection("step_dir", None, "none", "no @given/@when/@then found")

    best, count = counts.most_common(1)[0]
    confidence = "high" if count >= 3 else "medium"
    return Detection(
        "step_dir", best, confidence, f"{count} step module(s) in '{best}'"
    )


def detect_issue_key_prefix(root: Path) -> Detection:
    """Infer the issue-key prefix from feature filenames."""
    prefixes: Counter[str] = Counter()
    for path in _walk(root):
        if path.suffix != ".feature":
            continue
        match = _ISSUE_KEY_RE.match(path.stem)
        if match:
            prefixes[match.group(1)] += 1

    if not prefixes:
        return Detection(
            "issue_key_prefix", None, "none", "no ISSUE-123 style feature filenames"
        )

    best, count = prefixes.most_common(1)[0]
    return Detection(
        "issue_key_prefix",
        f"{best}-",
        "high",
        f"{count} feature file(s) named {best}-*",
    )


def detect_scenario_setup(root: Path, feature_dir: str | None) -> Detection:
    """Find the project's own per-scenario setup callable.

    Looks for a `before_scenario` hook and the project functions it calls.
    That hook is where suites generate per-scenario data, and a step slice
    run outside behave gets none of it — so naming it is what makes
    `aitlc steps run` work on data-dependent steps.
    """
    candidates: list[Path] = []
    for path in _walk(root):
        if path.suffix == ".py" and "before_scenario" in path.name:
            candidates.append(path)
        elif path.suffix == ".py":
            try:
                if "def before_scenario" in path.read_text(
                    encoding="utf-8", errors="replace"
                ):
                    candidates.append(path)
            except OSError:
                continue

    for path in candidates:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue

        # Map imported names back to the module they came from, so a call
        # inside the hook can be rendered as "module:function".
        imported: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    imported[alias.asname or alias.name] = node.module

        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name != "before_scenario":
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                name = getattr(call.func, "id", None)
                if not name or name not in imported:
                    continue
                # Must take (context, scenario) the way behave's hook does.
                if len(call.args) >= 2:
                    return Detection(
                        "scenario_setup",
                        f"{imported[name]}:{name}",
                        "high",
                        f"called by before_scenario in {path.relative_to(root)}",
                    )

    return Detection(
        "scenario_setup",
        None,
        "none",
        "no (context, scenario) call found inside before_scenario",
    )


def detect_browser_actions(root: Path) -> Detection:
    """Find a class that wraps a Playwright Page, if the project has one.

    Most behave+Playwright suites put a wrapper on `context.browser` and
    write steps against it rather than the raw Page. A step slice has to
    build the same shape or those steps fail on a missing method.
    """
    for path in _walk(root):
        if path.suffix != ".py" or "browser" not in path.name.lower():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or "action" not in node.name.lower():
                continue
            module = str(path.relative_to(root).with_suffix("")).replace("/", ".")
            return Detection(
                "browser_actions",
                f"{module}:{node.name}",
                "medium",
                f"class {node.name} in {path.relative_to(root)}",
            )
    return Detection("browser_actions", None, "none", "no Page-wrapper class found")


def detect_locators_dir(root: Path) -> Detection:
    """Find where the project defines its locators."""
    counts: Counter[str] = Counter()
    for path in _walk(root):
        if path.suffix != ".py" or "locator" not in path.name.lower():
            continue
        counts[str(path.parent.relative_to(root))] += 1

    if not counts:
        return Detection("locators_dir", None, "none", "no *locator*.py modules found")
    best, count = counts.most_common(1)[0]
    return Detection(
        "locators_dir", best, "medium", f"{count} locator module(s) in '{best}'"
    )


def detect_env_map(root: Path, env_file: str = ".env") -> dict[str, str]:
    """Map aitlc's generic credential names to the project's variable names.

    Reads only variable NAMES from the env file — never values. A project
    that keeps secrets out of the repo still gets a usable mapping, because
    the names are what aitlc needs.
    """
    path = root / env_file
    if not path.exists():
        return {}

    names: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            names.append(line.split("=", 1)[0].strip())
    except OSError:
        return {}

    mapping: dict[str, str] = {}
    for generic, (required, excluded) in _ENV_HINTS.items():
        # A vendor-specific setting should match that vendor's variable even
        # when another vendor's equivalent appears earlier in the file.
        vendor = _VENDOR_WORDS.get(generic.split("_", 1)[0])
        matches: list[str] = []
        for name in names:
            words = set(re.split(r"[^a-z0-9]+", name.lower()))
            if all(word in words for word in required) and not (words & set(excluded)):
                matches.append(name)
        if not matches:
            continue
        if vendor:
            preferred = [
                n
                for n in matches
                if set(re.split(r"[^a-z0-9]+", n.lower())) & set(vendor)
            ]
            matches = preferred or matches
        mapping[generic] = matches[0]
    return mapping


def profile_project(root: Path, *, env_file: str = ".env") -> ProjectProfile:
    """Detect everything aitlc needs to know about a project."""
    feature = detect_feature_dir(root)
    step = detect_step_dir(root)
    profile = ProjectProfile(
        root=root,
        detections=[
            Detection("name", root.name, "high", "project directory name"),
            feature,
            step,
            detect_issue_key_prefix(root),
            detect_scenario_setup(root, feature.value),
            detect_locators_dir(root),
            detect_browser_actions(root),
        ],
        env_map=detect_env_map(root, env_file),
    )
    return profile


def render_toml(profile: ProjectProfile) -> str:
    """Render a detected profile as an aitlc.toml.

    Undetected settings are emitted as commented placeholders rather than
    omitted, so the file itself shows what is still worth filling in.
    """
    lines = [
        "# Generated by `aitlc init`. Values were detected from this repo;",
        "# anything commented out could not be detected — fill it in if needed.",
        "",
        "[project]",
    ]

    for key in ("name", "issue_key_prefix", "feature_dir", "step_dir", "locators_dir"):
        detection = next((d for d in profile.detections if d.key == key), None)
        if detection and detection.value:
            lines.append(f'{key} = "{detection.value}"')
        else:
            lines.append(f'# {key} = ""   # not detected')

    actions = next((d for d in profile.detections if d.key == "browser_actions"), None)
    if actions and actions.value:
        lines.append(f'browser_actions = "{actions.value}"')
    else:
        lines.append('# browser_actions = "module:Class"   # not detected')

    setup = next((d for d in profile.detections if d.key == "scenario_setup"), None)
    lines += [
        "",
        "# The project's own per-scenario setup, called with behave's",
        "# (context, scenario) signature. `aitlc steps run` needs this: a step",
        "# slice runs outside behave and so gets no before_scenario at all.",
    ]
    if setup and setup.value:
        lines.append(f'scenario_setup = "{setup.value}"')
    else:
        lines.append('# scenario_setup = "package.module:function"   # not detected')

    lines += [
        "",
        "[env]",
        "# Maps aitlc's generic credential names to this project's real env var",
        "# names. Only names are stored here — never values.",
    ]
    if profile.env_map:
        for generic, actual in sorted(profile.env_map.items()):
            lines.append(f'{generic} = "{actual}"')
    else:
        lines.append('# jira_token = "JIRA_TOKEN"   # no .env found to infer from')

    lines += [
        "",
        "[xray]",
        'graphql_url = "https://xray.cloud.getxray.app/api/v2/graphql"',
        "",
    ]
    return "\n".join(lines)


def merge_toml(existing: str, generated: str) -> tuple[str, list[str]]:
    """Fill gaps in an existing config from a freshly detected one.

    Re-running `init` on a project that has been configured is the normal
    case, not the exception: the layout drifts, a new setting is added to
    the tool, someone wants the detector's opinion again. Overwriting is the
    wrong answer, because every hand-edit -- the ones that made the file
    correct -- is discarded silently.

    So an existing setting always wins, and only keys the file does not
    already set are added. Commented placeholders count as *unset*: they are
    what `init` writes when it cannot detect something, so a later run that
    can detect it should fill it in.

    Returns the merged text and the list of keys that were added, so the
    caller can report exactly what changed rather than "written".
    """
    set_keys = set()
    for line in existing.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("["):
            continue
        if "=" in stripped:
            set_keys.add(stripped.split("=", 1)[0].strip())

    # Walk the generated file section by section so an added key lands under
    # the heading it belongs to, not appended at the end where TOML would
    # read it as part of whichever section happens to be last.
    additions: dict[str, list[str]] = {}
    added_keys: list[str] = []
    section = ""
    for line in generated.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped
            continue
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in set_keys:
            continue
        additions.setdefault(section, []).append(line)
        added_keys.append(f"{section}{key}" if section else key)

    if not additions:
        return existing, []

    merged = existing.rstrip("\n").splitlines()
    for section, lines in additions.items():
        if section and section in (line.strip() for line in merged):
            index = max(
                i for i, line in enumerate(merged) if line.strip() == section
            )
            # Insert at the end of that section, before the next heading.
            end = index + 1
            while end < len(merged) and not merged[end].strip().startswith("["):
                end += 1
            merged[end:end] = lines
        else:
            if section:
                merged.append("")
                merged.append(section)
            merged.extend(lines)
    return "\n".join(merged) + "\n", added_keys
