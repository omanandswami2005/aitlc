# Roadmap — `aitlc`

What is built, what is still open, and what is deliberately not being built.
For how to use any of it, see [`USER-GUIDE.md`](USER-GUIDE.md).

**Tracker: 31 of 44 closed, 13 open.** Every row below came
from a real debugging session that the tool made harder than it needed to be;
none of them are speculative features.

## Open

Ordered by how much a real investigation pays for them.

| # | Gap | Notes |
|---|---|---|
| G3 | `init` has no merge mode | Re-running it overwrites a hand-edited `aitlc.toml`. Needs `--merge`. |
| G6 | no `--version` | `aitlc --version` still errors. One callback; trivial and still missing. |
| G9 | no way to read page state or call a project function | Biggest remaining hole for interactive debugging. |
| G10 | `parallel run` streams every child's transcript to stdout | Output is summarised but not separable per child. |
| G15 | the skip-tag pre-filter is narrower than the project's rule |  |
| G27 | `triage-run` truncates the call log to its least useful line | The `locator resolved to ...` line is the highest-value one and is still dropped. |
| G31 | no way to use an AWS SSO profile | Static keys in `.env` are still the only path; an expired token has to be exported by hand. |
| G36 | every `next`/`retry` pays full process startup | **Next up.** See *Persistent step console* below. |
| G37 | per-step processes regenerate run-scoped data | Same root cause as G36; stateful tails can never pass until it is fixed. |
| G38 | no wall-clock stamps, and no way to time an app condition | Durations exist; absolute timestamps do not. |
| G39 | a local run silently differs from the CI run of the same feature | Setup takes a different branch and nothing logs the switch. |
| G40 | parallel results are not separable; attribution rests on a re-run |  |
| G42 | the debug cycle cannot survive an expensive scenario | Sessions are not resumable across a restart. |

### Persistent step console (G36 + G37)

The single biggest correctness *and* speed problem left, and one root cause for
both. Every `debug next` / `debug retry` spawns a fresh interpreter that
re-imports the step registry, re-runs scenario setup, and reconnects to the
browser. That is slow, and worse, it is **wrong**: run-scoped data (generated
names, ids, emails) is regenerated per process, so a step that waits for
something an earlier step created polls forever for a name that never existed.
It looks exactly like the application hanging.

Planned shape, in the same style as `cdp launch`:

1. `debug start` also launches a **detached, long-lived step console** holding
   the imported registry, the behave context, and the CDP connection.
2. `retry` / `next` talk to it over newline-delimited JSON on a local socket,
   recorded in the session state file next to the browser's.
3. `debug stop` shuts it down; an idle timeout means it cannot leak.
4. **If the console is unreachable, fall back to today's per-step subprocess.**
   No regression: it degrades to correct-but-slow rather than failing.

Fixing the process boundary is what makes stateful tails runnable at all, so
G37 closes with it rather than separately.

## Closed

| # | Gap | Resolution |
|---|---|---|
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
