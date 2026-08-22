---
name: aitlc
description: >-
  Use when debugging a Behave + Playwright test suite, reproducing a CI failure
  locally, single-stepping a scenario, checking what a test did in CI, or
  syncing a Test's Gherkin with Xray — via the `aitlc` CLI. Applies to any repo
  that has an `aitlc.toml` (run `aitlc init` if it does not).
---

# aitlc — debugging Behave + Playwright suites

`aitlc` is a JSON-first CLI that debugs a Behave + Playwright suite and keeps it
in sync with Xray. It **never edits the suite it debugs** — instrumentation
attaches through behave's own runner API.

The one idea that explains the design: **aitlc does not re-implement behave; it
drives the real one.** `run`, `certify`, and the `debug` engine are all genuine
behave. So a debug session binds Examples rows, mints run-scoped data once,
parses data tables, and fires the project's own hooks — because it *is* behave,
merely paused.

## When to use which command

- **Reproduce / single-step a failure** → `debug` (see below).
- **Run a test and get a structured result** → `aitlc run PROJ-1234`
  (add `--debug` — a real failure parks into a live, resumable session instead
  of exiting; `aitlc debug retry` continues it).
- **Run many tests concurrently** → `aitlc parallel run -j 4`.
- **Did it pass in CI?** → `aitlc s3 verify-test PROJ-1234`.
- **Is it chronic or flaky?** → `aitlc s3 history PROJ-1234 --days 14`.
- **Will it even run here like CI?** → `aitlc preflight PROJ-1234`.
- **Read a live page as text** → `aitlc cdp inspect --a11y`.
- **Xray Gherkin** → `aitlc xray get-gherkin|compare-gherkin|update-gherkin`.
- **Run the project's own tooling with its env set up** → `aitlc behave …`,
  `aitlc pw …`, `aitlc paver …` (pass-through escape hatches).

## The debug cycle (single-stepping real behave)

```bash
aitlc -w PROJ-1234 debug start PROJ-1234 --at 12   # real behave runs setup, parks at step 12
aitlc -w PROJ-1234 debug next PROJ-1234            # run the current step, advance
aitlc -w PROJ-1234 debug retry PROJ-1234           # after an edit, re-run that step (no restart)
aitlc -w PROJ-1234 debug status PROJ-1234          # where the paused run is
aitlc -w PROJ-1234 debug certify PROJ-1234 --times 2   # fresh instance, real feature, N passes
aitlc -w PROJ-1234 debug stop PROJ-1234            # tear down the browser + session
```

`stop` kills the browser only, by default — it does NOT fire the suite's real
`after_scenario`/`after_feature` (a tag-driven logout, cleanup, etc.). Pass
`--cleanup` to run those for real before exiting, when you deliberately want a
clean handoff — never make it the default in your own workflow, since a
`run --debug` session often reuses a *persistent* CDP browser across
invocations, and an automatic logout there would silently reintroduce the
repeated-login cost this whole engine exists to avoid.

- `--at N` runs steps `0..N-1` through real behave and parks **on** N without
  running it; the first `next` runs N.
- `--example K` targets one Scenario Outline row (behave binds it via `FILE:LINE`).
- `retry` re-runs the current real behave `Step` object after reloading edited
  step modules — no scenario restart.
- **Gherkin edits are picked up too, no restart.** Before each `next`/`retry`
  the gate re-parses the feature and swaps in the freshly-bound steps (cursor
  follows by text). Re-parse is mtime-gated, so an unedited step pays nothing.
- `--background` returns immediately; poll `aitlc debug status`.
- `certify` is deliberately a fresh instance, never the debug browser: a
  CDP-attached browser reuses an existing context, so it is not proof.
- **No id needed in a single-feature project.** Omit the test id and every
  feature-running command (`run`, `debug start`, `steps run`, `preflight`, and
  `debug next`/`retry`/`status`/`stop`) uses the project's default feature — the
  sole `*.feature` in `feature_dir`, or `[project].default_feature`. A path or
  id always wins.

**Use the loop — edit and step, don't restart.** This is the point of the
engine: when a step fails, edit the failing step (the Gherkin line and its
params, or the Python behind it) and just run `debug retry` (or `next`). Before
each one the gate re-parses the feature and reloads your step modules, so your
edit runs against the browser, login and setup you already paid for. You only
need a fresh `debug start` when you change a setup step *before* your park
point. Editing Gherkin — the common case — is now as cheap as editing Python.

There is no separate "step console" or fallback engine: `debug` is the gated
behave runner, one path.

## Reuse a live browser instead of full runs (CDP)

The most common waste is repeated full `run`s. Launch one debug Chrome and let
every command attach to it:

```bash
aitlc cdp launch                       # detached CDP browser, survives the shell
aitlc run PROJ-1234                    # attaches to it automatically if one is live
aitlc paver run parallel --local       # same: reuses the live browser
```

`aitlc` sets the suite's CDP env var (default `PLAYWRIGHT_CDP_URL`, configurable
as `[project].playwright_cdp_env`) so the suite attaches instead of launching a
fresh browser. `run` accepts `--cdp-url URL` / `--no-cdp`; `paver`/`behave`
accept `--aitlc-cdp-url` / `--aitlc-no-cdp`.

## Pause-on-failure is a live session now, not a dead end

`aitlc run --debug` runs every step normally; the moment one fails, the process
parks into the SAME live, socket-served gate `debug start` uses — instead of
exiting. There is no restart, and no separate `debug start` needed even for the
first failure:

```bash
aitlc run PROJ-1234 --debug
# → {"paused_on_failure": true, "resumable": true, "hint": "fix the code, then: aitlc debug retry PROJ-1234"}
aitlc debug retry PROJ-1234   # fix the code, retry the exact failed step, same session
```

Provided entirely by aitlc's runner — the suite needs no `after.py`/
`environment.py` edit. A crash before any step runs (a hooks/steps
`SyntaxError`/`ImportError`) is reported as `crashed: true` with the real
traceback instead, correctly distinguished from a genuine pause.

## Rules aitlc holds to (and you should too)

- **Never report success for something that did not happen.** A missing
  precondition, an unreachable CDP URL, or a skipped feature is reported
  explicitly, not rendered as an innocent empty/normal result.
- **Ask the tool, don't assume its version.** aitlc probes `behave --help`
  for the runner flag rather than parsing a version string.
- **Nothing project-specific in the code.** Layout, hook names, credential and
  CDP env-var names all come from `aitlc.toml`; `aitlc init` detects them.
- **Cheapest evidence first, browser last.** `s3 history`/`triage-run` and the
  cached CI JSON usually reach a diagnosis with no test run.

## Reference

- `README.md` — overview and quick start.
- `USER-GUIDE.md` / `user-guide.html` — full reference (written to be read by an agent).
- `ARCHITECTURE.md` — how the package is organised (gate runner, command registry, adapters).
- `INTEGRATION.md` — the exact layer-by-layer contract between aitlc and a suite.
- `aitlc.toml.example` — every config key, annotated.
