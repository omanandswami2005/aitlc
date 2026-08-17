# Roadmap — `aitlc`

What is built, what is deliberately not being built, and what is still open.
For how to use any of it, see [`USER-GUIDE.md`](USER-GUIDE.md).

Status: the command surface below is complete and covered by the test suite.
Version `0.1.0` — the API and the `aitlc.toml` schema may still change.

## Built

**Running tests.** `run` with structured JSON or compact TOON output, retry,
retry-only-when-the-failure-matches-a-known-flake, a local lockfile that makes
launching the same test twice at once structurally impossible, and a
concurrency-aware queue for remote grids. `parallel run` and `parallel focus`
run and pin a selection without editing tags in the suite. Bare test IDs
resolve recursively; a `FILE:LINE` selects a single Examples row.

**The debugging cycle (0.2).** `debug start / retry / next / certify / stop`
holds one isolated browser and a step index, so fixing a step costs a re-run of
*that step* rather than the scenario, and a scenario with four defects costs one
setup instead of four. `certify` is a separate verb on a fresh instance,
defaulting to two consecutive passes, because a CDP-attached browser is never
proof and one pass does not disprove a race. `s3 triage-run` turns a CI run's
per-execution Behave JSON into totals plus one row per failure, replacing a
key listing, a bulk download and a hand-written parser.

**Keeping what happened (0.2).** `journal list/show/diff/cache` records each
invocation and caches fetched artifacts, so a follow-up question is a file read
rather than another run — and `diff` answers "did my fix work, or was that
luck". Everything is redacted before it touches disk, size-capped and pruned.

**Locator hygiene (0.2).** `locators lint` flags positional selectors, grid
cells with no `role='cell'` guard, and unanchored xpaths, each with the rewrite
attached rather than only the diagnosis.

**Debugging live.** `cdp launch` keeps a detached browser alive across many
iterations, so setup and login are paid once rather than every cycle.
`steps run --range` executes a slice of a scenario in that browser, replacing
the habit of commenting out the steps that already passed. `cdp inspect
--a11y` reads a page as an accessibility tree — assertable text carrying
nesting, control state and field values that a screenshot cannot express, at a
fraction of the tokens.

**Suite health.** `steps unused` finds dead step definitions through behave's
own registry, so the answer agrees with what the runner would dispatch, and
counts steps invoked via `context.execute_steps(...)`. `history` records every
run outcome, which is what makes a *new* flake visible rather than only the
ones already catalogued. `doctor` checks the environment before a run rather
than after a confusing failure.

**Evidence.** `report` captures and replays real terminal output via `pyte`.
`trace` extracts frames from a Playwright trace. `s3 report-summary` reduces a
multi-megabyte HTML report to compact JSON, separating summary statistics,
per-feature breakdown and a capped failure list from the embedded screenshots
that drive the size — without ever loading them.

**Integrations.** Xray Gherkin read/update/compare, Test↔Execution↔TestRun
navigation, and a concurrent-paginated step-usage search. Jira task creation.
Tunnel status and restart. Per-run failure and duration summaries to a Teams
webhook.

**Escape hatches.** `aitlc behave` and `aitlc pw` run those tools directly
with the project's `.env` and interpreter already resolved, so adopting aitlc
never means losing a flag it does not wrap. `--print-command` prints the exact
invocation without running it.

**Setup.** `aitlc init` inspects a repo and writes a working `aitlc.toml`,
reporting how each value was found and leaving anything undetected as a
commented placeholder.

## Not planned

- **An MCP server.** A CLI is cheaper in tokens and works in more contexts.
  Structured output covers what an agent actually needs.
- **Docker distribution.** An installed mode and a zero-install mode already
  cover the ground; a container adds packaging surface without closing a gap.
- **Anything that commits on your behalf.** `propose-fix` proposes. That line
  does not move.
- **Infrastructure orchestration.** Provisioning and grid management belong to
  whatever runs the grid, not to a debugging CLI.

## Behaviour changes in 0.2

Both are fixes, but they change what a caller sees, so they are called out
rather than buried:

- **`steps run` exits non-zero when the child ran no steps and failed.** It
  previously printed `{"results": []}` and exited 0, which is indistinguishable
  from a successful empty run. Scripts that keyed on exit code 0 will now see
  the failure they were missing.
- **`update-gherkin` normalizes its input and refuses header lines.** Passing a
  full `.feature` used to write tags and a `Feature:` line into the Test; it now
  writes the step body, as `compare-gherkin` always assumed.

## Known bugs

Found by using the tool for two weeks of real debugging on a large Behave +
Playwright suite. Each was reproduced; the cause is a specific line.

- **`xray update-gherkin` does not normalize its input.** `compare-gherkin`
  reduces a local `.feature` through `normalize_local_feature()` because a
  Test's `gherkin` field stores only the step body. `update-gherkin` sends the
  file verbatim, so the natural command — passing the same file you just
  compared — writes `@tags` and the `Feature:` line into the Test and leaves it
  invalid. The readback error then says the write "did not persist as sent",
  which reads as *nothing happened* when in fact the Test was modified.
  **Data loss; fix first.** Normalize, refuse a payload containing a `Feature:`
  or tag line, and reword the readback failure.

- **`steps run` hides the child process's failures.** `run_console` uses
  `subprocess.run(..., capture_output=True)` and parses stdout only. A fatal
  configuration error (for example `--mobile` with `--cdp-url` and no
  `browser_factory`) is written to stderr and dropped, so the command prints
  `{"loaded_step_modules": N, "results": []}` and exits 0. A non-zero child
  exit must surface, and unrecognised `{"event": ...}` records — the child
  already emits a `parse_error` — must not be silently discarded.

- **`_extract_error_message` does not strip `Captured stderr`.** It truncates
  at `Captured stdout:` and `Captured logging:` and then takes the *last*
  non-blank line, so any run that writes to stderr after the assertion reports
  that instead of the failure. Observed: a step reported `warnings.warn(` while
  the real error was a locator assertion. This string is also what
  `classify-failure` matches on, so no pattern library can work around it.

- **`cdp inspect --a11y-query` is ignored on the CDP fallback path.** Only the
  `aria_snapshot()` branch filters; the `Accessibility.getFullAXTree` fallback
  builds its node list without reading `query`. `--a11y-all` is honoured on the
  fallback but not the snapshot branch, so the two disagree in opposite
  directions. Silently dropping a flag is worse than rejecting it.

- **`steps run --range` cannot start on an `And`/`But` step.** The slice is
  re-parsed standalone, so a leading continuation keyword has no preceding step
  and Behave rejects it. Roughly half the lines in a real feature start with
  `And`, and the failure surfaces as an empty, successful-looking run.

- **`compare-gherkin` reports a phantom diff for tab-indented Examples rows.**
  Normalization strips tabs from the local side only, so a Test whose Examples
  table is tab-indented upstream can never match. Compare on whitespace-
  normalized lines.

- **Commands answer "none" when run outside the project root.** Config is
  searched upward; above the project there is none, the root falls back to the
  cwd, and path-derived answers (tracked browsers, reports, status files) come
  back empty rather than "no config found here".

## Open

- **Windows support** is untested. Nothing is knowingly POSIX-only beyond
  process-group handling in `core/chrome_cdp.py`, but untested is untested.
- **A starter pattern library.** `classify-failure` needs a YAML file that
  every project must currently write from nothing. A documented starter set of
  vendor-agnostic signatures would make the command usable on day one.
- **Dependency weight.** `boto3` and `jira` are required at install time but
  used by two adapters each. Moving them behind optional extras would cut a
  default install substantially.
- **Schema stability.** `aitlc.toml` and the JSON output shape are not yet
  frozen. They should be before 1.0, and changes to either need a deprecation
  path once anything depends on them.

- **A debug session, rather than a set of commands.** The pieces exist
  (`cdp launch`, `steps run --cdp-url`, `cdp inspect`, `run`) but nothing models
  the loop they serve: take a CI failure, drive one kept browser to that point,
  iterate on the broken step, move forward, then certify in a fresh instance.
  Left to prose it degrades into re-running whole scenarios — which is slow and,
  in suites that mutate data, destructive. A `debug start / retry / next /
  inspect / certify` state machine holding the port and step index in a session
  file would make the cheap path the default one.

- **Persist artifacts and command output.** Fetched reports are re-downloaded
  every time, and command output lives only in scrollback, so a follow-up
  question means re-running something that already computed the answer. An
  artifact cache keyed by source, plus a journal of every invocation (argv,
  exit code, duration, payload), would make "did my fix work, or was that luck"
  a diff. Anything written must go through the existing redaction path, be size-
  bounded, and be opt-outable.

- **Triage a CI run in one command.** Locating one run's per-execution Behave
  JSON currently means listing hundreds of object keys and grepping a timestamp,
  fetching each file singly, then writing a parser. Those JSONs are the better
  source — a whole run is a few hundred KB against a multi-MB HTML report, and
  they already carry per-step status, timings and errors. `s3 triage-run`
  (resolve a run, fetch its JSONs, print totals plus one row per failure), with
  `--prefix` on `list-reports` and a bulk fetch, replaces all of it.

- **Report which code path is live.** Two capabilities silently fall back
  depending on the installed Playwright: the accessibility snapshot and CDP
  emulation. `doctor` should print the resolved versions and, for each such
  capability, which branch is active and what that disables — one line would
  have replaced an afternoon of source-reading.

- **Lint locators.** Upstream guidance is explicit that `.first` hides
  ambiguity rather than resolving it, and that strict mode exists to surface
  unexpected matches. Suites accumulate the opposite: unanchored `//*` xpaths,
  positional `aria-rowindex` / `data-rowindex` selectors, and grid selectors
  with no `role='cell'` guard that silently match the header row. All are
  mechanically detectable from the configured locators directory, as is a
  related trap on the step side — `retry_on_failure`-style decorators whose
  exception tuple omits what the wrapped body actually raises, which turns ten
  attempts into one.

- **Mock one API operation instead of building data for it.** Playwright's
  routing can fulfil or abort a matching request, which is how an error path
  gets tested without contriving backend state -- a suite that can only reach
  "payment declined" by arranging a declined payment mostly does not test it.
  A `--mock OPERATION=status[,body]` on a step slice would cover the common
  case. The counterpart, `add_init_script`, would let a slice pin
  non-determinism (clock, random) before any page script runs.

- **Subscribe to protocol events, not just send commands.** CDP sessions here
  only ever `send`; `session.on(...)` exposes console messages, network timing
  and performance metrics that Playwright's own API does not surface, and
  sessions that are opened are never `detach()`ed. Worth doing when something
  concrete needs it rather than speculatively.

- **Distinguish a dirty debug browser from a clean one.** A browser context is
  the unit of isolation, and a CDP attach necessarily reuses an existing one, so
  a long-lived debug browser accumulates sessions and eventually fails a run at
  its own login — which reads as a test bug. `cdp launch` should record
  provenance, `run --debug` should warn when the target profile was last driven
  by something else, certification should always use a fresh instance, and
  `cdp stop --all` should reconcile against real processes instead of reporting
  zero while browsers are alive.
