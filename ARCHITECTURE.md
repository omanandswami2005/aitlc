# Architecture — `aitlc`

How the package is organised and why. For what each command does, read
[`USER-GUIDE.md`](USER-GUIDE.md); this file is for people changing the code.

## 1. Core vs. adapters

One rule shapes the whole package: **core code knows nothing about any
particular project, or about Xray, or about any test-execution vendor.**
Everything project-specific arrives through `aitlc.toml`.

| Layer | Project-aware? | Contents |
|---|---|---|
| **Core** | No | Behave→JSON wrapping, CDP attach/freeze, the single-stepping gate and the step-slice console (`steps run`/`call`), terminal replay, TOON serialization, concurrency locking, pattern matching, AST-based locator and codegen scanning, diff blast-radius checking |
| **Adapters** | Yes | Xray GraphQL client, tunnel and remote-queue management, Jira task creation, S3 evidence fetching, Teams webhook posting |

This is a hard requirement rather than a style preference, and it earns its
keep twice. It is what lets a project adopt one command without adopting the
tool's whole worldview — a suite with no Xray and no remote grid never loads
those adapters, because adapters are imported when their command runs, not
eagerly at startup. And it is why the package could be published at all: the
core had nothing project-specific to scrub.

The rule bites in practice. An adapter must not reach into a host project's
own config or client objects, however convenient — `adapters/teams/webhook.py`
is a from-scratch Adaptive Card poster for exactly that reason.

## 2. Package layout

```
aitlc/
  pyproject.toml              # [project.scripts] aitlc = "aitlc.cli:app"
  aitlc.toml.example          # annotated config template
  src/aitlc/
    cli.py                    # thin composition root; commands self-register (§3)
    config.py                 # aitlc.toml loader and schema
    runtime/
      runner.py               # behave Runner subclass — instrumentation + the single-stepping gate
      attach.py               # picks how to attach it (see §5)
    core/
      behave_runner.py        # subprocess wrapping, JSON/TOON output
      feature_select.py       # bare-ID and FILE:LINE resolution, tag filters
      gate_client.py          # client for the gated runner's control socket (debug)
      step_console.py         # run a slice of steps against a live browser (steps run / call)
      chrome_cdp.py           # launch/probe/stop persistent debug browsers
      cdp_attach.py           # attach to a live page; accessibility snapshots
      focus.py                # persisted feature selection
      history.py              # per-run outcome log; flake vs. broken
      unused_steps.py         # dead step-definition detection
      init_config.py          # repo inspection behind `aitlc init`
      patterns.py             # match a failure against a pattern library
      terminal_replay.py      # pyte-based, redraw-correct terminal replay
      trace_evidence.py       # frame extraction from a Playwright trace
      report_summary.py       # large HTML report -> compact JSON
      locator_scan.py         # AST scan of a project's locator definitions
      codegen.py              # AST extraction from codegen output
      blast_radius.py         # diff -> shared-file impact check
      locks.py                # per-test lockfile
      toon.py                 # compact serialization for uniform tables
      dotenv.py               # .env loading
      redact.py               # secret redaction on every output path
      live_status.py          # stdlib+behave only; copied into the target env
      playbook.py             # embedded debugging-discipline text
      script_runner.py        # guarded execution of project-owned scripts
    adapters/
      xray/client.py          # Gherkin read/write/compare, Test navigation
      xray/gherkin_normalize.py
      lambdatest/tunnel.py    # tunnel status and restart
      lambdatest/queue.py     # concurrency-aware remote queueing
      jira/tasks.py           # plain Jira issue creation
      s3/evidence.py          # generic object find/download
      teams/webhook.py        # generic Adaptive Card build and post
    commands/                 # CLI wiring only — no business logic
      _registry.py            # command discovery + mounting (§3)
  tests/                      # required, not optional
```

`commands/` stays thin on purpose. A command parses flags, calls one core or
adapter function, and prints. Logic that lives there cannot be tested without
going through Typer, and cannot be reused by another command.

`core/live_status.py` is the one file with an unusual constraint: it is never
imported by aitlc: it is copied as *text* into the target project's own
interpreter, which is why it may import nothing beyond the standard library
and behave.

## 3. CLI framework

**Typer**, which derives arguments and options from type-hinted function
signatures, so each command's parameters double as its documented interface
and `--help` is generated rather than maintained.

One gotcha worth writing down: a single, non-grouped command must be mounted
with `app.command("run")(fn)`, never wrapped in its own `typer.Typer()` sub-app
— the latter silently produces `aitlc run run ARGS`. Only commands that
genuinely host subcommands (`xray`, `cdp`, `steps`, `parallel`, `tunnel`,
`jira`, `trace`, `s3`, `history`, `journal`, `locators`, `debug`) mount as a
sub-Typer.

**Commands self-register; `cli.py` never lists them.** Each command module
declares a module-level `COMMAND` dict — `{"name", "attr", "kind", "order"}` —
and `commands/_registry.py` discovers every module in the package, reads those
declarations, and mounts them in `order` (`register_all(app)`). Adding a command
is dropping one file with one `COMMAND` line; `cli.py` stays a thin composition
root that owns only the global options (`--version`, `--workspace`). This also
records mount *intent* next to each command, which matters because three modules
(`init`, `start`, `propose-fix`) own a Typer `app` yet are mounted as a plain
command — a fact that used to live only in `cli.py`. The root escape-hatch group
(`aitlc behave` / `pw` / `paver`) mounts with an empty name.

## 4. Config file — `aitlc.toml`

`aitlc init` writes it by inspecting the repo; `aitlc.toml.example` documents
every key. aitlc searches upward from the working directory, so one file at a
repo root covers every subdirectory.

Two properties matter more than the schema itself.

**No secret value is ever stored.** `[env]` maps aitlc's generic credential
names to the *names* of the environment variables a project already uses.
aitlc learns which variable to read, never what is in it.

**Undetected is not the same as guessed.** `init` writes anything it cannot
identify as a commented placeholder. A wrong value written confidently fails
later, somewhere unrelated, and costs far more than a blank.

## 4a. Where output goes — `core/workspace.py`

Every artifact path resolves through one module. Commands never build an
output path themselves, which is what makes `--workspace` total rather than
something each command has to remember to honour.

```
workspace.output_path(root_dir, ".aitlc", "runs")
```

Resolution, most specific first: `--workspace`, `AITLC_WORKSPACE`,
`[project].workspace`, then `reports/`. The value is process-global state set
once at CLI start — deliberately, because core helpers receive a `root_dir`
and not a config object, and threading config through nineteen call sites to
avoid one module-level value would be worse.

The name must be relative to the project root. Absoluteness is checked on the
raw string before any trimming: stripping a leading slash first turns
`-w /etc` into `<project>/etc`, writing somewhere the caller never asked for
rather than refusing.

One exception, and it is an OS limit rather than a choice: the control sockets
(the debug gate's, and the step-slice console's) live in the system temp
directory, keyed by a digest of project root and test id. A socket path is
capped near 104 bytes by the kernel, and a path under a deeply nested project
exceeds it and fails at `bind()`.

## 4b. The debug engine: a paused real behave run

`debug` does not reconstruct behave's run loop; it drives a real, paused one.
`runtime/runner.py` (in gate mode) lets behave run `before_all` /
`before_scenario` and the setup steps `[0, --at)` through its own loop, then
**takes over** at the park step: it binds a Unix socket and advances or re-runs
the actual behave `Step` objects on request (`core/gate_client.py` is the
aitlc-side client). Because both the setup and the stepping go through behave's
own machinery, there is nothing to diverge from it — Examples binding, data
tables, docstrings, run-scoped data and the project's own hooks are all
behave's, not an approximation.

This is why there is no separate "step console" for debugging and no fallback
engine: one path, the gate. (`core/step_console.py` remains, but only as the
backend for `steps run` and `call`, which run a slice against a live browser —
a different feature, not a debug fallback.)

A Gherkin edit is picked up without a restart: before each `next`/`retry` the
gate re-parses the feature (behave's own `parse_file` + `walk_scenarios`, so the
Examples row is bound by behave) and swaps in the fresh steps, following the
cursor by text. The re-parse is gated on the feature's mtime, so a step with no
edit pays only a `stat`; the debugging loop is mostly Gherkin edits, so this is
the case that had to stay cheap.

The browser is handed over through the suite's CDP env var (default
`PLAYWRIGHT_CDP_URL`, named in `[project].playwright_cdp_env`) before the hooks
run, so a suite that supports CDP attach connects to the debug browser itself.
aitlc owns the browser *process*; everything above it belongs to the suite. The
gate hard-exits on `stop`, so the browser survives for a final inspection.

A failed **setup** step means behave (`--stop`) aborts before the gate can park;
aitlc reports that the run did not reach the park point rather than presenting a
clean park on top of a broken precondition. `after_scenario` and `after_all` are
never reached between steps — they tear down the state a debug session exists to
hold.

See INTEGRATION.md for the layer-by-layer contract.

## 5. Attaching without editing the target project

A debugging tool that requires editing the suite it debugs cannot be adopted
incrementally, cannot be uninstalled cleanly, and silently becomes a no-op for
anyone who installs the tool but not the edit. aitlc therefore attaches in
three layers, in `runtime/attach.py`:

1. **behave's own runner option.** `runtime/runner.py` subclasses behave's
   `Runner` and overrides `run_hook`, calling `super()` first so every hook
   the project defines still runs, unchanged and in order. The same subclass
   serves two opt-in modes, both env-driven so an un-instrumented run is
   byte-for-byte behave's own: **observe** (`AITLC_PAUSE_ON_FAILURE` — halt on
   a failed step before teardown, what `run --debug` uses) and **gate**
   (`AITLC_GATE` — park and single-step, what `debug` uses, §4b).
2. **Version probing.** The option's flag name *and* its class-path format
   differ between behave releases. aitlc asks `behave --help` what it
   supports instead of parsing a version string, because forks and vendored
   builds do not follow upstream numbering.

   | behave | Flag | Class path |
   |---|---|---|
   | 1.2.7.dev | `--runner-class` | `pkg.module.Class` |
   | 1.3+ | `--runner` / `-r` | `pkg.module:Class` |
   | 1.2.6 | neither | falls through to (3) |

3. **A universal fallback.** Where no such option exists, aitlc writes a
   `sitecustomize.py` onto `PYTHONPATH`; Python imports it at interpreter
   startup, and it patches `ModelRunner.run_hook` in place. It no-ops unless
   `AITLC_INSTRUMENT=1`, because that file lands on the path of every child
   process the run spawns.

Which layer was used is reported, never assumed. A fallback that quietly
substitutes for the real mechanism is how "the tool did nothing" gets
mistaken for "the tool found nothing".

## 6. Known-flake pattern library

`aitlc classify-failure` matches a failure against a YAML library of known
signatures, resolved from the *target project's* config root — the library
describes one suite's recurring failures, so it belongs with that suite, not
in this package.

```yaml
patterns:
  - id: click-interception-reflow
    match:
      error_contains: ["intercepts pointer events", "element is not stable"]
    description: "Page reflow intercepts the click target"
    suggested_action: "force the click; this is not a wait problem"
```

Matching is first-match, not best-match: entries are written from specific,
observed error strings, so the most specific one is listed first and a broad
catch-all pattern is a bug rather than a convenience.

Signature matching only ever covers failures somebody already described, which
is why `core/history.py` exists alongside it — recording every run's outcome
surfaces a *new* flake the second time it happens. A test that has only ever
failed is reported as broken, not flaky; retrying it spends time to reach the
same answer.

## 7. Distribution

Published to PyPI under two distribution names, `aitlc` and `dax-aitlc`, built
by `scripts/build_dual_name.py`. Both install identical code and both provide
the `aitlc` command; only the name on the index differs. See README's
*Releasing* section.

The sdist uses an explicit `include` allowlist rather than an exclude list, so
it fails closed: a new internal file stays out by default instead of shipping
until somebody remembers to exclude it. `tests/test_packaging.py` pins that.

Docker was considered and dropped. An installed mode and a zero-install mode
already cover the ground; a container would add packaging surface without
solving a problem either one leaves open.

### Publishing

```bash
.venv/bin/pip install -e ".[publish]"
python scripts/build_dual_name.py     # both names, into dist/
python -m twine check dist/*
python -m twine upload --repository testpypi dist/*
python -m twine upload dist/*
```

`build_dual_name.py` rewrites the distribution name, builds, and restores
`pyproject.toml` in a `finally` — a failed build never leaves the repo holding
a name nobody chose.

Install from TestPyPI into a scratch environment and run `aitlc init` against a
real project before the last line. That is the step that catches a broken
config template or a missing dependency; nothing local will.

Two things about the final upload are irreversible: a version number can never
be reused, and deleting a project does not release its name. Check the
`[project.urls]` values first — they are baked into a version's metadata and
cannot be edited afterwards.

## 8. Non-goals

- **No MCP server.** A CLI is cheaper in tokens and works in more places.
- **No auto-commit, anywhere.** `propose-fix` writes a Markdown proposal and
  an evidence bundle. Nothing in this package writes to git or to a tracker
  without a separate, explicit call outside its own control flow.
- **No hidden success.** Missing setup, an incomplete corpus, or a fallback
  mechanism are each reported explicitly. Silence surfaces later as an
  unrelated-looking failure, which is the expensive kind.
