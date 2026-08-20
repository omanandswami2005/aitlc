# Roadmap — `aitlc`

What is built, what is still open, and what is deliberately not being built.
For how to use any of it, see [`USER-GUIDE.md`](USER-GUIDE.md).

**Tracker: 48 of 48 gaps closed.** v0.5.0 closed the four gaps (G45–G48) raised
against 0.4.0. v0.6.0 is an architecture release, below. Every row came from a
real debugging session that the tool made harder than it needed to be; none of
them are speculative features.

## v0.6.0 — the fast loop is real behave (architecture)

The debug fast-loop no longer reconstructs behave's run loop; it drives a real,
paused behave process and single-steps it. The gaps that were divergences of a
reimplementation (G34 Examples binding, G37 run-scoped data, G46 tables /
docstrings, G18 hooks / collectors, G39 provenance) cannot recur, because there
is nothing left to reconstruct: behave binds the example row, mints run-scoped
data once, parses the tables, and fires the project's own hooks.

- **Gated runner.** `aitlc.runtime.runner:AitlcRunner` gained a gate mode:
  behave runs `before_all`/`before_scenario` and the setup steps, then parks at
  the target step holding the live Context and browser, and advances / re-runs
  REAL behave `Step` objects over a control socket. Attached through behave's
  own `--runner` option (version-probed, with the sitecustomize fallback).
- **`debug` is gate-only.** `start`/`next`/`retry`/`status`/`stop` drive the
  paused process (`core/gate_client.py`). One engine, one path.
- **Deleted, not kept as a fallback.** The step-console reconstruction that
  `debug` used (`_slice_file`, `_run_steps`, `_launch_console`, the `console`
  command) and the superseded parsing/arithmetic in `core/debug_session.py`
  (`feature_steps`, Examples binding, slice/resync/attempt bookkeeping) are
  gone. `core/step_console.py` remains only as the backend for `steps run` and
  `call`, which are separate features, not a debug fallback.
- **Verified against real behave**, not mocks: `test_gate_runner.py` drives a
  genuine behave process (park/next/retry/failed/stop) and proves pause-on-
  failure halts before teardown; `test_debug_gate.py` drives the same through
  the `aitlc debug` commands end to end. This differential-style check is the
  one test that mechanically catches the whole class of divergence.
- **Suite guidance for the target suite:** aitlc supplies pause-on-failure via
  its runner (`AITLC_PAUSE_ON_FAILURE`), so a project needs no `after.py` edit
  to keep the browser on a failed step.

## v0.5.0 — delivered

Four gaps found while running the shipped 0.4.0, all in the `debug` session
path — the workflow 0.3.0/0.4.0 built out, now exercised hard enough to show
where it still diverged from a real behave run or hid what it was doing. Theme
of the release: **debug-session fidelity and progress visibility.** Sourced from
`aitlc-gaps.md` (the entries tagged `aitlc 0.4.0`). All four are **done** — see
Closed below.

| # | Item | Shipped |
|---|---|---|
| G46 | data table lost in a debug slice; a failed setup step did not stop/flag the batch | `feature_steps` keeps each step's table/docstring; slice + console reattach it (verified against real behave); `debug start` flags `setup_failed`, `debug status` surfaces it. |
| G45 | `run --debug` forced a mobile viewport with no opt-out | `run --window-size` (desktop default, phone with `--mobile`), threaded to `chrome_cdp.launch`. |
| G47 | `debug start` had no `--failures-only` / `--summary` | Both added as filters over the already-computed setup output. |
| G48 | `debug start` blocked with zero progress visibility | Per-step progress file; `debug status` reads it mid-flight; `--background` returns a poll handle immediately. |

Every one landed with a test that exercises the real boundary — the data table
actually crossing into the slice via real `parse_steps`/`parse_file` (G46), the
real `chrome_cdp.launch` window size (G45) — not a fake asserting itself.
G33/G44 are why.

## Open

Every gap raised from a real session is closed. What remains is verification
that one of them fully delivers:

| # | Item | Status |
|---|---|---|
| G42 | checkpoint restore | Cookie-level restore verified against a real logged-in session: 49 cookies captured and all 49 restored byte-identically, with the check proven falsifiable by clearing them first. **Whether the application accepts the restored session end to end is unverified** — one attempt landed on a sign-in page, a second timed out on a slow network, and neither ruled the other out. Treat restore as "session material replayed", not yet as "you are signed in". |

## Closed

| # | Gap | Resolution |
|---|---|---|
| G45 | `run --debug` forced a mobile viewport with no opt-out | `run --window-size`; the debug launch defaults to desktop, or a phone size under `--mobile`. |
| G47 | `debug start` had no `--failures-only` / `--summary` | Both are filters over the already-computed setup output; full output stays the default. |
| G48 | `debug start` blocked with no progress visibility | Per-step progress file written by the step console; `debug status` reads it while a start is still running; `--background` returns a poll handle immediately. |
| G46 | a step's data table was lost in a debug slice, and a failed setup step did not stop/flag the batch | `feature_steps` keeps each step's table/docstring as a multi-line block; the console (`parse_steps`) and one-shot slice (`parse_file`) reattach it, verified against real behave. `debug start` records `setup_failures` and flags `setup_failed` + a warning; `debug status` surfaces them. |
| G1 | `steps run` swallowed the child's stderr | `stderr_tail` is carried on every console result. |
| G2 | `init` could not detect three needed settings | `init` probes for browser factory / actions / scenario setup. |
| G4 | no output until `steps run` finishes | `live_status` writes progress continuously. |
| G5 | `cdp inspect --a11y-query` ignored on the fallback path |  |
| G7 | failure JSON did not point at the evidence just written | Trace and screenshot paths ride along with the failure. |
| G8 | `--retry-only-if-known-flake` could not help |  |
| G11 | outside the project root, commands answered "none" | Now a `ConfigError` instead of a confident wrong answer. |
| G12 | the `error` field could report a urllib3 warning | `extract_error` strips every captured stream first. |
| G13 | could not tell a concurrency artifact from a real failure | Browser pool keys on in-use, not task index. |
| G14 | `--range` could not start on an `And`/`But` | `promote_leading_continuation`. |
| G16 | `run` and `parallel run` wrote status under different names |  |
| G17 | no local mobile switch | `--mobile` takes a device key. |
| G18 | no way to run part of a scenario with the project's hooks | `scenario_setup`. |
| G19 | `update-gherkin` did not normalize (DATA LOSS) | Normalizes and refuses header lines. |
| G20 | phantom diff for tab-indented Examples rows |  |
| G21 | no path from "a CI report failed" to a failure table | `s3 triage-run`. |
| G22 | nothing fetched was kept, so every question cost a re-run | `journal` + artifact cache. |
| G23 | the debugging cycle was documented nowhere | `debug start/retry/next/certify/stop`. |
| G24 | `doctor` did not report versions or the live code path |  |
| G25 | no locator lint | `locators lint`. |
| G26 | nothing distinguished a dirty debug browser from a clean one | CDP provenance is recorded. |
| G28 | expired credentials surfaced as a Rich traceback | Handled as a clean error. |
| G29 | `find-step-usage` wrote progress to stdout, corrupting its JSON | Progress goes to stderr. |
| G30 | `--at` filtered *after* truncating to `--limit` | Filter first, truncate second. Cost three guesses at `--limit` before a real run appeared. |
| G32 | nothing warned that a local run cannot pass its setup |  |
| G33 | `debug start` shipped broken — crashed on every invocation | Tuple unpack. Now covered by a real-launch test, not a fake. |
| G34 | Scenario Outline placeholders ran literally | Examples binding plus `--example`; unbound placeholders are refused. |
| G35 | `debug retry` reported "failed" when the step never ran | `retry`/`next` load env; a step that did not run reports `not_run`. |
| G41 | no per-test history across runs | `s3 history` — matrix, signatures, verdict, break date, persisted to one file. |
| G43 | no way to ask "did test X pass in the latest run" | `s3 find-test` / `s3 verify-test`. |
| G44 | tests mocked the boundary the bugs lived on | `test_fake_fidelity.py`: real-launch integration, contract checks, property guards. |
| G3 | `init` has no merge mode | `--merge` adds only newly detected keys, keeping every value already set. |
| G6 | no `--version` | Read from package metadata, not a constant. |
| G27 | `triage-run` truncates the call log | The resolved element, the intercepting overlay and the retry count now reach the table. |
| G31 | no way to use an AWS SSO profile | `[s3].profile` / `AWS_PROFILE`, resolved fresh on every call. |
| G36 | every `next`/`retry` pays full process startup | Persistent step console, with a fallback to a process. |
| G37 | per-step processes regenerate run-scoped data | Closed with G36: one process, one set of that data. |
| G38 | no wall-clock stamps or condition timer | `started_at`/`ended_at` per step, plus `cdp time-until`. |
| — | artifacts scattered across `reports/` | `--workspace` puts one investigation in one directory, applied at core level. |
| G9 | no way to read page state or call a project function | `cdp inspect --storage` (values fingerprinted) and `aitlc call 'module:attr'`. |
| G10 | `parallel run` streams every child's transcript | Each run writes `<workspace>/.parallel/<stem>.log`. |
| G15 | the skip pre-filter was narrower than the suite's own rule | Matches the tag at any placement, plus `_prod`/`_stage`/`_dev`. |
| G39 | a local run silently differs from the CI one | `aitlc preflight` reports missing session, misplaced hook tags, order dependence. |
| G40 | parallel failures needed a re-run to attribute | Overlap and shared-account evidence, free; `--verify-failures` stays opt-in. |
| G42 | the debug cycle could not survive an expensive scenario | `debug checkpoint` / `restore` / `checkpoints`, with a TTL. |

## Not planned

- **An MCP server.** A CLI is cheaper in tokens and works in more contexts.
- **Docker distribution.** An installed mode and a zero-install mode already
  cover the ground.
- **Anything that commits on your behalf.** `propose-fix` proposes.
- **Infrastructure orchestration.** Provisioning and grid management belong to
  the platform, not to a debugging CLI.

## Working rules

Two rules earned the hard way, and both are enforced by the suite:

- **If a test replaces an aitlc function, there must also be a test that does
  not.** Stub the OS, the network, the clock — never the thing being verified.
  A fake written to match the call site rather than the real function let a
  command ship crashing on every invocation with twelve tests green (G33, G44).
- **Validate against real data once before calling anything done.** Fixtures
  agree with whoever wrote them. Real data does not, and it has caught a
  formatting defect and a day-vs-run scoping error that no fixture would have.
