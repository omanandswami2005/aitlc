# aitlc — Field Guide

A debugging CLI for Behave + Playwright suites. Structured JSON output, and it
never asks you to edit the suite it debugs.

Version 0.8.0.

Every rule here came from a real investigation that went wrong. Where something
is stated firmly, it is because the opposite was tried first.

## Contents

- [The five-minute version](#the-five-minute-version)
- [One directory per investigation](#one-directory-per-investigation)
- [Setup](#setup)
- [Environment and .env](#environment-and-env)
- [How it attaches to your suite](#how-it-attaches-to-your-suite)
- [The debug cycle](#the-debug-cycle)
- [Reading a live page](#reading-a-live-page)
- [Calling your own code](#calling-your-own-code)
- [Checkpoints](#checkpoints)
- [Running tests](#running-tests)
- [What happened in CI](#what-happened-in-ci)
- [Before you trust a local run](#before-you-trust-a-local-run)
- [Suite health](#suite-health)
- [Xray sync](#xray-sync)
- [Patterns that pay off](#patterns-that-pay-off)
- [Traps](#traps)
- [Troubleshooting](#troubleshooting)

---

## The five-minute version

```bash
aitlc --version
aitlc init                                  # detect layout, write aitlc.toml
aitlc preflight PROJ-1234                   # will this run here like it does in CI?
aitlc -w PROJ-1234 debug start PROJ-1234 --at 12
aitlc -w PROJ-1234 debug next PROJ-1234     # forward, one step, same browser
aitlc -w PROJ-1234 debug retry PROJ-1234    # after an edit, re-run just that step
aitlc -w PROJ-1234 debug certify PROJ-1234  # fresh browser, real feature, twice
aitlc s3 verify-test PROJ-1234              # did it pass in CI last night?
```

Nothing above edits your feature files. That is the point: commenting out the
steps that already passed drifts from the file CI actually runs, and the drift
is invisible until it costs you a day.

---

## One directory per investigation

Every command produces something — traces, cached CI reports, session state,
browser profiles, logs. Point a workspace at what you are working on and it all
lands in one place.

```bash
aitlc --workspace PROJ-29019 debug start PROJ-29019
aitlc -w PROJ-29019 s3 verify-test PROJ-29019
```

```
PROJ-29019/
  .aitlc/artifacts/     cached CI reports, fetched once
  .aitlc/checkpoints/   saved setups
  .aitlc/debug/         session state + gate log
  .aitlc/runs/          the command journal
  .parallel/            one full log per feature
  .cdp/                 browser profiles and logs
  traces/               Playwright traces
```

Switch the name and the previous investigation stays intact beside it. Delete
the directory and it all goes together.

| Where | Use it for |
|---|---|
| `--workspace` / `-w` | one command |
| `AITLC_WORKSPACE` | a shell working on one thing |
| `[project].workspace` | a project that always wants it |

Unset, everything stays under `reports/` as before. The name is relative to the
project root; an absolute path is refused rather than quietly made relative.

---

## Setup

```bash
aitlc init            # writes aitlc.toml
aitlc init --merge    # re-run later: adds new keys, keeps your edits
aitlc doctor          # can this environment actually run tests?
```

`init` reads your repo: where features and steps live, the issue-key prefix in
filenames, the per-scenario setup hook, and the *names* of the environment
variables you use. No secret is read, stored or printed.

`--merge` matters more than it sounds. Re-running `init` on a configured
project is normal — the layout drifts, a setting is added — and overwriting
discards the hand edits that made the file correct.

### Credentials

```toml
[s3]
profile = "my-sso-profile"      # resolved fresh on every call
```

Static keys in a `.env` expire, and when they do `aws sso login` does *not* fix
it: the stale values in the file take precedence over the refreshed profile. A
named profile (or `AWS_PROFILE`) avoids that entirely.

---

## Environment and `.env`

Every `aitlc` command loads a `.env` before it does anything, and every tool it
launches — behave, `paver`, Playwright, the debug gate — inherits it. You do
**not** `export` a wall of variables first. That was only necessary when running
`poetry run paver …` (or behave) *directly*, outside aitlc.

```bash
aitlc paver run parallel --local     # loads ./.env, then runs paver with it inherited
aitlc run PROJ-1234                  # same — .env reaches behave
poetry run paver run parallel        # NOT through aitlc: nothing loads .env here
```

- **Which file.** `./.env` at the project root (where `aitlc.toml` lives).
  Override per command with `--env-file` (`run`, `debug`, …) or
  `--aitlc-env-file` (the `paver` / `behave` / `pw` pass-throughs).
- **Your shell always wins.** A variable already set in the environment is
  never overwritten, so `export FOO=x` or `FOO=x aitlc …` still overrides
  `.env`. Precedence, most-wins first: the shell, then `.env`.
- **Plain `KEY=value` only.** `#` comments and surrounding quotes are handled.
  There is no shell interpolation (`${VAR}`) and no `export ` prefix — write
  `LT_USERNAME=abc`, not `export LT_USERNAME=abc` (the latter would create a
  bogus key `export LT_USERNAME`).
- **No placeholder resolution.** aitlc reads literal values. If your `.env`
  still holds `AWS_SECRET:…`-style placeholders, resolve them into a real file
  first — e.g. `poetry run fetch-secrets --profile … --template .env.tmpl
  --output .env` — then the value is picked up.

So the manual `export` block is gone: put resolved `KEY=value` pairs in `./.env`
and run through `aitlc`. Anything you want to override for one run, prefix it
inline (`DEVICE_NAME=MOBILE_DEVICE aitlc paver run parallel --local`).

### Getting the same values into your own shell

Everything above is about aitlc's own subprocesses. Sometimes you need `.env`
in the shell itself — `step_repl.py`, a raw `poetry run behave`, a `curl`/`psql`
check against a value in there:

```bash
source "$(aitlc env)"
```

One command: activates the project's venv, sets `PYTHONPATH`, and exports every
`.env` key. Nothing sensitive touches stdout — `aitlc env` writes the real
`export …` lines (secret values included) to an owner-only file under the
workspace and prints only that file's path, so the command itself is safe to
run through a logged shell or a tool that captures output. `--no-activate`
skips the venv `source` line if you only want the exports; `--env-file` picks
a different file than `./.env`.

---

## How it attaches to your suite

The rule: **never re-implement what the suite already does.**

| Layer | Owner | How aitlc reaches it |
|---|---|---|
| Browser process | aitlc | detached, isolated profile |
| Browser attach | your suite | `PLAYWRIGHT_CDP_URL` |
| `environment.py` hooks | your suite | behave's `Runner.load_hooks()` |
| Step definitions | your suite | behave's `load_step_definitions()` |
| Step dispatch | behave | `registry.find_match(...)` |
| `before_step` / `after_step` | your suite | fired around every step |
| Evidence | your suite | whatever `after_step` already produces |

**Every path loads your real environment.** `run` and `parallel run` invoke
behave itself, so they always did. `debug` now does too: it drives a real,
paused behave `Runner` and single-steps it — so `before_all`, `before_feature`,
`before_scenario`, `before_step` and `after_step` all fire, with behave's
Context layers, tag handling and Examples binding, not an approximation of them.

That last one matters: `after_step` is where a suite captures its failure
screenshot and its API traffic. A session that skipped it threw away exactly
the material that explains a failure.

`after_scenario` and `after_feature` are deliberately **not** fired between
steps — they tear down what a debug session exists to keep. They never run
automatically on `debug stop` either — a debug/CDP browser is often reused
across invocations, and this suite's own cleanup can include a logout; running
it by default would silently reintroduce the repeated-login cost the whole
engine exists to avoid. Pass `debug stop --cleanup` to fire them for real,
in-process, when you deliberately want a clean handoff (see "The debug cycle"
below).

See [`INTEGRATION.md`](INTEGRATION.md) for the full contract.

---

## The debug cycle

```bash
aitlc debug start PROJ-1234 --at 12    # isolated browser, driven to step 12
aitlc debug next PROJ-1234             # run the step under the cursor, then advance
aitlc debug continue PROJ-1234         # run every remaining step, stop at the first failure
aitlc debug retry PROJ-1234            # after an edit, re-run that step
aitlc debug eval PROJ-1234 "document.title"          # raw JS on the live page
aitlc debug run-text PROJ-1234 "click on element ID: \"save_btn\""  # any step, no cursor move
aitlc debug run-line PROJ-1234 42       # same, by file line number instead of retyping text
aitlc debug status PROJ-1234           # where am I?
aitlc debug screenshot PROJ-1234        # this session's page, no --cdp-url needed
aitlc debug inspect PROJ-1234 --a11y    # same, accessibility tree instead of pixels
aitlc debug list --prune                # every tracked session; drop dead bookkeeping
aitlc debug certify PROJ-1234 --times 2
aitlc debug stop PROJ-1234              # THIS session's browser down; no cleanup hooks fired
aitlc debug stop PROJ-1234 --cleanup    # + fires the suite's real after_scenario/after_feature first
```

`stop --cleanup` calls `after_scenario`/`after_feature` directly, in the live
paused process, with the real context/scenario/feature — not reconstructed
from outside. Opt-in only: `debug start` always launches a fresh, disposable
browser, so cleanup-then-kill there is free, but `run --debug` commonly reuses
a *persistent* CDP browser across invocations, and this suite's own cleanup
can include a tag-driven logout — defaulting cleanup to on would silently
force a real login on the next `run --debug`. Reach for it only when you
deliberately want to end a session clean (a handoff, or the next investigation
needs a logged-out start).

`--at N` runs steps `0..N-1` and parks **on** N without running it. The first
`next` therefore runs N; only once a step has been attempted does `next` move
past it. Advancing first would skip the step you parked on, and everything
after it would fail for a reason that is not there.

### Why it is fast, and why that matters for correctness

`start` drives a **real, paused behave run**: behave runs `before_all`,
`before_scenario` and the setup steps `0..N-1` through its own loop, then parks
at step N holding the live Context, the run-scoped data it minted, and the
browser. `retry` and `next` send a JSON line over a Unix socket and the paused
process advances or re-runs the actual behave `Step` object.

```
old:  next → new process → import behave + playwright + every step module
                         → re-run scenario setup → attach browser → 1 step → exit
new:  next → 0.2 ms socket round-trip → 1 real behave step in the paused process
```

| | Per step |
|---|---|
| Socket round-trip, no step | 0.1–0.2 ms |
| A one-second step | 1.00 s wall, 1.0 s reported |
| Same step, process-per-step (the old way) | **+6 s** |

The speed is the smaller half. A process per step regenerates **run-scoped
data** — generated names, e-mails, ids — so a step waiting for something an
earlier step created polls forever for a name that never existed. It looks
exactly like the application hanging. One paused process means one set of that
data: minted once, shared by every later step, exactly as in a real run.

There is no separate engine and no fallback to fall out of sync with — `debug`
is the gated behave runner, one path. (`steps run` and `call` still run a slice
against a live browser; they are separate features, not a debug mode.)

A slice gets no `before_all`/`before_feature`/`before_scenario` — that is what
makes it fast, and it is the tradeoff for skipping `debug start`. Whatever
those hooks set on `context` is simply absent. `steps run` defaults
`context.screen_width`/`screen_height` to 1280x720 so a project whose
`before_feature` derives a viewport from them does not crash outright — a
missing attribute otherwise looks like the step itself is broken when it never
got to run. The result's `context_defaults_applied` says when this happened,
so it is never silently indistinguishable from a real hook having run. Anything
a hook sets beyond that is still absent; reach for `debug start` once a step
needs more than a viewport default.

### What happens when you edit something

**Python — a step definition, a page object, a locator.** Picked up
automatically: before it runs a step, `retry`/`next` reload your project's step
modules, **and** any other project module a step imports normally (a page
object, a helper) whose file changed since the session started, in that
order — a step's own `from pages.search.search_page import SearchPage`
resolves against the freshly-reloaded attribute, not a stale one. A module
that genuinely can't reload cleanly (its own top-level code raises) reports
its own error in the reply rather than silently running stale code. You pay
the reload once; the browser, the Context and the run-scoped data stay live
across it.

**Gherkin — the feature file itself.** Picked up automatically, no restart.
Before each `next`/`retry` the gate re-parses the feature with behave's own
parser and swaps in the freshly-bound steps (behave binds the Examples row, so
the new steps keep their real tables, docstrings and substituted text). The
cursor follows the step it was on **by text, not index**: inserting a step above
it shifts every index below, so keeping the number would silently move you onto
a different step. If the step you were on is gone (you edited it), the index is
clamped and reported.

```json
"feature": {"steps_before": 57, "steps_after": 58, "cursor": "followed", "index": 13}
```

The re-parse is gated on the file's mtime, so a step with no edit pays only a
`stat` (nothing). The one step after an edit pays the parse — single-digit ms
for a normal feature, ~90 ms for a very large Scenario Outline, both negligible
next to the step's own work.

### Certify is not the debug browser

`certify` launches a fresh instance and runs the real feature, twice by
default. A CDP-attached browser reuses an existing context — the opposite of
isolation — so it is never proof. Two consecutive passes are the default
because one pass does not disprove a race.

### A real `breakpoint()`, not just JS `eval`

`debug eval` reads the live page — useful, but blind to the Python side of a
failing step: a local variable, an intermediate value, anything that never
reaches the DOM. A code-level `breakpoint()` anywhere in project code run
under `debug`/`run --debug` now parks for real instead of hanging — the gate
subprocess's stdin is closed and its stdout is a log file, so a bare `pdb`
prompt would have nowhere to read from or print to. `PYTHONBREAKPOINT` is
pointed at aitlc's own hook instead, which parks on a *second* socket rather
than the main gate one — the main socket is already busy blocking on the very
`next`/`retry` request whose step body called `breakpoint()`, so it cannot
also answer a second concurrent command.

```bash
aitlc debug status PROJ-1234              # "paused_at": "breakpoint" once one is hit
aitlc debug py PROJ-1234 "some_local_var" # Python eval in the paused frame's own scope
aitlc debug resume PROJ-1234              # continue exactly where it stopped
```

`debug py` is evaluated against `frame.f_locals`/`frame.f_globals` — the same
thing `pdb`'s `p <expr>` does — which is the actual value of breakpoint
support: `debug eval` already works at any gate pause, breakpoint or not, so
routing it to a breakpoint pause too adds nothing new by itself. `next`/
`retry` on the same session simply wait until `resume` arrives; nothing is
lost, nothing restarts, the original call just continues from that exact line.

---

## Reading a live page

```bash
aitlc cdp launch                       # detached browser that outlives your shell
aitlc cdp inspect --port 9333 --a11y   # what a screen reader sees
aitlc cdp inspect --port 9333 --storage
aitlc cdp inspect --check '#save,[data-testid="row"]'
aitlc cdp time-until '#banner' --condition hidden
aitlc cdp list                         # every tracked instance, alive or dead
aitlc cdp stop --all
```

The accessibility tree answers "is the upgrade button on screen" in a fraction
of a screenshot's size, and it is directly assertable.

`--storage` reports cookies and localStorage. Values are **fingerprinted**
(length + short digest), not printed: a session cookie is a working credential.
The fingerprint is still stable and distinguishing, so it answers "same session
as before?" without leaking anything. `--reveal` opts in.

`time-until` measures how long the app takes to satisfy a condition — for
tuning a wait against a real backend job. By default the element must first be
seen in the *opposite* state: without that check, an element already hidden
(because the page never loaded) reports a confident "cleared in 0.4s" for
something that never happened.

Already inside a paused `debug` session (not a bare `cdp launch`)? `aitlc
debug eval TEST-ID "<js-expr>"` reads the same live page without needing the
port — it runs against whichever page the gate finds on the paused Context.
`aitlc debug screenshot TEST-ID` and `aitlc debug inspect TEST-ID --a11y` are
the same idea for a screenshot or the accessibility tree: both resolve the
CDP endpoint from the session itself, so there's nothing to look up first.

---

## Calling your own code

```bash
aitlc call 'pages.login:SignInPage.current_user'
aitlc call 'pages.audience:AudiencePage.count_rows' --arg 2
```

The fast loop stops at the Gherkin boundary and real debugging keeps crossing
it. Asserting on a page object's private helper — "which user does the app
actually think is signed in" — is not a step and had no expression at all.

Runs in your project's interpreter, where its modules are importable. Whether
the browser handle is passed is read from the signature; `--pass-browser
yes|no` overrides. Loads `.env` first, same as `run`/`debug`/`doctor` — a
project function that reads a secret or `ENVIRONMENT_URL` works the same way
it would in a real run, not just when those happen to already be exported in
your shell.

---

## Checkpoints

A scenario can cost fifteen minutes of setup before the interesting step.

```bash
aitlc debug checkpoint after-setup --test-id PROJ-1234 \
    --value random_name=abcxyz --entity user=someone@example.com
aitlc debug checkpoints                 # what exists, and is it still usable?
aitlc debug restore after-setup
```

Three things are captured together, because any one alone is useless: browser
state, the run-scoped values your suite minted, and the entities the setup
created on the server.

**Staleness is part of the record.** A session cookie and a fresh user do not
stay valid indefinitely, so a checkpoint carries when it was taken and refuses
to restore past its TTL. Silently restoring a dead session produces exactly the
false failure this exists to prevent.

> Verified: cookies restore byte-identically, attributes intact. Whether the
> *application* accepts a restored session end to end is not yet proven — treat
> restore as "session material replayed", not "you are signed in".

---

## Running tests

```bash
aitlc run PROJ-1234                  # structured JSON result
aitlc run PROJ-1234 --debug          # on failure, parks into a live session (debug retry continues it)
aitlc parallel run -j 4              # concurrent, without editing tags
aitlc parallel run --list            # preview: what runs, what is skipped, why
aitlc parallel focus PROJ-1234       # pin a selection
```

Bare test IDs resolve recursively — no full path, and no tagging other features
to narrow a run. `FILE:LINE` selects one Examples row.

**Omit the id entirely and aitlc uses the project's default feature** — the sole
`*.feature` in `feature_dir`, or an explicit `[project].default_feature`. So a
single-feature project can just run `aitlc run`, `aitlc debug start`, `aitlc
steps run` or `aitlc preflight` with no argument (and `debug next`/`retry`/
`status`/`stop` default to that same session). Skips and Xray tags are respected
exactly as with a named run — it still goes through real behave. A path or id is
always accepted and wins.

### `parallel run` and `paver run parallel`

`aitlc parallel run` is a drop-in for `paver run parallel --local`. It does not
call paver; it runs behave itself with a thread pool and a browser pool, and
adds what the paver path could not do: `--jobs`, per-feature structured
results, `FILE:LINE`, and `--list`.

Two deliberate differences, both fixing real gaps:

- **Discovery is recursive by default.** `glob("features/*.feature")` is not,
  so nested suites are invisible to the paver path. `--no-recursive` restores
  exact parity.
- **Skipped files are reported, not dropped.** "Skipped by tag" must never look
  the same as "never discovered".

The browser pool gates reuse on *in use*, not on task index — with more
features than jobs, a pool indexed by task lets two concurrent runs share one
Chrome. Claimed browsers return in a `finally`, so a failing run cannot shrink
the pool and deadlock the rest.

Each run writes its full console output to `<workspace>/.parallel/<stem>.log`.
Failures also carry which runs overlapped in wall-clock and whether any drove
**the same account** — free evidence, where `--verify-failures` costs a re-run
and stays opt-in.

The skip pre-filter matches a skip tag at any placement, and the
`<tag>_prod` / `_stage` / `_dev` forms, so an environment-tagged file no longer
costs a spawned process to discover a skip written plainly in the file.

### Reuse a live browser instead of a fresh one every run

The most common waste is running full scenarios back to back, each launching a
browser and logging in. Launch one debug Chrome and let every behave-based
command attach to it:

```bash
aitlc cdp launch                     # detached CDP browser, survives the shell
aitlc run PROJ-1234                  # attaches automatically if one is live
aitlc paver run parallel --local     # so does the paver pass-through
```

aitlc sets your suite's CDP env var — default `PLAYWRIGHT_CDP_URL`, named in
`[project].playwright_cdp_env` — so the suite connects to the open browser
instead of launching its own. It never overrides a value already in the
environment. Force a fresh browser with `--no-cdp` (run) or `--aitlc-no-cdp`
(paver/behave); point at a specific endpoint with `--cdp-url` /
`--aitlc-cdp-url`.

**Reusing a browser means reusing whatever state it was left in.** A plain
`aitlc run` (no `--debug`) checks whether the instance it is about to attach
to has ever been driven before and warns if so, since that can carry over a
previous login a fresh scenario does not expect:

```json
{"cdp_attach": "http://127.0.0.1:9333", "via": "PLAYWRIGHT_CDP_URL",
 "warning": "port 9333 has been driven 2 time(s) before (last by 'PROJ-1234') and may already be authenticated -- if this feature's own hooks/steps log in, that step can fail against an already-logged-in browser. Use `aitlc cdp launch --new` for a fresh instance, or `@skip_login` if the reused session is intentional."}
```

The concrete failure this catches: a feature with no `@skip_login`, run a
second time against the same persistent browser — its own login hook fails
against a tab that is already signed in, several steps before the actual
scenario logic, in a way that reads like an unrelated locator problem. Fix
is either matching `@skip_login`-style tagging to the browser's real auth
state, or `aitlc cdp launch --new` for a genuinely fresh instance.

`debug start` carries the same warning when it reuses a tracked instance.
`--debug`'s own older check (`is_dirty_for`) only fires when a *different*
test_id last drove the browser; the same test_id reusing its own browser
across repeated `debug start` attempts previously got no warning from
either path.

### Run the project's own tools, with its environment set up

```bash
aitlc paver run parallel --local     # your suite's real paver task
aitlc behave features/foo.feature    # real behave, with .env + interpreter resolved
aitlc pw show-trace trace.zip        # the project's Playwright CLI
```

These are pass-throughs: aitlc loads your `.env`, resolves the project
interpreter, and hands every remaining argument straight to the tool, so
adopting aitlc never means losing a flag it does not wrap. `--print-command`
shows the exact invocation without running it.

---

## What happened in CI

```bash
aitlc s3 find-test PROJ-1            # which plan and run — no download
aitlc s3 verify-test PROJ-1 PROJ-2   # pass/fail + the failing step and error
aitlc s3 history PROJ-1 --days 14    # chronic, intermittent, or an outage
aitlc s3 triage-run --suite <plan>   # one whole run, one table
```

**A test key is usually not an execution key.** A plan runs one feature file
per execution key, and the tests inside carry their own `@TEST_<KEY>` scenario
tags. Searching object names finds nothing, which reads exactly like "it did
not run" — so `find-test` reports `not_named_by_any_object` and points at
`verify-test`, which reads the documents.

`verify-test` reads the per-test Behave JSON, not the HTML report: orders of
magnitude smaller, already structured, and it carries the tags that make a
nested key findable at all.

```
test          08-17  08-18   rate   verdict
PROJ-1        FAIL   FAIL    3/3    deterministic
PROJ-2        FAIL   PASS    1/3    intermittent
PROJ-3        OUT    PASS    0/2    healthy
```

- **deterministic** — one signature every time. Reproduce it; a single run will
  show it.
- **intermittent** — signatures vary. Establish a base rate before bisecting.
- **OUT** — an outage run, excluded from the rate rather than inflating it.

Failures are grouped by signature with volatile parts (timings, generated ids)
masked, so one defect does not look like five. Scoped by **day**, not by run: a
suite executes many times a day and a run count collapses the matrix to one
column. Written to `<workspace>/.aitlc/test-history.json` so the next reader
does not re-download it.

Triage keeps the call-log lines that explain a failure — the resolved element
(`<button disabled ...>` is not a waiting problem), the overlay that
intercepted the click, and the retry count that separates "never appeared" from
"appeared but blocked".

---

## Before you trust a local run

```bash
aitlc preflight PROJ-1234
```

Reports how a local run would differ from a full execution, without launching
anything. Exits non-zero when something would genuinely run differently, so a
script cannot claim "reproduced locally" over the top of it.

Three differences, all invisible in a failure message:

1. **No session of its own.** In an execution the session comes from a hook or
   an earlier scenario; alone the file starts signed out and fails for a reason
   that does not exist in CI. Sometimes the login is literally commented out.
2. **Hook tags in a position nothing reads.** Hooks read *feature*-level tags,
   while an export from an issue tracker puts the issue's labels on the
   *scenario*. The file looks correctly tagged and gets none of that setup.
3. **Order dependence.** Scenarios in one execution share a browser and each
   other's data.

---

## Suite health

```bash
aitlc doctor                 # versions, and which code path is live
aitlc steps unused           # dead step definitions, via behave's own registry
aitlc locators lint          # positional selectors, grid indexes, `.first`
aitlc history show           # locally recorded outcomes, flakiest first
aitlc journal list           # what was run, how long it took, what it produced
```

`doctor`'s `behave`/`playwright` versions are resolved from the TARGET
project's own environment (a `poetry run python -c ...` probe), not aitlc's
own — the two commonly differ when aitlc is installed separately from the
project it's debugging, and the project's own version is the one that
actually runs every step. `versions_from` in the output says which one you're
looking at; it falls back to aitlc's own only if that probe itself can't run.

**A plain `run` and a `debug` session journal the same way.** A `run`'s JSON
now carries a full `steps` list too, not just `steps_by_status` counts and
`failures` — one entry per step, in file order, with its keyword, status,
duration and error (empty features/skips are `[]`, same fields, so a script
reading it doesn't need a separate "no steps ran" branch). `debug next`/
`retry`/`continue`/`run-text`/`run-line` journal themselves with entries
shaped the same way (a `continue` call's entry carries the whole `results`
list). `journal list`/`show`/`diff` therefore cover a debug session exactly
like a full run, however many `next`/`retry` calls it took — the only
practical difference is you get one entry per debug command instead of one
per whole run.

---

## Xray sync

```bash
aitlc xray get-gherkin PROJ-1234
aitlc xray compare-gherkin PROJ-1234 --file path/to.feature
aitlc xray update-gherkin PROJ-1234 --file path/to.feature
aitlc xray find-step-usage "click on element ID"
```

`update-gherkin` normalizes its input and refuses header lines. Passing a whole
`.feature` file used to write the `Feature:` line into the Test body, which is
data loss. `compare-gherkin` normalizes the same way, so a tab-indented
Examples row no longer shows a permanent phantom diff.

**Back up before writing.** `get-gherkin` to a file first; that makes the push
reversible.

---

## Patterns that pay off

**Edit and step, don't restart.** This is the whole point of the loop: when a
step fails, edit it — the Gherkin line and its params, or the Python behind it —
and run `debug retry`. Before every `retry`/`next` the gate re-parses the
feature and reloads your step modules, so the edit runs against the browser,
login and setup you already have. Editing Gherkin is now as cheap as editing
Python; reserve `debug start` for a change to a setup step *before* your park
point.

**Cheapest evidence first, browser last.** A whole triage can reach a diagnosis
with no test run: totals from `triage-run`, the full call log from the cached
JSON, one trace frame, and `find-step-usage` for the established pattern before
writing any fix.

**Require a positive signal, not the absence of a negative one.** "The page did
not redirect" does not mean you are logged in — a rejected token leaves the app
on its own URL rendering nothing. Assert on something that only exists in the
success state.

**Check your check.** A verification that passes before the change is applied
is worthless. Prove it fails when it should.

**Chronic and intermittent need opposite responses.** Same step and error every
run: reproduce it. Varying signatures: establish a base rate first. Acting on
the wrong one wastes hours.

**A disabled button is not a waiting problem.** When a click times out, read
the `locator resolved to ...` line before touching the timeout.

**Run the scenario the way the runner runs it.** If a local reproduction
behaves strangely *before* reaching the step under test, suspect the harness,
not the app.

**If a test replaces one of the tool's own functions, another test must not.**
A fake written to match the call site rather than the real function let a
command ship crashing on every invocation with the suite green.

---

## Traps

**Feature tags are not scenario tags.** Hooks read `feature.tags`. An export
from an issue tracker writes the issue's labels on the scenario, so a file can
look tagged and select nothing. `preflight` catches it.

**Accounts switch only via logout.** There is usually no Sign In affordance
while a session is live.

**Email slots are not interchangeable.** `random_email`, `freemium_email` and
friends are pre-generated per slot; creating a user against one and signing in
against another produces a user that exists and cannot be used.

**A skipped feature can look like a passing one.** `passed: true` with no
passed steps is what a skip looks like. `parallel run` reports `skipped` as its
own outcome.

**A stale socket file looks alive.** Liveness is a round trip, not an
`exists()`.

**Calling plain `run` again after every fix is the expensive habit this
engine exists to break.** A plain `run` (no `--debug`) is a brand-new process
that pays full setup — including a real login — from scratch every time, so
repeating it after every fix compounds that cost across the whole debugging
session and looks exactly like "the suite is stuck, failing the same way
forever." `run --debug` already avoids this on its own: the first real
failure parks into a live, resumable session instead of exiting (see
Troubleshooting, "`paused_on_failure`"), so `aitlc debug retry <test_id>`
continues it with zero new setup — no separate `debug start` needed, even for
the very first failure. Reach for `debug start --at N` instead only when you
already know which step you want to park before, or need a fresh isolated
browser for some other reason.

---

## Troubleshooting

**`aitlc --version` disagrees with what you edited.** You are running an
installed copy. The version is read from package metadata, so it is telling you
the truth.

**Commands answer as if the answer were "none".** You are outside the project
root and config resolution failed. That is now an error rather than a confident
wrong answer.

**S3 commands fail after `aws sso login`.** Static keys in `.env` take
precedence over the refreshed profile. Set `[s3].profile`.

**`debug start` says the run did not reach the park point.** A setup step
failed, so behave (`--stop`) aborted before parking — the session is not left on
a broken precondition. Read `<workspace>/.aitlc/debug/gate.log` for the failing
step; a common cause is a step module failing to import because the env file was
not found.

**A `run` result is completely empty (`steps_by_status: {}`, no failures) but
exit code is nonzero.** behave died before writing any report at all — most
often a `SyntaxError`/`ImportError` in a hooks or steps module (an edit that
did not actually save cleanly, a stray character pasted into a file) — not a
step failure, so re-running reproduces the identical empty result every time
and looks like the suite is stuck. Check `crash_traceback` in the same JSON
output first; it holds the real traceback. With `--debug` the same
distinction applies, correctly: `crashed: true` (with `crash_traceback`)
means behave crashed before running anything real; `paused_on_failure: true,
resumable: true` means a real step failed and the process is now a live
session — not frozen, not exited, waiting for `aitlc debug retry <test_id>`
(or `next`). The two used to be indistinguishable, and `paused_on_failure`
used to mean "inspect-only, then restart"; it doesn't anymore.

**Edited a step's body and `retry`/`next` still ran the old code.** Fixed —
this used to be a real bug, not you misreading the reload. behave doesn't
load step files through Python's import system at all (`exec_file()`s each
one into a throwaway namespace), and its own step registry can get silently
replaced out from under a long-lived process by a project's own
`reset_runtime()` call (used for worker/scenario isolation in some suites).
Both are now accounted for in the reload path. If you ever see it again on a
current build, it's a real regression worth reporting, not an edit you
mistyped.

**A step that never ran reports `not_run`, not `failed`.** Three outcomes, not
two. Calling it failed sends you to fix code that is fine.

**The debug browser looks like a phone.** It defaults to desktop to match a
real run; pass `--window-size` for a mobile suite.

**`debug list` shows a session from days ago.** A session file persists until
`debug stop` clears it — if that never happened (a crash, a killed shell,
forgetting), the bookkeeping just sits there. `debug list --prune` drops any
session whose gate process no longer actually answers its socket, and only
that one: a session still genuinely alive is left alone, since it may be one
you haven't finished with yet, not something to guess about.
