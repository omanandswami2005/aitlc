# aitlc

A CLI for debugging Behave + Playwright test suites and keeping them in sync
with Xray. Structured JSON output first, so a person and an agent can read the
same result.

**It never asks you to edit the suite it debugs.** No hook blocks, no
`environment.py` changes, nothing to keep in sync — instrumentation attaches
through behave's own runner API, with a fallback for versions that lack it.

```bash
uv tool install aitlc      # or: pipx install aitlc
cd /path/to/your/project
aitlc init                 # detects your layout, writes aitlc.toml
aitlc run PROJ-1234
```

The command is always `aitlc`. It is published under two names — `aitlc` and
`dax-aitlc` — which install the same tool; use whichever your org prefers.

## What it does

**Keep one investigation in one directory.** Point a workspace at what you are
working on and every artifact from every command lands there — traces, cached
CI reports, session state, browser profiles, logs.

```bash
aitlc --workspace PROJ-29019 debug start PROJ-29019
```

Switch the name and the previous investigation stays beside it; delete the
directory and it all goes together. Unset, everything stays under `reports/`.

**Run and target tests.** Bare test IDs resolve recursively, so you never need
a full path, and never need to tag other features to narrow a run. One Examples
row of a Scenario Outline can be run on its own.

```bash
aitlc run PROJ-1234                 # structured JSON result
aitlc run PROJ-1234 --debug         # halt on failure, browser stays open
aitlc parallel run -j 4             # concurrent, without editing tags
aitlc parallel focus PROJ-1234      # pin a selection, then just `aitlc parallel run`
```

**Debug a failure end to end.** A session holds one isolated browser and your
position in the scenario, so fixing a step costs a re-run of *that step* rather
than the whole scenario — and a scenario with four defects costs one setup
instead of four.

```bash
aitlc s3 triage-run --suite <plan>       # what CI actually failed on
aitlc debug start PROJ-1234 --at 12      # isolated browser, driven to the step
aitlc debug retry PROJ-1234              # edit -> re-run that step -> repeat
aitlc debug next PROJ-1234               # forward, from the state you have
aitlc debug certify PROJ-1234 --times 2  # fresh instance, real feature, twice
```

`retry` and `next` run in a persistent step console rather than a new process
each time. That is a correctness feature first: a process per step regenerates
run-scoped data — generated names, ids, emails — so a step waiting on
something an earlier step created polls forever for a name that never existed,
which looks exactly like the application hanging. Without a console they fall
back to spawning a process, so it is slow, never broken.

**Find out what CI did, without guessing which report to open.**

```bash
aitlc s3 verify-test PROJ-1 PROJ-2   # pass/fail + the failing step and error
aitlc s3 history PROJ-1 --days 14    # chronic, intermittent, or an outage day
```

A test key is usually a scenario tag inside a differently-named file, not an
execution key, so a filename-only lookup reports "did not run" for a test that
ran. `history` groups failures by signature: one signature every time means
reproduce it, varying signatures mean establish a base rate before bisecting.

`certify` is deliberately separate and never uses the debug browser: a
CDP-attached browser reuses an existing context, so it is never proof. Two
consecutive passes are the default because one pass does not disprove a race.

**Read back what already happened.** Every run is recorded, and fetched reports
are cached, so a follow-up question is a file read rather than another run.

```bash
aitlc journal list --last 5
aitlc journal diff <earlier> <later>     # did the fix work, or was that luck?
```

Payloads are redacted before they touch disk, size-capped and pruned.

**Check locator hygiene.** `aitlc locators lint` flags selectors that pass while
reading the wrong element — positional row indices, grid cells with no
`role='cell'` guard (a header carries `data-field` too), unanchored `//*`
xpaths — each with the rewrite attached, not just the diagnosis.

**Debug live.** Keep one browser across many iterations instead of paying setup
and login on every change. `aitlc steps run --range 14-19` resumes a scenario
partway through in an already-open browser — replacing the habit of commenting
out the steps that already passed.

```bash
aitlc cdp launch                    # detached; survives the shell that started it
aitlc cdp launch --new              # isolated: own port + own profile
aitlc steps run PROJ-1234 --range 14-19 --cdp-url http://127.0.0.1:9333
```

**Read a page as text, not pixels.** The accessibility tree answers "is X on
screen" as assertable text, and carries nesting, control state and field values
a screenshot cannot express. Measured on one real page: 55 KB screenshot →
1,961 characters for the full tree → 20 characters for a targeted query.

```bash
aitlc cdp inspect --cdp-url http://127.0.0.1:9333 --a11y --a11y-query "Save"
```

**Find dead step definitions.** behave has no equivalent of Cucumber's
unused-step report. Matching goes through behave's own registry, so the answer
agrees with what the runner would dispatch, and steps invoked via
`context.execute_steps(...)` count as used.

```bash
aitlc steps unused
```

**Track real flakiness.** Signature matching only ever covers flakes somebody
already described. `aitlc history` records every run outcome, so a new flake is
visible the second time it happens. A test that has only ever failed is
reported as broken rather than flaky — retrying it spends time to reach the
same answer.

**Sync with Xray.** Read, compare and write a Test's Gherkin; pull every
feature from a Test Execution or Plan; find where a step is really used.

**Escape hatches.** `aitlc behave` and `aitlc pw` run those tools directly with
your project's `.env` and interpreter already set up, so adopting aitlc never
means losing a flag it does not wrap. `--print-command` shows the exact
invocation without running it.

## Documentation

| File | For |
|---|---|
| `USER-GUIDE.md` | Full reference — written to be read directly by an agent |
| `user-guide.html` | The same guide as a browsable page |
| `aitlc.toml.example` | Annotated configuration template |

## Requirements

Python 3.10+. The suite under test keeps its own environment; aitlc runs
outside it and shells in, so the two never need to share dependencies.

## Design notes

Three rules the code holds to, each learned from a bug that cost real time:

- **Never report success for something that did not happen.** Missing setup, an
  incomplete feature corpus, or an instrumentation fallback are each reported
  explicitly, because silence surfaces later as an unrelated-looking failure.
- **Ask the tool, do not assume its version.** behave's custom-runner option
  changed both its name and its argument format across releases, so aitlc
  probes `behave --help` rather than parsing a version string.
- **Nothing project-specific in the code.** Layout, hook names and credential
  variable names all come from `aitlc.toml`, and `aitlc init` detects them.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest tests/ -q
```

## License

MIT — see `LICENSE`.
