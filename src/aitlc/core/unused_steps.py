"""Find step definitions no feature file uses.

Cucumber ships this (`cucumber --dry-run -f stepdefs` prints "NOT MATCHED
BY ANY STEPS"; the Java runner has an `unused:` plugin). behave has no
equivalent, so unused step definitions accumulate silently — they still
import, still pass review, and still have to be maintained and migrated.

Correctness here rests on two decisions:

**Match with behave's own registry, not a regex.** A step definition is a
pattern (`parse` or `re`), and reimplementing that matching would disagree
with the runner on exactly the tricky cases — optional groups, custom
types, `re` steps — producing confident, wrong answers. This asks
`registry.find_match` the same question behave asks at runtime.

**Count steps invoked from other steps.** Cucumber's own documentation
warns that its unused report has false positives for steps called only
from other steps. In behave that is `context.execute_steps("...")`, which
is common in composite steps. Those literals are extracted from the step
modules' AST and matched too, so a step used only by another step is
correctly reported as *used*.

Runs inside the TARGET project's interpreter (behave and the project's own
step modules must import), so it depends on nothing from aitlc.
"""

from __future__ import annotations

import ast
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class UnusedStepsResult:
    """Which registered step definitions no feature file reaches."""

    total_registered: int = 0
    total_feature_steps: int = 0
    feature_files_scanned: int = 0
    unused: list[dict] = field(default_factory=list)
    used_only_by_other_steps: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Return a JSON-serializable form of this result."""
        return {
            "total_registered": self.total_registered,
            "total_feature_steps": self.total_feature_steps,
            "feature_files_scanned": self.feature_files_scanned,
            "unused_count": len(self.unused),
            "unused": self.unused,
            "used_only_by_other_steps": self.used_only_by_other_steps,
            "warnings": self.warnings,
            "errors": self.errors,
        }

    def add_confidence_warnings(self) -> None:
        """Flag results that the scanned corpus cannot actually support.

        This report is only ever as complete as the feature files it can
        see. Suites that keep their canonical Gherkin elsewhere — a test
        manager such as Xray, or a separate repo — expose only a subset
        locally, and every step used exclusively by an unseen feature then
        looks dead.

        Measured on a real suite: 51 local feature files against a much
        larger managed suite reported 83% of step definitions as unused.
        Acting on that number would have deleted live code, so a result
        this shape is reported as low confidence rather than as a finding.
        """
        if not self.total_registered:
            return

        ratio = len(self.unused) / self.total_registered
        if ratio > 0.5:
            self.warnings.append(
                f"{ratio:.0%} of step definitions appear unused across "
                f"{self.feature_files_scanned} feature file(s). That usually means "
                "the scanned features are an incomplete copy of the suite (for "
                "example, canonical Gherkin held in a test manager) rather than "
                "genuinely dead code. Verify the corpus is complete before "
                "deleting anything."
            )
        if self.feature_files_scanned == 0:
            self.warnings.append(
                "no feature files were found, so every step definition is "
                "trivially unused; check --feature-dir"
            )


def extract_execute_steps_literals(step_dir: Path) -> list[str]:
    """Return every Gherkin string passed to `context.execute_steps(...)`.

    Parsed from the AST rather than matched with a regex so that multi-line
    and implicitly concatenated literals — the normal way a composite step
    is written — are captured whole instead of truncated at a line break.

    Only literal arguments can be resolved; a step text built at runtime
    from a variable is not knowable statically, which is a real limit worth
    stating rather than hiding.
    """
    literals: list[str] = []
    for path in sorted(step_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name != "execute_steps":
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    literals.append(arg.value)
    return literals


def split_gherkin_block(block: str) -> list[str]:
    """Split an `execute_steps` block into individual step texts.

    The leading keyword is dropped because behave matches on the text
    after it, and a data-table row (`| a | b |`) belongs to the step above
    rather than being a step itself.
    """
    steps: list[str] = []
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith(("|", "#", '"""')):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] in {
            "Given",
            "When",
            "Then",
            "And",
            "But",
            "*",
        }:
            steps.append(parts[1].strip())
    return steps


_SCRIPT = '''
import ast, json, importlib, pkgutil, sys
from pathlib import Path

step_dir = sys.argv[1]
feature_dir = sys.argv[2]
# Step texts invoked via context.execute_steps(), extracted by the
# caller from the step modules' AST.
composite_steps = json.loads(sys.argv[3]) if len(sys.argv) > 3 else []

sys.path.insert(0, ".")
from behave.step_registry import registry
from behave.parser import parse_file
from behave.model_core import Status

errors = []

package = str(Path(step_dir)).replace("/", ".").replace("\\\\", ".")
for _finder, name, _ispkg in pkgutil.iter_modules([step_dir]):
    try:
        importlib.import_module(f"{package}.{name}")
    except Exception as exc:
        errors.append(f"{name}: {type(exc).__name__}: {exc}")

# registry.steps maps keyword -> [Matcher]; a definition registered under
# several keywords is one definition, keyed by its function location.
definitions = {}
for keyword, matchers in registry.steps.items():
    for matcher in matchers:
        func = matcher.func
        key = (func.__code__.co_filename, func.__code__.co_firstlineno)
        entry = definitions.setdefault(key, {
            "pattern": matcher.pattern,
            "function": func.__name__,
            "file": func.__code__.co_filename,
            "line": func.__code__.co_firstlineno,
            "keywords": [],
            "used": False,
            "used_by_feature": False,
        })
        if keyword not in entry["keywords"]:
            entry["keywords"].append(keyword)

by_location = {}
for (filename, lineno), entry in definitions.items():
    by_location[(filename, lineno)] = entry


def mark(step_text, keyword, from_feature):
    """Ask behave's registry which definition would run this step."""
    class _S:
        pass
    s = _S()
    s.name = step_text
    s.step_type = keyword
    s.keyword = keyword
    try:
        match = registry.find_match(s)
    except Exception:
        match = None
    if match is None:
        return False
    func = match.func
    key = (func.__code__.co_filename, func.__code__.co_firstlineno)
    entry = by_location.get(key)
    if entry is not None:
        entry["used"] = True
        if from_feature:
            entry["used_by_feature"] = True
    return True


feature_step_count = 0
feature_file_count = 0
for feature_path in sorted(Path(feature_dir).rglob("*.feature")):
    feature_file_count += 1
    try:
        feature = parse_file(str(feature_path))
    except Exception as exc:
        errors.append(f"{feature_path}: {exc}")
        continue
    if feature is None:
        continue
    for scenario in feature.walk_scenarios():
        for step in scenario.all_steps:
            feature_step_count += 1
            mark(step.name, step.step_type, True)

# A step reached only from another step is still used; Cucumber's own
# unused report is documented as producing false positives here.
for text in composite_steps:
    mark(text, "given", False)

print(json.dumps({
    "definitions": list(definitions.values()),
    "feature_step_count": feature_step_count,
    "feature_file_count": feature_file_count,
    "errors": errors,
}))
'''


def analyze(
    *,
    cwd: Path,
    poetry_cmd: list[str],
    step_dir: str,
    feature_dir: str,
    execute_steps_texts: list[str],
) -> UnusedStepsResult:
    """Run the analysis inside the target project's interpreter."""
    script_args = [step_dir, feature_dir, json.dumps(execute_steps_texts)]
    proc = subprocess.run(
        [*poetry_cmd, "run", "python3", "-c", _SCRIPT, *script_args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )

    result = UnusedStepsResult()
    payload = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
    if payload is None:
        result.errors.append(
            "step analysis produced no result: "
            + (proc.stderr.strip()[-400:] or "no output")
        )
        return result

    definitions = payload["definitions"]
    result.total_registered = len(definitions)
    result.total_feature_steps = payload["feature_step_count"]
    result.feature_files_scanned = payload.get("feature_file_count", 0)
    result.errors.extend(payload.get("errors", []))

    for entry in definitions:
        if entry["used_by_feature"]:
            continue
        record = {
            "pattern": entry["pattern"],
            "function": entry["function"],
            "file": entry["file"],
            "line": entry["line"],
            "keywords": entry["keywords"],
        }
        if entry["used"]:
            result.used_only_by_other_steps.append(record)
        else:
            result.unused.append(record)

    result.add_confidence_warnings()
    return result
