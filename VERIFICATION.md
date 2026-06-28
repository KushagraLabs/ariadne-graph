# VERIFICATION.md — How to stop getting slop

> For the repo owner (non-coder) and any AI agent working here.
> This file OVERRIDES "it works" and "tests pass" as definitions of done.
> If an agent's change conflicts with this file, the agent is wrong.

## Why this file exists

This repo had a 33KB AGENTS.md and 292 passing tests, and still shipped a
**fuzzy name resolver pretending to be a real one**. More instructions did
not help. Passing tests did not help — because the tests checked "it runs,"
not "it's correct on the hard case."

The fix is not more planning. It is **proof, on the case designed to break it,
that you can watch without reading code.**

## The one rule

**No feature is "done" until there is a test that FAILS on the wrong/cheap
implementation and PASSES on the real one — and the owner has seen it go
red, then green, on the hard case.**

"Does it work?" → an agent always says yes.
"Show me the red-then-green on the hard case" → a fuzzy fake cannot fake this.

## What the owner does (no code reading required)

For every feature, ask the agent for exactly this, in order:

1. **"What is the hard case where a cheap version would look right but be wrong?"**
   Make the agent name it in plain English. If it can't, the feature isn't understood yet.

2. **"Write that as a test. Run it against the CURRENT code. Paste the output."**
   You want to see it **FAIL**. A failing test proves the test actually checks
   something real. (A test that passes immediately often tests nothing.)

3. **"Now make it pass. Paste the output showing red → green."**
   You watch the same test go from FAIL to PASS. You do not read the code.
   You read the two test outputs.

4. **"Did any other test break?"** Run the whole suite. Regressions are slop too.

If the agent skips to step 3, or says "it works, trust me," or makes the test
pass by weakening the test — that is the slop. Stop and call it out.

## The trap to watch for: tests that test nothing

The agent will be tempted to write a test that passes on the fuzzy version,
because that's the easy path to "green." Defense:

- The test MUST fail first (step 2). No red, no trust.
- The test MUST describe a *specific wrong answer* the cheap version gives,
  not just "returns something."
- Example of a USELESS test: "calling foo() creates a CALLS edge." (Fuzzy passes this.)
- Example of a REAL test: "when two files both define `save`, a call to the
  local `save` creates an edge to the LOCAL one, not the other file's." (Fuzzy FAILS this.)

## Per-feature acceptance template

Paste this to the agent for any non-trivial feature:

```
Feature: <name>
The hard case (plain English): <the case a cheap impl gets wrong>
The wrong answer a cheap impl gives: <specific>
The right answer: <specific>

Deliver in this order, pasting terminal output at each step:
1. A test encoding the hard case.
2. That test FAILING against current code.
3. The implementation.
4. That test PASSING + full suite still green.
Do not weaken the test to make it pass. Do not skip step 2.
```

## Scope discipline (the other half)

Slop also comes from doing too much. One feature = one hard case = one red→green.
If an agent's change touches 12 files for a 1-file feature, that's a smell —
ask why. Big diffs hide fake work; small diffs are auditable even by a non-coder.
