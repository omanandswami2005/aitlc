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
