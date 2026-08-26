"""`aitlc propose-fix` — Idea 3 groundwork, propose-only, never auto-commit.

SRS §2.2 is explicit: the actual auto-fixing agent is out of scope for this
tool — building it means an LLM making the "is this fix correct" judgment
call, which isn't deterministic tool code. What IS real groundwork: taking
a diff someone (human or agent) already wrote and packaging it with the
context a reviewer needs — classification of the original failure, and a
mechanical blast-radius check — into one artifact. This command never
writes to git and never applies anything; `--diff` is read-only input.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from aitlc.config import AitlcConfig
from aitlc.core import behave_runner
from aitlc.core.blast_radius import check as blast_radius_check
from aitlc.core.patterns import PatternLibrary

app = typer.Typer(help="Package a proposed fix (diff) with evidence for human review.")


@app.callback(invoke_without_command=True)
def propose_fix(
    test_id: str = typer.Argument(...),
    diff_file: Path = typer.Option(
        ..., "--diff", exists=True, help="A unified diff (e.g. `git diff` output)."
    ),
    prior_report: Path | None = typer.Option(
        None,
        "--report",
        help="An `aitlc run --json` output — classifies the failure this diff addresses.",
    ),
    out: Path = typer.Option(..., "--out", help="Where to write the proposal.md."),
) -> None:
    """Assemble the evidence needed to propose a fix."""
    config = AitlcConfig.find_and_load()
    diff_text = diff_file.read_text()

    radius = blast_radius_check(
        diff_text, step_dir=config.step_dir, locators_dir=config.locators_dir
    )

    classification_lines = ["*No `--report` given — original failure not classified.*"]
    if prior_report is not None:
        patterns_path = config.root_dir / "patterns.yaml"
        if patterns_path.exists():
            payload = json.loads(prior_report.read_text())
            if isinstance(payload, list):
                # Same raw report.json shape classify-failure accepts.
                result = behave_runner.parse_report(prior_report)
                failures = [{"step": f.step, "error": f.error} for f in result.failures]
            else:
                failures = payload.get("failures", [])
            library = PatternLibrary.load(patterns_path)
            classification_lines = []
            for failure in failures:
                match = library.classify(
                    failure.get("step", ""), failure.get("error", "")
                )
                if match:
                    classification_lines.append(
                        f"- **{failure['step']}** → known: `{match.pattern.id}` "
                        f"({match.pattern.suggested_action})"
                    )
                else:
                    classification_lines.append(
                        f"- **{failure['step']}** → no known pattern (genuinely novel)"
                    )
        else:
            classification_lines = ["*No patterns.yaml found — cannot classify.*"]

    escalate_warning = ""
    if radius.touches_shared_dirs:
        escalate_warning = (
            "\n> ⚠️ **This diff touches shared code** "
            f"({', '.join(radius.touches_shared_dirs)}). The rule: "
            '"the fix touches shared code used outside the one failing test" '
            "is an explicit escalation trigger — confirm via search that this "
            "change is scoped to what you intend before applying it.\n"
        )

    proposal_md = f"""# Proposed fix — {test_id}

**This is a proposal, not an applied change.** Nothing has been committed.
Review against the debugging playbook's minimal-change-first rule
(`aitlc start` embeds the full playbook) before applying.

## Failure classification

{chr(10).join(classification_lines)}

## Blast radius

Changed files: {', '.join(radius.changed_files) or '(none detected)'}
{escalate_warning}
## Diff

```diff
{diff_text}
```
"""
    out.write_text(proposal_md)
    typer.echo(
        json.dumps(
            {
                "proposal": str(out),
                "changed_files": radius.changed_files,
                "touches_shared_dirs": radius.touches_shared_dirs,
                "scoped_narrowly": radius.is_scoped_narrowly,
            }
        )
    )


# Mounted by commands/_registry.py. Plain command, preserving `aitlc propose-fix`.
COMMAND = {"name": "propose-fix", "attr": "propose_fix", "order": 80}