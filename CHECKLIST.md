# aitlc — dev checklist

Single source of truth for what is being built now and what is queued. Grouped
by workstream. Check items off as they land with a test. Detailed rationale for
the v0.5.0 gaps lives in ROADMAP.md and (source) `aitlc-gaps.md`.

Legend: [ ] todo · [~] in progress · [x] done

---

## Workstream 1 — pluggable CLI (architecture)

Discovery-based command registry so adding a command is dropping one file.

- [x] `commands/_registry.py`: `CommandSpec` + `discover()` + `register_all(app)`
- [x] Every command module declares a module-scope `COMMAND` dict
- [x] `cli.py` reduced to a thin composition root (`_registry.register_all(app)`)
- [x] Regression test: mounted command set + order matches the previous wiring
- [x] Full pytest suite green (519 passed)

## Workstream 2 — aitlc as a wrapper over existing tooling

aitlc should hand off to what already exists, not reimplement it. Today it
wraps `behave` and `playwright` (passthrough_cmd). Extend that.

- [x] `aitlc paver <args>` — pass through to the project's `paver` (e.g.
      `aitlc paver run parallel --local`), with the project env + interpreter
      already resolved, same as `behave`/`pw`
- [x] Generic passthrough pattern: a new tool is one command added to
      passthrough_cmd.py (root escape-hatch group), mounted automatically
- [x] `--print-command` support (mirrors existing passthroughs)

## Workstream 3 — tie aitlc to the codebase's Playwright CDP URL (top pain point)

Problem: the fast loop keeps degrading to a full `aitlc run` instead of reusing
the live CDP browser. Make CDP the default path when a CDP endpoint is known.

- [x] Identified the suite's attach var: `PLAYWRIGHT_CDP_URL`
      (features/behave_env/platforms/local.py) — set it and the suite attaches
- [x] Configurable via `[project].playwright_cdp_env` (default
      PLAYWRIGHT_CDP_URL); documented in aitlc.toml.example
- [x] `chrome_cdp.resolve_live_cdp_url()` — reuse a running tracked instance
- [x] `aitlc run` reuses a live CDP browser by default (`--cdp/--no-cdp`,
      `--cdp-url`); `paver` and `behave` passthroughs do too (`--aitlc-cdp` /
      `--aitlc-no-cdp` / `--aitlc-cdp-url`)
- [x] Guardrail: never override a CDP var already in the environment; only
      attach to instances that answer a live probe (no stale-state ECONNREFUSED)
- [ ] `doctor` reports the resolved CDP URL and whether a browser is live there
      (nice-to-have, not yet done)

## Workstream 4 — v0.5.0 gaps (debug-session fidelity + visibility)

From `aitlc-gaps.md`, entries tagged aitlc 0.4.0. Detail in ROADMAP.md.

- [x] G46 (correctness-first): data table lost in a debug slice; a failed setup
      step must stop/flag the batch; `debug status` surfaces failed setup steps
      — DONE: feature_steps keeps each step's table/docstring; slice + console
      paths reattach it (verified against real behave parse_steps/parse_file);
      start() flags setup_failed + warning, status() surfaces setup_failures
- [x] G45: `aitlc run --window-size` (mirror `debug start`); desktop by default,
      phone under `--mobile`, explicit value wins — threaded to chrome_cdp.launch
- [x] G47: `debug start --failures-only` / `--summary` output modes
- [x] G48: `debug start` writes a per-step progress file; `debug status` reads it
      mid-flight; `--background` returns immediately with a poll handle

---

---

## Workstream 5 — single-stepping real behave (v0.6.0, done)

Stop reconstructing behave's run loop; pause the real one. Closes the whole
class of fidelity gaps by construction, and collapses debug to one path.

- [x] Gated `AitlcRunner`: park at a step, advance/re-run real behave Step
      objects over a control socket (`runtime/runner.py`, `core/gate_client.py`)
- [x] `debug start/next/retry/status/stop` are gate-only; console fallback,
      `_slice_file`/`_run_steps`/`_launch_console`/`console` deleted
- [x] `core/debug_session.py` pruned to the gate model (dead parsing/arithmetic
      removed); `step_console.py` kept only for `steps run` / `call`
- [x] Obsolete tests removed; verified against REAL behave
      (`test_gate_runner.py`, `test_debug_gate.py`) — 489 passed
- [x] Suite's `after.py` DEBUG_PAUSE_ON_FAILURE edit removed; aitlc's runner
      pause (`AITLC_PAUSE_ON_FAILURE`) verified to halt before teardown

## Working rules (enforced)

- If a test replaces an aitlc function, there must also be a test that does not.
  Stub the OS / network / clock — never the thing being verified. (G33/G44)
- Validate against real data once before calling anything done.
- Never report success for something that did not happen — a missing
  precondition or an unreachable CDP URL is reported explicitly, not rendered
  as an innocent empty/normal result.
