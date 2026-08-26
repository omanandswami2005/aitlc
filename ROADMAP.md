# Roadmap — `aitlc`

What is built, what is still open, and what is deliberately not being built.
For how to use any of it, see [`USER-GUIDE.md`](USER-GUIDE.md).

**Tracker: 64 of 67 gaps closed.** Every row came from a real debugging
session that the tool made harder than it needed to be; none of them are
speculative features. See each version section below for what's in it.

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

## v0.7.x — gate-unification hardening

| # | Item | Shipped |
|---|---|---|
| G49 | `run --debug` crashed with `JSONDecodeError` when the runner halted on failure (no report file to parse) | `behave_runner.parse_report` catches the missing/empty report and returns an empty `RunResult`; `run --debug` distinguishes a genuine pause from a real crash via `crash_traceback`. |
| G50 | `cdp launch` defaulted to a 375×812 mobile viewport for every scenario | Desktop (1920×1080) by default; `--mobile` opts a suite into the phone size. |
| G51 | `steps run --range` never fired `before_feature`/`before_scenario`, so any hook-set `context` attribute (e.g. `context.screen_width`) was simply absent — an immediate `AttributeError` with no connection to the real cause | `apply_scenario_setup` (`core/step_console.py`) now defaults `screen_width`/`screen_height` unconditionally, before any early return, and reports `context_defaults_applied` so the default is visible rather than silent. |
| G52 | `run` reported an empty, indistinguishable-from-nothing result (`exit_code:1, steps_by_status:{}`) when a hooks/steps module failed to import — no forensic trail anywhere | `behave_runner.run()` always captures stderr; when a run never wrote a report, the tail (gated on a real `Traceback` marker, not just "stderr is non-empty" — the first version of this false-flagged a genuine `--debug` pause as a crash) is surfaced as `crash_traceback` plus an explicit "don't just re-run" hint. |
| G53 | A `run --debug` gate-on-failure park reported **where** (index/current step) but never **why** — no assertion/exception text anywhere in the `paused_on_failure` reply or `debug status`, even though the failed step's `error_message` was sitting right there and unused | `_gate_on_failure_run_hook` captures `step.error_message` into `self._aitlc_park_error`; `_status()` includes it as `"error"`; both `run --debug`'s `paused_on_failure` payload and `debug status`'s reply now forward it. |
| G55 | A cleanly-**passing** `run --debug` scenario (0 failures, finished in under two minutes) was reported as `{"error": "did not finish or fail within 1800.0s"}` a full 30 minutes later — and held that test's run-lock for the entire false-timeout window, blocking any later `run`/`debug` on the same test id | `await_park_or_exit`'s liveness check used `os.kill(pid, 0)`, which still succeeds against an exited-but-unreaped zombie (the process that spawned the child never called `.wait()`/`.poll()` on it). Fixed by threading the actual `Popen` object through and polling it with `.poll()` — a real non-blocking `waitpid` that both reaps the child and returns instantly once it has actually exited. Regression-tested against a real subprocess (`tests/test_gate_launch.py`), not a mock of the OS boundary the bug lived in. |
| G58 | `debug next`/`retry` crashed the entire gated behave process outright on the currently-shipped behave version — `AttributeError: module 'behave.matchers' has no attribute 'step_matcher'` inside `_reload_steps`, on every single call, since `_reload_steps` always reloads step modules before running the requested step | Newer behave split `use_step_matcher`/`step_matcher` out of `behave.matchers` into `behave.api.step_matchers`, and replaced the module-level `current_matcher` getter/setter with a factory (`use_current_step_matcher_as_default()`/`use_default_step_matcher()`), matching behave's own current `load_step_modules`. `_reload_steps` now probes for the new module first and only falls back to the pre-split `behave.matchers` attributes for an older behave — same "ask the tool, don't assume its version" principle as the rest of this project. This was the most impactful of the seven: every `next`/`retry` call was silently broken until this fixed it, on this environment's behave version. |
| G54 | `debug retry`/`next`'s reload only re-execs files under `step_dir`, mirroring behave's own step-loading. A fix inside a page-object/helper module a step imports normally (a real Python `import`, not behave's exec-file loading) stayed cached in the live process's `sys.modules` — `retry`/`next` silently kept running the OLD code, with no warning the edit was skipped | `_reload_stale_project_modules` finds every project module (outside `step_dir`, under the project root) whose file changed since the session started and calls `importlib.reload()` on it — BEFORE `_reload_steps` re-execs the step files, so a step's own `from pages.search.search_page import SearchPage` resolves against the freshly-reloaded attribute, not a stale one. Works cleanly for this kind of codebase's common page-object shape (a class of `@staticmethod`s, no persistent module state) because `importlib.reload()` mutates the SAME module object in place. A module that genuinely can't reload (its own top-level code raises) reports its own error in a `warning`/`stale_modules` field instead of silently running stale code OR crashing the gate — verified both ways end-to-end: a real edit picked up and asserted on inside the re-run step, and a real reload failure surfaced cleanly. `run-text` (see below) gained the same reload-before-run contract `retry`/`next` already had. |
| G59 | `debug stop` called `chrome_cdp.stop_all(root_dir)` — killing EVERY CDP Chrome tracked for the project, not just the one this session's own `debug start` launched. Found via live end-to-end testing, not the unit suite: a manually-launched, unrelated persistent Chrome in active use for a separate investigation was silently killed as collateral damage the moment an unrelated `debug stop` ran | `chrome_cdp.stop(root_dir, port=session.port)` — the already-existing, PID-verified scoped-stop function — replaces `stop_all`; `debug stop` now only ever touches the port its OWN `debug start` recorded. `stop_all` is unchanged for its one real caller, `aitlc cdp stop --all`, explicitly project-wide by design. |
| G60 | `doctor` reported aitlc's own behave/playwright versions, not the target project's — measured a full major-version gap live | Probes the target's own env via `poetry run python -c ...`, falls back to aitlc's own only if that fails. |
| G61 | Every `journal.record` call site left `duration_s` at 0.0 — always, everywhere | `run` sums real `ScenarioResult.duration_seconds`; the 3 `s3` commands wall-clock themselves. Verified live. |
| G62 | `aitlc call` crashed every invocation — `NameError: time` never imported in `step_console.py` | Added the missing import. |
| G63 | `aitlc call` never loaded `.env`, unlike every other command — silently broke on any project needing `ENVIRONMENT_URL`/secrets | `call_cmd.py` now loads it, matching `run`/`debug`/`doctor`. |
| G64 | A failed scenario-setup made `call` report a useless "the console produced no result" — the real reason was sitting in an unrecognized `"done"` event | Result parser now surfaces the setup failure's own detail. |
| G65 | `classify-failure` crashed (`AttributeError`) on a genuine "raw report.json" input — its own documented second accepted shape is a top-level list, not the dict it assumed | Reuses `behave_runner.parse_report` to handle the list shape too. |
| G66 | `propose-fix --report` crashed the same way on the same raw report.json shape | Same fix as G65, applied there too. |

Not yet closed:

| # | Item | Status |
|---|---|---|
| G67 | `steps run`'s `unhandled_events` reported `feature_status: "failed"` next to 3 correctly-passed steps in `results` — the real, relied-on output was right; this diagnostic field was not | Not root-caused. A bare `parse_file()` on the same feature gives `Status.untested`/`.passed` depending on file — the actual code path reads `feature.status` AFTER real `before_scenario` hooks ran against it, and something in that sequence leaves it at `.failed`. Does not affect `results`' correctness. |
| G56 | A live `debug next`/`retry` session reported `status=undefined` for a step whose exact text matched a registered step definition, after ~15+ `next`/`retry` calls and several live edits to the feature file (steps added earlier in the file, shifting every later cursor). | Not root-caused; may be related to G58 (a matcher-API crash can leave the registry/cursor in a state that looks like desync rather than a clean crash) — worth deliberately reproducing post-G58 before assuming it's still open as originally diagnosed. |
| G57 | `run --debug` consistently failed to collect ANY steps for one particular feature/scenario that ran completely fine under plain `run` — parked immediately at `{"parked_at": 0, "current_step": null}` with `total: 0`. | Not root-caused; same caveat as G56 — re-verify against this specific feature/scenario shape post-G58 before continuing to chase it as a separate bug. |

### New capabilities added alongside the above

Not gap fixes — closing a real, previously-unreachable feature gap and a
requested UX consistency pass, while the gate engine was already open for
this round of work:

- **`aitlc debug eval "<js-expr>"`** — evaluates a JS expression against the
  live paused page via Playwright's `page.evaluate()`. The breakpoint()-
  equivalent for the browser side of a paused session (read DOM state,
  count/inspect elements, pull text) without editing a page object to add
  one, and without needing a second CDP connection (G54/G55 in the older
  numbering, before the gate-unification rewrite, both moot now — this
  reads off the SAME live `context` the gate already holds).
- **`aitlc debug run-text "<gherkin-step>"`** — exposes `core/step_console.py`'s
  `run_text` gate command, which existed on the socket protocol already but
  had no CLI command sending it. Runs any registered step against the live
  paused context without advancing the debug cursor; gained the same
  reload-before-run contract as `retry`/`next` (see G54 above) so it also
  exercises your latest edit, not a stale one.
- **Consistent step-count progress everywhere.** `aitlc run`'s live status
  file (`live_status.py`, read via `--no-status`'s opt-out or polled
  externally) now reports `step_index`/`step_total` for the current
  scenario — the same `index`/`total` shape `debug status` already
  reported, so "where am I" means the same thing regardless of which
  command is running. `aitlc parallel run` gained a live "`[N/M] feature:
  outcome`" line per completed feature (via `as_completed` instead of a
  silently-blocking `pool.map`) plus a `.parallel/progress.json` file —
  previously a parallel sweep gave zero visible progress until every
  feature had finished, however long that took.

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

Three gaps from v0.7.x dogfooding aren't root-caused yet (G56, G57, G67 —
see that section above), plus verification that one older, already-shipped
gap fully delivers:

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
