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

Run & investigate one test:
- **Run a test, get a structured result** → `aitlc run PROJ-1234` (`--debug` —
  a real failure parks into a live, resumable session instead of exiting;
  `aitlc debug retry` continues it. `--dry-run` just checks every step
  resolves. `--retry N` / `--retry-only-if-known-flake` for flake handling.
  `--mobile` / `--remote` for local mobile emulation or a real LambdaTest
  device.)
- **Single-step / reproduce a failure** → `debug start/next/retry/status/stop`
  (see "The debug cycle" below).
- **Run the whole rest of a scenario, not one step at a time** → `debug
  continue` — advances through every remaining step and stops at the first
  failure (or the end), instead of driving `next` in a shell loop yourself.
- **Investigate mid-session** → `debug eval "<js-expr>"` (raw JS against the
  live paused page) or `debug run-text "<gherkin-step>"` (any registered
  step, without advancing the cursor).
- **Certify a fix actually holds** → `debug certify --times N` — a fresh
  instance, never the debug browser (which only *looks* proven).
- **Run a slice of steps outside a real session** → `steps run <feature>
  --range START-END` — cheaper than `debug` when you don't need the gate's
  live-editing loop, but gets no `before_scenario` (the project's
  `scenario_setup` config covers the common case; a suite with real
  tag-driven skip/branch logic still needs `debug`).
- **Find dead step definitions** → `steps unused`.
- **Call one project function directly** → `aitlc call 'module:attr'` — a
  page object's private helper, without writing a throwaway Playwright
  script.
- **Run many tests concurrently** → `aitlc parallel run -j 4` (`focus`/`--clear`
  to scope a sweep without editing tags).
- **Record a session and map its selectors** → `aitlc record <url>
  --suggest-steps`.

CI and history:
- **Did it pass in CI?** → `aitlc s3 verify-test PROJ-1234`.
- **Is it chronic or flaky?** → `aitlc s3 history PROJ-1234 --days 14`.
- **What actually failed in a CI run?** → `aitlc s3 triage-run --suite <plan>`
  — reads the run's own JSON; never re-run locally just to see where it
  failed.
- **Match a failure against known patterns** → `aitlc classify-failure
  <report.json>` (accepts both `aitlc run`'s own JSON and a raw behave
  `json.pretty` report).
- **Package a fix for review** → `aitlc propose-fix TEST-ID --diff <diff>
  --out proposal.md` — proposes, never commits.
- **Will it even run here like CI does?** → `aitlc preflight PROJ-1234`.
- **Replay a run as a shareable terminal recording** → `aitlc report
  TEST-ID --out replay.html`.
- **What's aitlc's own history of running this?** → `aitlc journal
  list/show/diff` — "did my fix work, or was that luck" without a re-run.

Environment and setup:
- **Read a live page as text, storage, or a raw call** → `aitlc cdp
  inspect --a11y` / `--storage`.
- **Manage the persistent debug browser** → `aitlc cdp launch/status/list/stop`.
- **Is the environment even set up right?** → `aitlc doctor` (behave/playwright
  versions **from the target project's own environment**, not aitlc's own —
  add `--remote` for LT credential/tunnel/proxy checks).
- **Activate the venv + load `.env` in one line** → `source "$(aitlc env)"`.
- **Bootstrap a new project** → `aitlc init` (`--merge` to add only newly
  detected keys without touching what's already set).
- **Trace evidence** → `aitlc trace show/list-frames/extract-frame/fetch-s3`
  on a Playwright trace `.zip`.
- **Locator risk** → `aitlc locators lint` / `rules`.

Xray sync:
- **Read/write/compare Gherkin** → `aitlc xray get-gherkin | update-gherkin |
  compare-gherkin`.
- **Test/Execution relationships** → `aitlc xray create-test |
  link-to-execution | executions-for-test | tests-for-execution |
  runs-for-execution`.
- **Where is a step actually used?** → `aitlc xray find-step-usage`.
- **Pull CI's own feature files down** → `aitlc xray fetch-features
  <plan-or-execution> --status FAILED`.

Escape hatches and other integrations:
- **Run the project's own tooling with its env set up** → `aitlc behave …`,
  `aitlc pw …`, `aitlc paver …`.
- **Jira** → `aitlc jira create-task` (a real, visible side effect — confirm
  before running against a shared board).
- **Post a Teams notification** → `aitlc notify-teams` (also a real, visible
  side effect).
- **A LambdaTest tunnel** → `aitlc tunnel start/stop/status`.
- **The shared pooled-user table** → `aitlc users validate/generate` — acts
  directly on shared infrastructure with no dry-run; both refuse without
  `--yes` on purpose.

## `steps`/`call` vs `debug` — not a fallback relationship

`debug` is the gated behave engine: real `before_all`/`before_scenario`, a
live Context, single-stepping over a control socket. `steps run` and `call`
are separate, lighter-weight tools built on the same underlying console
(`core/step_console.py`), not a degraded version of `debug` kept around for
compatibility — each fits a real, distinct case:

- `steps run --range` skips `before_scenario` entirely (the project's
  `scenario_setup` config backfills the common per-scenario data case, but
  real tag-driven skip/branch logic in the project's own hooks still won't
  run) — reach for it when you want a slice fast and don't need the gate's
  live-editing loop.
- `call` reaches a project function directly, with no Gherkin expression at
  all — the case `debug`/`steps` can't cover by design, since there is no
  step to bind to.

Use `debug` when you need real hook fidelity or the edit-and-retry loop;
reach for `steps`/`call` when the slice or the function is all you need and
paying for a full gated session isn't worth it.

## The debug cycle (single-stepping real behave)

```bash
aitlc -w PROJ-1234 debug start PROJ-1234 --at 12   # real behave runs setup, parks at step 12
aitlc -w PROJ-1234 debug next PROJ-1234            # run the current step, advance
aitlc -w PROJ-1234 debug continue PROJ-1234        # run every remaining step, stop at the first failure
aitlc -w PROJ-1234 debug retry PROJ-1234           # after an edit, re-run that step (no restart)
aitlc -w PROJ-1234 debug eval PROJ-1234 "document.title"      # raw JS on the live page
aitlc -w PROJ-1234 debug run-text PROJ-1234 "click on element ID: \"save_btn\""  # any step, no cursor move
aitlc -w PROJ-1234 debug status PROJ-1234          # where the paused run is
aitlc -w PROJ-1234 debug certify PROJ-1234 --times 2   # fresh instance, real feature, N passes
aitlc -w PROJ-1234 debug stop PROJ-1234            # tear down the browser + session
```

`stop` kills only the browser THIS session launched, by default — it does NOT
fire the suite's real `after_scenario`/`after_feature` (a tag-driven logout,
cleanup, etc.). Pass `--cleanup` to run those for real before exiting, when
you deliberately want a clean handoff — never make it the default in your own
workflow, since a `run --debug` session often reuses a *persistent* CDP
browser across invocations, and an automatic logout there would silently
reintroduce the repeated-login cost this whole engine exists to avoid.

- `--at N` runs steps `0..N-1` through real behave and parks **on** N without
  running it; the first `next` runs N.
- `--example K` targets one Scenario Outline row (behave binds it via `FILE:LINE`).
- `retry` re-runs the current real behave `Step` object after reloading edited
  step modules **and** any other project module a step imports normally (a
  page object, a helper) that changed since the session started — both are
  picked up automatically, in the right order, before the step re-runs. A
  module that genuinely can't reload cleanly reports its own error instead of
  either running stale code or crashing the session.
- **Gherkin edits are picked up too, no restart.** Before each `next`/`retry`
  the gate re-parses the feature and swaps in the freshly-bound steps (cursor
  follows by text). Re-parse is mtime-gated, so an unedited step pays nothing.
- `--background` returns immediately; poll `aitlc debug status`.
- `certify` is deliberately a fresh instance, never the debug browser: a
  CDP-attached browser reuses an existing context, so it is not proof.
- **No id needed in a single-feature project.** Omit the test id and every
  feature-running command (`run`, `debug start`, `steps run`, `preflight`, and
  `debug next`/`retry`/`status`/`stop`/`eval`/`run-text`) uses the project's
  default feature — the sole `*.feature` in `feature_dir`, or
  `[project].default_feature`. A path or id always wins.

**Use the loop — edit and step, don't restart.** This is the point of the
engine: when a step fails, edit the failing step (the Gherkin line and its
params, or the Python behind it) and just run `debug retry` (or `next`). Before
each one the gate re-parses the feature and reloads your step modules, so your
edit runs against the browser, login and setup you already paid for. You only
need a fresh `debug start` when you change a setup step *before* your park
point. Editing Gherkin — the common case — is now as cheap as editing Python.

There is no separate "step console" or fallback engine: `debug` is the gated
behave runner, one path — `steps`/`call` are separate tools for their own
narrower cases, not a lesser version of this one (see the section above).

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

`debug stop` only ever kills the browser its OWN `debug start` launched
(tracked by port on the saved session) — a separate `aitlc cdp launch` for
manual work elsewhere is never touched by it. `aitlc cdp stop --all` is the
one command that is deliberately project-wide.

## Pause-on-failure is a live session now, not a dead end

`aitlc run --debug` runs every step normally; the moment one fails, the process
parks into the SAME live, socket-served gate `debug start` uses — instead of
exiting. There is no restart, and no separate `debug start` needed even for the
first failure:

```bash
aitlc run PROJ-1234 --debug
# → {"paused_on_failure": true, "resumable": true, "parked_at": 7,
#    "current_step": "...", "error": "AssertionError: ...",
#    "hint": "fix the code, then: aitlc debug retry PROJ-1234"}
aitlc debug retry PROJ-1234   # fix the code, retry the exact failed step, same session
```

The `error` field carries the actual assertion/exception text, not just where
it happened — `debug status` on the same session reports it too, for every
poll, not only the first one.

Provided entirely by aitlc's runner — the suite needs no `after.py`/
`environment.py` edit. A crash before any step runs (a hooks/steps
`SyntaxError`/`ImportError`) is reported as `crashed: true` with the real
traceback instead, correctly distinguished from a genuine pause.

## Universal debugging practices (this tool and beyond)

Lessons that generalize past this specific CLI — for a human or an agent
driving it, on this project or any other:

- **Read the real option list before guessing a fixed-choice value.** A UI
  filter, a dropdown, an enum-like config value — typing a plausible-sounding
  choice and treating a downstream metric that changed as proof it was
  accepted is a trap: the automation underneath a filter/select often just
  string-matches whatever you gave it, and a *wrong* value can silently match
  something adjacent (a tooltip, a nearby label) rather than failing loudly.
  Enumerate the actual live options first — an accessibility snapshot of the
  open control, or `debug eval` reading the DOM directly — before trusting a
  guess.
- **A batch that continues past one failure is not proof everything before it
  worked.** Any non-interactive, multi-step driver (a piped script, a batch
  command, `parallel run`) that logs an error and keeps going makes a later
  step's clean PASS look like confirmation of everything before it. Read the
  full transcript for the steps that matter; don't infer success for step N
  from step N+1 having run at all.
- **A convenience wrapper can silently drift out of sync with what it wraps.**
  A local script, a fast-path shortcut, a cached install — anything that
  duplicates or shells out to the real thing can fall behind it. Prefer a
  wrapper that degrades gracefully (falls back to the real path with a clear
  note) over one that hard-crashes when its assumption about the underlying
  tool's shape stops holding.
- **Require a positive signal, not the absence of a negative one.** "Nothing
  errored" or "the page didn't redirect" is not the same as "the thing I
  wanted happened" — a rejected token can leave an app looking idle rather
  than failing visibly. Assert on something that only exists in the actual
  success state.
- **Check your check.** A verification that already passes before the fix is
  applied proves nothing. Run it against a deliberately broken input first,
  and confirm it fails the way you'd expect — otherwise a green check might be
  passing for the wrong reason.
- **Diff against a known-good case before assuming a new bug.** When several
  failures share a signature, a working sibling (same setup shape, same
  account type) is often the fastest root-cause tool available — order and
  state can matter as much as content, and a passing case shows the order
  that's actually relied on.
- **When something's "hidden" or inconsistent, check state before code.** A
  shared/reused account, a stale cache, leftover data from a previous run —
  all of these can look exactly like a locator or timing bug until you
  actually look at the current state directly.
- **Verify a fix live against the real system before calling it done**,
  not just against a synthetic/unit-level reproduction — a fix that only
  passes in isolation may not hold against the real environment's actual
  versions, config, and data. This is the whole reason `debug`'s gate drives
  real behave instead of reconstructing it, and why `certify` insists on a
  fresh instance rather than the browser already in front of you.

## Known gaps and workarounds

- **No interactive Python REPL on pause.** `debug`'s pause gives step-level
  granularity (the browser, the failing step, now the real error text) but not
  a live Python frame with local variables. `debug eval`/`run-text` cover the
  browser/step side of this; for a Python-logic bug, a temporary
  `breakpoint()` in the page object plus a foreground, `--no-capture` run is
  still the way to get a real local-variable frame.
- **Page introspection beyond `cdp inspect --a11y`/`debug eval` is limited.**
  No first-class console-log or network-request capture from a paused
  session yet — a project's own request/response logging (if it has any) is
  the fallback.
- Two gaps from real dogfooding aren't root-caused yet: a live `next`/`retry`
  session can report `status=undefined` for a step that should clearly match,
  after many edits and cursor moves in one long session (workaround: start a
  fresh session rather than chasing it mid-session); and `run --debug` has,
  at least once, failed to collect any steps at all for one particular
  feature shape while `run` (no `--debug`) worked fine on the exact same file
  — if this happens, fall back to plain `run` for that one feature rather
  than assuming the suite itself is broken.

## Rules aitlc holds to (and you should too)

- **Never report success for something that did not happen.** A missing
  precondition, an unreachable CDP URL, or a skipped feature is reported
  explicitly, not rendered as an innocent empty/normal result.
- **Ask the tool, don't assume its version.** aitlc probes `behave --help`
  for the runner flag, and a target project's real installed behave/
  playwright versions via that project's own environment, rather than
  assuming either from aitlc's own.
- **Nothing project-specific in the code.** Layout, hook names, credential and
  CDP env-var names all come from `aitlc.toml`; `aitlc init` detects them.
- **Cheapest evidence first, browser last.** `s3 history`/`triage-run` and the
  cached CI JSON usually reach a diagnosis with no test run.
- **A fix isn't done until it's verified against the real thing it claims to
  fix** — a unit test alone can pass while the actual command still crashes
  end to end; run it for real against a real project before calling it closed.

## Reference

- `README.md` — overview and quick start.
- `USER-GUIDE.md` / `user-guide.html` — full reference (written to be read by an agent).
- `ARCHITECTURE.md` — how the package is organised (gate runner, command registry, adapters).
- `INTEGRATION.md` — the exact layer-by-layer contract between aitlc and a suite.
- `ROADMAP.md` — every gap found and fixed, release by release, with the real repro that motivated each one.
- `aitlc.toml.example` — every config key, annotated.
