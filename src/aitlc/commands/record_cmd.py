"""`aitlc record <url> --suggest-steps` — codegen plus locator diffing."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import typer
from aitlc.config import AitlcConfig
from aitlc.core.codegen import CodegenError, extract_string_literals, run_codegen
from aitlc.core.locator_scan import scan_directory


def record(
    url: str = typer.Argument(..., help="URL to open in the Playwright recorder."),
    suggest_steps: bool = typer.Option(
        False,
        "--suggest-steps",
        help="Diff recorded selectors against the project's existing locators.",
    ),
    save_code: Path = typer.Option(
        None, "--save-code", help="Also save the raw generated Playwright code here."
    ),
) -> None:
    """Record a session with Playwright codegen and map its selectors.

    Records a real interactive session via Playwright's own codegen —
    the browser window and terminal instructions are Playwright's, not
    aitlc's. Once you close the recorder, aitlc reports which selectors
    already match something in your locators dir vs. which are new
    (Idea 3's "max existing step reuse" rule: map onto what exists before
    inventing new code).
    """
    if save_code:
        output_path = save_code
    else:
        # mkstemp, not mktemp: mktemp only *returns a name*, leaving a window
        # in which another process can create that path first (TOCTOU). The
        # recorded file can contain selectors and typed values from a real
        # session, so it should land in a file this process exclusively
        # created. The descriptor is closed immediately — codegen writes the
        # path itself — but the file now exists with 0600 from the start.
        handle, temp_name = tempfile.mkstemp(suffix=".py", prefix="aitlc_codegen_")
        os.close(handle)
        output_path = Path(temp_name)

    typer.echo(
        f"Opening Playwright recorder at {url} — close the browser window when done.",
        err=True,
    )
    try:
        run_codegen(url, output_path)
    except CodegenError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=1) from exc

    generated_code = output_path.read_text()

    if not suggest_steps:
        typer.echo(json.dumps({"generated_code_path": str(output_path)}))
        return

    config = AitlcConfig.find_and_load()
    known_locators = scan_directory(config.root_dir / config.locators_dir)

    literals = extract_string_literals(generated_code)
    seen: set[str] = set()
    matches = []
    new_candidates = []
    for value, line in literals:
        if value in seen:
            continue
        seen.add(value)
        ref = known_locators.get(value)
        if ref:
            matches.append(
                {
                    "value": value,
                    "matches_key": ref.key,
                    "in_file": str(ref.file),
                }
            )
        elif len(value) > 2 and any(c in value for c in "#./[]="):
            # Heuristic filter, not a hard rule: only flag things that look
            # selector-ish (has a CSS/xpath-ish character) as "new" — plain
            # typed-in field values (names, emails, etc.) also show up as
            # string literals in codegen output and aren't useful noise here.
            new_candidates.append({"value": value, "generated_line": line})

    typer.echo(
        json.dumps(
            {
                "generated_code_path": str(output_path),
                "matched_existing_locators": matches,
                "new_locator_candidates": new_candidates,
            },
            indent=2,
        )
    )
