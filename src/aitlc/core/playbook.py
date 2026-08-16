"""The bundled, generic debugging playbook.

Ships inside the package (a plain Python string, not a data file — that
way it's guaranteed present in the wheel with zero extra packaging config)
so `aitlc start` can hand a fresh agent or person the same operating
discipline this tool was built out of, without depending on any one
project's own agent-instruction files. The mechanics that discipline
originally relied on are already commands here (`aitlc run`,
`aitlc steps run`); what is left is the judgment, which is genuinely
project-agnostic.
"""

PLAYBOOK_MD = """\
## Debugging playbook

Five rules, each traced to a real mistake made and caught while building
this tool — not abstract advice.

1. **Diagnose before you retry.** A failure that "might just be flaky" and
   a failure that's a real, reproducible bug produce identical-looking
   symptoms on the first attempt. Read the actual error text
   (`aitlc classify-failure`) before deciding which one you're looking at.
   Blind retrying burns time and can paper over a real regression.

2. **Minimal-change-first — reuse before you invent.** Before writing new
   code: (a) does an existing step/function already do this? Reuse it.
   (b) Is the smallest fix a data/config change (a Gherkin edit, an env
   var), not a code change? (c) Only touch shared code as a last resort,
   scoped as narrowly as possible, confirmed via search to affect only
   what you intend. An agent that defaults to "write new code" accumulates
   near-duplicate logic and silently grows the blast radius of every fix.

3. **Require a positive signal, not the absence of a negative one.** "The
   error went away" is not the same as "the fix is correct." Assert on
   something that only exists in the genuinely-working state. A retry that
   happens to pass doesn't confirm anything about *why* it passed.

4. **Check your check.** A verification that passes before your change is
   worthless — you don't know if it was ever capable of failing. Confirm
   it fails against a deliberately broken input first, then confirm your
   real fix flips it. This catches checks that are quietly checking the
   wrong thing.

5. **Close with one real, clean run — the fast inner loop is not the
   proof.** Live consoles, CDP-attach, and structured step-by-step replay
   (`aitlc steps run`) are for iterating cheaply while you're still finding
   the fix. None of that certifies anything on its own — it starts from
   whatever state you left the browser in, and shortcuts like fast-login
   skip the real user journey entirely. Finish with one full, real run
   (`aitlc run <test-id>`) and quote *that* result, not the console's.

**On known-flake vs. new bug:** not every failure is a bug to fix. Some
are genuine backend/infra non-determinism (a timing-dependent job not
finished yet, a degraded tunnel connection) that no test-code change can
fix. `aitlc classify-failure` exists to make that distinction explicit and
consistent instead of a fresh judgment call every time — trust a matched
pattern's suggested action, and treat an unmatched failure as the signal
to actually investigate, not to guess.
"""
