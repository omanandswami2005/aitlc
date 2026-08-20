# How deeply aitlc attaches to a suite

Written after driving a real suite end to end and finding that most of what
looked like tool bugs were places where aitlc *approximated* the suite instead
of using it. The rule that came out of it:

> Never re-implement something the suite already does. Load the suite's own
> code and call it.

Everything below is stated as what is wired today, what is deliberately not,
and what it cost to learn.

## The layers, innermost first

| Layer | Who owns it | How aitlc reaches it |
|---|---|---|
| Browser process | aitlc | `cdp launch` / `debug start` — detached, isolated profile |
| Browser attach | the suite | `PLAYWRIGHT_CDP_URL`, read by the suite's own platform layer |
| Environment / hooks | the suite | behave `Runner.load_hooks()` on its real `environment.py` |
| Step definitions | the suite | behave `Runner.load_step_definitions()` |
| Step dispatch | behave | `registry.find_match(step).run(context)` |
| Per-step hooks | the suite | `run_hook("before_step"/"after_step", context, step)` |
| Evidence | the suite | whatever `after_step` already produces |

The only layer aitlc owns outright is the browser process. Everything above it
belongs to the suite, and aitlc's job is to reach it, not to replace it.

## What changed, and why it mattered

**The debug engine is a paused real behave run, not a reconstruction.** An
earlier version stepped a hand-built console that called one configured function
as a stand-in for `environment.py`, and every gap it hit (Examples placeholders
run literally, run-scoped data regenerated per step, data tables dropped) was a
place where the reconstruction diverged from behave. That whole approach was
removed. `debug` now drives behave's own `Runner`: it runs `before_all` /
`before_feature` / `before_scenario` and the setup steps for real, parks at the
target step, and advances/re-runs the actual behave `Step` objects. Context
layers, tag handling, Examples binding, data tables and per-step hooks are
behave's, because it *is* behave — merely paused. There is nothing left to
diverge, and no fallback engine to keep in step.

**The browser is handed over, not duplicated.** Setting `PLAYWRIGHT_CDP_URL`
before the hooks run means a suite that supports CDP attach connects to the
debug browser itself. aitlc stops owning a second browser it would then have
to keep in step with the suite's.

**Per-step hooks carry the evidence.** `after_step` is where a suite captures
its failure screenshot and its API traffic. Skipping it threw away exactly the
material that makes a failure explicable and left the person to reproduce it
again to obtain the same information. `step.status` is set before the hook
runs, because that is what the hook reads to decide whether to capture — and
it is set to behave's own `Status` enum, since a hook comparing against
`Status.failed` would silently never match a string.

## What is deliberately not wired

- **`after_scenario` / `after_all`.** They tear down what a debug session
  exists to keep. They run on `debug stop`, not between steps.
- **The suite's own browser creation, when its hooks decline to build one.**
  A feature can be skipped by a tag rule, or the platform branch can be gated
  on feature-level tags that an export left on the scenario. aitlc reports
  `hooks_provided_browser: false` and supplies its own handle, rather than
  failing several steps later with an error about a missing attribute, which
  reads as a broken suite.

## The trap this surfaces in real suites

Hooks read **feature**-level tags. An export from an issue tracker puts the
issue's labels on the **scenario**. The file then looks correctly tagged and
gets none of the setup those tags select — confirmed on a real feature whose
`skip_login` sat on the scenario while its feature tags were empty.

`aitlc preflight` reports this before anything is launched. It is the cheapest
check in the tool and it explains a whole class of "it behaves differently
locally".

## Cost, measured

| Path | Per step |
|---|---|
| Socket round-trip, no step | 0.1–0.2 ms |
| A one-second step | 1.00 s wall, 1.0 s reported |
| Same step, process-per-step (old) | +6 s |

The gate adds no measurable dispatch cost: `next`/`retry` are a socket
round-trip into a process that is already warm and holds the live Context and
browser. What remains is the CLI's own start (~0.6 s per invocation) and the
step's real work. A step that fails spends its own timeout, which no
architecture can shorten.
