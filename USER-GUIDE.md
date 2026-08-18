# aitlc — Field Guide

A CLI for debugging Behave + Playwright suites and keeping them in sync with
Xray. Structured JSON output first, so both a person and an agent can read
every result.

**Design rule that shapes everything here:** aitlc never requires you to edit
the project it debugs. No hook blocks, no `environment.py` changes, nothing to
keep in sync. See [How it stays codebase-independent](#how-it-stays-codebase-independent).

---

## Contents

- [Setup](#setup)
- [One directory per investigation](#one-directory-per-investigation) — `--workspace`
- [How it stays codebase-independent](#how-it-stays-codebase-independent)
- [Running tests](#running-tests) — `run`, `parallel`, `behave`
- [Debugging live](#debugging-live) — `cdp`, `steps`
- [Reading a page cheaply](#reading-a-page-cheaply) — `cdp inspect --a11y`
- [Suite health](#suite-health) — `steps unused`, `history`, `doctor`
- [Xray sync](#xray-sync) — `xray`
- [Evidence](#evidence) — `trace`, `s3`, `report`
- [What happened in CI](#what-happened-in-ci) — `s3 find-test`, `verify-test`, `history`
- [Escape hatches](#escape-hatches) — `behave`, `pw`
- [Everything else](#everything-else)
- [Troubleshooting](#troubleshooting)

---

## One directory per investigation

Every command produces something — traces, cached CI reports, session state,
browser profiles, logs. Point a workspace at whatever you are working on and
all of it lands in one place:

```bash
aitlc --workspace PROJ-29019 debug start PROJ-29019
aitlc -w PROJ-29019 s3 verify-test PROJ-29019
```

```
PROJ-29019/
  .aitlc/artifacts/    cached CI reports, fetched once
  .aitlc/debug/        session state
  .aitlc/runs/         the command journal
  .cdp/                browser profiles and logs
  traces/              Playwright traces
```

Switch the name and the previous investigation stays intact beside it, so
"what did we collect last time" is answered by looking rather than by
remembering. Delete the directory and everything from that investigation goes
with it.

Three ways to set it, most specific first:

| Where | Use it for |
|---|---|
| `--workspace` / `-w` | one command |
| `AITLC_WORKSPACE` | a shell working on one thing for a while |
| `[project].workspace` in `aitlc.toml` | a project that always wants it |

Unset, everything stays under `reports/` exactly as before. The name is
relative to the project root; an absolute path is refused rather than quietly
turned into one inside the project.

---

## Setup

### 1. Install

```bash
uv tool install aitlc          # or: pipx install aitlc
aitlc --help
```

The same tool is published under two distribution names — `aitlc` and
`dax-aitlc`. They install identical code and both provide the `aitlc` command;
pick whichever name your organisation's index prefers. Install only one.

aitlc runs *outside* your project's virtualenv and shells into it, so it does
not need to share dependencies with the suite under test.

### 2. Generate `aitlc.toml`

```bash
cd /path/to/your/project
aitlc init --dry-run     # see what it detected, write nothing
aitlc init               # write aitlc.toml
```

`init` reads the repo and fills the file in: feature and step directories from
where the `.feature` files and step decorators actually are, the issue-key
prefix from feature filenames, and `scenario_setup` from the call inside your
own `before_scenario`. `[env]` is populated from your `.env` — **variable names
only; no value is ever read, stored or printed**.

Each detection reports how it was found and how confident it is. Anything
undetected is written as a commented placeholder rather than guessed, because a
wrong value written confidently fails later and somewhere else.

aitlc searches upward from the current directory, so one file at the repo root
covers every subdirectory. The generated file looks like this:

```toml
[project]
name = "myproject"
issue_key_prefix = "PROJ-"
feature_dir = "features"
step_dir = "features/steps"
locators_dir = "config/web_locators"

# The project's own per-scenario setup, run with behave's (context, scenario)
# signature. Without this, a step slice gets no before_scenario at all.
scenario_setup = "features.environment_helpers:populate_scenario_data"

# The class your steps actually talk to. Most suites wrap Playwright's Page in
# one; name it here and a step slice gets the same context.browser a real run
# would. Leave unset if your steps use the Page directly.
browser_actions = "helpers.browser:BrowserActions"

[env]
# Map aitlc's generic credential names -> your project's actual env var names.
# aitlc never stores secrets; it only learns which variables to read.
jira_token = "JIRA_TEST_TOKEN"
jira_xray_client_id = "JIRA_XRAY_CLIENT_ID"
jira_xray_client_secret = "JIRA_XRAY_CLIENT_SECRET"

[xray]
graphql_url = "https://xray.cloud.getxray.app/api/v2/graphql"
```

### What each `[project]` setting controls

| Setting | Detected by `init`? | What it changes |
|---|---|---|
| `name` | yes — directory name | Labels output only |
| `issue_key_prefix` | yes — from feature filenames | Lets you type `1234` instead of `PROJ-1234` |
| `feature_dir` | yes — where `.feature` files live | Bare-ID resolution searches here, recursively |
| `step_dir` | yes — where step decorators live | `steps unused`, and the step console's registry |
| `locators_dir` | yes — common locator layouts | `record --suggest-steps` uses it to tell a newly recorded selector from one you already have |
| `scenario_setup` | yes — the call inside your `before_scenario` | Makes `steps run` slices work on data-dependent steps |
| `browser_actions` | no — write it yourself | The class assigned to `context.browser` in a step slice. Without it the raw `Page` is used, and steps written against a wrapper fail on the first call |
| `browser_factory` | no — write it yourself | A class exposing `launch_local_mobile_browser_via_cdp(playwright, cdp_url, device_name)`. Only needed to combine `--mobile` with `--cdp-url` |

The two undetected settings are undetected on purpose: there is no reliable
signal for which of a project's many classes is *the* browser wrapper, and
guessing wrong produces a confusing failure several steps later. `aitlc init`
writes them as commented placeholders instead.

### 3. Verify

```bash
aitlc doctor          # environment checks
aitlc run <TEST-ID> --dry-run   # confirms steps resolve, no browser
```

`--dry-run` is the cheapest real signal that config, paths and step imports
are all correct.

---

## How it stays codebase-independent

A debugging tool that requires editing the suite it debugs cannot be adopted
incrementally, cannot be uninstalled cleanly, and silently becomes a no-op for
anyone who installs the tool but not the edit.

aitlc attaches instead, in three layers:

**1. behave's own runner option (preferred).** aitlc ships
`aitlc.runtime.runner:AitlcRunner`, a `Runner` subclass passed via behave's
documented `--runner-class` / `--runner` flag. It calls `super().run_hook(...)`
for every hook, so all your project hooks still run, unchanged and in order —
aitlc only observes, and halts when explicitly asked.

**2. Version detection.** The flag name *and* the class-path format differ by
behave version, and both fail with the same unhelpful message when wrong:

| behave | flag | path format |
|---|---|---|
| 1.2.7.dev | `--runner-class` | `pkg.module.Class` (parsed with `rsplit(".", 1)`) |
| 1.3+ | `--runner` / `-r` | `pkg.module:Class` |
| 1.2.6 | neither | — |

aitlc asks `behave --help` what it supports rather than parsing a version
string, because forks and vendored builds do not follow upstream numbering.

**3. A universal fallback.** Where no flag exists, aitlc writes a
`sitecustomize.py` into a temp directory on `PYTHONPATH`. Python imports that
automatically at interpreter startup, and it patches
`behave.runner.ModelRunner.run_hook` in place. It no-ops unless
`AITLC_INSTRUMENT=1`, because that file lands on the path of *every* child
process, including ones with no behave installed.

Your project is never modified by any of these paths.

> **Migrating from a hook block?** If your own hooks already carry a
> pause-on-failure block, it is now redundant — but harmless. aitlc's runner
> gates on its own variable (`AITLC_PAUSE_ON_FAILURE`), so the two can never
> both fire. Delete the block when convenient.

---

## Running tests

### `aitlc run <TEST-ID>`

Run one feature file and report structured results.

```bash
aitlc run PROJ-24026
aitlc run PROJ-24026 --dry-run          # steps resolve, no browser
aitlc run PROJ-25466:47                 # one Examples row (see below)
aitlc run PROJ-24026 --debug            # halt on failure, browser stays open
aitlc run PROJ-24026 --retry 2 --retry-only-if-known-flake
```

Bare test IDs resolve recursively, so you never need the full path, and you
never need to tag other features to narrow a run.

**`FILE:LINE` selects a scenario, not a resume point.** behave runs *the
scenario containing that line, from its first step*. For a single-scenario
file that is the whole file. Where it genuinely pays off is a **Scenario
Outline** — pointing at one Examples row runs only that row (measured: 37
passed / 37 untested instead of 74).

*Agent use:* exit code is 0/1; stdout is one JSON object with
`steps_by_status` and a `failures[]` array carrying `step` and `error`. Add
`--toon` for a more compact table. Every run is appended to history
automatically.

### `aitlc parallel run [IDS...]`

Run many features concurrently — the `paver run parallel` workflow without
editing tags.

```bash
aitlc parallel run                      # everything discovered
aitlc parallel run PROJ-1 PROJ-2 -j 4
aitlc parallel run --list               # preview selection, run nothing
aitlc parallel run --debug --isolated -j 4
```

Skip tags already in your files are honored, and skipped files are **reported
with the reason** rather than silently dropped — "skipped by tag" must never
look identical to "never discovered".

### `aitlc parallel focus`

Pin what the bare command runs, so you stop typing filenames.

```bash
aitlc parallel focus PROJ-24026 PROJ-25931   # set once
aitlc parallel run                            # then just run
aitlc parallel focus --clear
```

This replaces the "tag every other file, then revert" habit. The selection
lives under `reports/`, so nothing in the repo changes and nothing can be
committed by accident.

---

## Debugging live

### `aitlc cdp launch|status|list|stop`

Own a long-lived debug Chrome that outlives the shell that started it.

```bash
aitlc cdp launch            # detached, mobile-sized by default
aitlc cdp status            # is it actually answering?
aitlc cdp list              # every tracked instance, alive or dead
aitlc cdp launch --new      # isolated: own port + own profile
aitlc cdp stop --all
```

Three traps this closes, each of which produced a misleading error before:

- Backgrounding Chrome from a shell ties it to that shell; when the shell
  exits, the next attach fails with a bare `ECONNREFUSED`.
- Liveness is the **port**, not the PID — `list` reports tracked-but-dead
  instances as `"running": false` instead of hiding them.
- `stop` verifies the recorded PID is still *our* Chrome before signalling, so
  a stale state file cannot kill an unrelated process group.

Use `--new` when several browsers must run at once (parallel suites, or
multiple agents). Separate *profiles* matter, not just ports: a shared profile
directory corrupts under concurrent Chromes and leaks cookies between tests.

### The persistent step console

`debug start` also starts a long-lived step console, and `retry` / `next` use
it instead of spawning a process each time.

```bash
aitlc debug console PROJ-1          # is one running?
aitlc debug console PROJ-1 --stop   # shut it down
```

This is a correctness feature before it is a speed one. A process per step
re-imports the step registry and re-runs scenario setup, but the real damage
is that **run-scoped data is regenerated per process** — generated names, ids,
emails. A step waiting for something an earlier step created then polls
forever for a name that never existed, which looks exactly like the
application hanging.

If the console is not running, `retry` and `next` fall back to spawning a
process, so a missing console is slow, never broken.

### `aitlc steps run <ID> --range A-B`

Resume a scenario partway through, in an already-open browser. This replaces
commenting out the steps that already passed.

```bash
aitlc cdp launch
# setup + login once (~2 min)
aitlc steps run PROJ-24026 --range 6-13 --cdp-url http://127.0.0.1:9333 --mobile "Galaxy S8"
# iterate on later steps in the SAME session — seconds per cycle
aitlc steps run PROJ-24026 --range 14-19 --cdp-url http://127.0.0.1:9333 \
                --mobile "Galaxy S8" --scenario-setup none
```

`--range 31-` (open-ended) means "line 31 to the end".

**Use `--scenario-setup none` on every slice after the first.** Setup mints
fresh per-scenario data; re-running it on a resume invalidates the session the
earlier slice established.

**A step slice gets no `before_scenario`.** That is where most suites generate
per-scenario data. aitlc invokes your real hook via `[project].scenario_setup`
and **stops immediately** if it fails, rather than running on into a failure
that surfaces several steps later looking like an app bug.

*Agent use:* emits JSON-lines — one object per step with `status`,
`duration_s`, `error`, plus a `scenario_setup` record showing what setup
actually produced.

---

## Reading a page cheaply

### `aitlc cdp inspect --a11y`

Read the live page as structured text instead of pixels.

```bash
aitlc cdp inspect --cdp-url http://127.0.0.1:9333 --a11y
aitlc cdp inspect --cdp-url ... --a11y --a11y-query "Apply filters"
aitlc cdp inspect --cdp-url ... --a11y --a11y-selector "#panel"
aitlc cdp inspect --cdp-url ... --check "#saveBtn,//button[text()='Close']"
```

Measured on one real page:

| Form | Size | Assertable? |
|---|---|---|
| Screenshot PNG | ~55 KB | needs vision |
| Full a11y tree | 1,961 chars | yes, text |
| Targeted query | 20 chars | yes, text |

The tree also carries what a screenshot cannot express: nesting, control state
(`[expanded]`), and field values (`textbox "Search filters": City`).

Built on `page.aria_snapshot()` (YAML). `page.accessibility` was deprecated for
three years and then removed, so aitlc falls back to the CDP `Accessibility`
domain only on Playwright versions predating `aria_snapshot`.

*Agent use:* this is the cheapest way to answer "is X on screen". Start with
`--a11y-query`; the response reports `chars` and `full_chars`, so you can see
what the query saved and tighten it. Reach for a screenshot only when the
question is genuinely visual — layout, overlap, styling.

---

## Suite health

### `aitlc steps unused`

Report step definitions that no feature file uses. behave has no equivalent of
Cucumber's unused-step report.

```bash
aitlc steps unused
aitlc steps unused --no-include-composite   # reproduce Cucumber's false positives
```

Matching goes through behave's **own registry**, so the answer agrees with what
the runner would dispatch — a regex reimplementation would disagree on exactly
the tricky cases. Steps invoked via `context.execute_steps(...)` are extracted
from the AST and counted as used.

> **Check the corpus before believing the number.** The result is only as
> complete as the feature files it can see. Where canonical Gherkin lives in a
> test manager rather than the repo, most steps look dead — measured here, 51
> local feature files reported 83% of definitions as unused. The command warns
> when the ratio is implausible. Treat that as "incomplete corpus", not
> "delete these".

### `aitlc history show`

Flake rate from observed outcomes, not hand-written signatures.

```bash
aitlc history show --flaky-only
aitlc history show --last 200
aitlc history clear
```

`aitlc run` appends every outcome automatically, so a new flake is visible the
second time it happens.

**Flaky means it has both passed *and* failed.** A test that has only ever
failed is broken, and retrying it just spends time to reach the same answer —
the two are reported separately on purpose.

### `aitlc doctor`

Environment checks: credentials, tunnel health, browser availability.

---

## Xray sync

```bash
aitlc xray get-gherkin PROJ-24026
aitlc xray compare-gherkin PROJ-24026            # local vs live
aitlc xray update-gherkin PROJ-24026 --file body.txt
aitlc xray fetch-features PROJ-29026 --status FAILED
aitlc xray find-step-usage "click on audience tab"
```

**Xray stores only the step body** — no `Feature:` line, no tags, no
`Scenario:` header, no comments. `compare-gherkin` normalizes your local file
before diffing, so it compares like for like. `update-gherkin` re-fetches after
writing to confirm the write actually persisted; the mutation echoing your
input back is not proof.

`fetch-features` is the "everything that failed last night" entry point:
it resolves a whole Test Execution or Plan into runnable `.feature` files.

---

## Evidence

```bash
aitlc trace extract-frame trace.zip      # last frame as an image (cheap tier)
aitlc trace show trace.zip               # full interactive viewer (expensive tier)
aitlc s3 report-summary                  # counts + failures, no download
aitlc report <TEST-ID>                   # run + capture a replayable terminal recording
```

Escalate in that order — a single frame explains most failures, and the
interactive viewer costs far more to open and read.

---

## What happened in CI

Three questions, three commands, none of which need you to know which suite
report a test lives in.

```bash
aitlc s3 find-test PROJ-1                 # which plan and run, no download
aitlc s3 verify-test PROJ-1 PROJ-2        # pass/fail + the failing step
aitlc s3 history PROJ-1 --days 14         # chronic or intermittent?
```

`verify-test` reads the per-test Behave JSON rather than the HTML report:
orders of magnitude smaller, already structured, and it carries the scenario
tags that make a nested test key findable at all.

**A test key is usually not an execution key.** A plan runs one feature file
per execution key, and the tests inside carry their own `@TEST_<KEY>` scenario
tags. Searching object names for such a key finds nothing, which reads exactly
like "it did not run" — so `find-test` reports `not_named_by_any_object` and
points at `verify-test`, which reads the documents.

`history` answers the question that decides what to do next:

```
test         08-11 08-12 08-14 08-15   rate   verdict
PROJ-1       FAIL  FAIL  FAIL  FAIL    4/4    deterministic
PROJ-2       FAIL  PASS  .     FAIL    2/3    intermittent
```

Deterministic means one signature every time — reproduce it, a single run will
show it. Intermittent means the signatures vary — establish a base rate before
bisecting anything. Failures are grouped by signature with volatile parts
(timings, generated ids) masked, so one defect does not look like five. A run
where *every* test failed is labelled an outage and excluded from the rates
rather than inflating them. Everything is written to
`<workspace>/.aitlc/test-history.json` so the next reader does not re-download
it.

Scoped by **day**, not by run: a suite executes many times a day, and a run
count collapses the matrix into a single column.

### Credentials

```toml
[s3]
profile = "my-sso-profile"   # resolved fresh on every call
```

Static keys in a `.env` expire, and when they do `aws sso login` does *not*
fix it — the stale values in the file take precedence over the refreshed
profile. A named profile (or `AWS_PROFILE`) avoids that entirely.

## Escape hatches

aitlc's own commands cover the paths worth making easy. These two cover
everything else, so adopting aitlc never means losing a flag it does not wrap.

```bash
aitlc behave --aitlc-debug features/x.feature   # any behave args, forwarded
aitlc behave --print-command <args>             # show the command, run nothing
aitlc pw show-trace trace.zip
aitlc pw codegen https://example.com
aitlc pw install chromium
```

What they add over typing `behave` directly: your `.env` loaded first, the
right interpreter and working directory, and aitlc's optional instrumentation.
aitlc's own flags are prefixed `--aitlc-` so they can never collide with a
behave flag, now or in a future release.

*Agent use:* `--print-command` prints the exact invocation as JSON. Use it to
hand a reproduction to someone without aitlc, and to explain what you are about
to run before running it.

---

## Everything else

| Command | Purpose |
|---|---|
| `aitlc start` | Bootstrap briefing for a fresh agent or engineer |
| `aitlc classify-failure` | Match a failure against `patterns.yaml` |
| `aitlc propose-fix` | Assemble the evidence needed to propose a fix |
| `aitlc record --suggest-steps` | Record a session, diff selectors against existing locators |
| `aitlc notify-teams` | Post a run summary to a Teams webhook |
| `aitlc jira create-task` | Create a Jira Task |
| `aitlc tunnel status\|restart` | LambdaTest tunnel health |
| `aitlc users validate\|generate` | Pooled test-user maintenance (requires `--yes`) |

`aitlc users` commands act on a **shared** pool and refuse to run without
`--yes`; the underlying scripts have no prompt and no dry run.

---

## Troubleshooting

**`--debug` did nothing.** Check the `instrumentation` line printed to stderr.
If it says `sitecustomize`, aitlc could not use behave's runner option — the
`detail` field distinguishes "this build has no such option" from "behave could
not be probed at all" (usually a missing virtualenv).

**Instrumentation silently ignored.** Instrumentation flags must precede the
positional feature path; behave's positional argument ends option parsing, so
anything after it is treated as another path.

**`No module named 'aitlc.runtime.runner:AitlcRunner'`.** A path-format
mismatch, not a missing install: behave 1.2.7 needs the dotted form. aitlc picks
the right shape per detected flag; seeing this means the flag was overridden by
hand.

**A step slice fails on data-dependent steps.** `[project].scenario_setup` is
unset, so no per-scenario data exists. The `scenario_setup` record in the output
says `skipped` when this happens.

**`steps unused` reports most of the suite.** Your feature corpus is
incomplete — see the warning in the output before deleting anything.
