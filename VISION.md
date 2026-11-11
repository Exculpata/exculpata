# Exculpata vision

Exculpata is a free, local-first discovery review workspace for criminal defense teams. The goal is for real case teams to trust it on real cases.

## What it is
- **One assistant.** Attorneys prompt in plain words. The assistant decides for itself how to search, read, reason, calculate, cite, and flag what's missing, using the tools it's given.
- **It reasons across the discovery.** It doesn't just answer and link to a file. It pulls the pieces together and connects them, shows its work, and cites every piece.
  - The test case: "Did the client or any party ever own more than 10% of this stock?" A good answer finds each filing, statement, and transaction, works out the ownership over time, shows the math, cites each source, and says plainly where the record has gaps.
- **Honest.** When the record doesn't support an answer, it says so instead of guessing.
- **Local-first.** Case processing is local by default. External AI processing is optional and requires explicit operator configuration.

## Never
- Never add a "mode" or a single-use feature. Make the one assistant better instead.
- Never cap answers, drop computed numbers, or narrow behavior just to make a test pass.
- Never allowlist a single file name, or otherwise slip past a guard or check. Fix the check at the root.
- Never answer a repeat failure with another exception. Stop and diagnose it.
- Never weaken an egress, privacy, or safety check.

## How we build
- Anything bigger than a small fix starts with a design note in `specs/` covering the problem, the root cause, the approach, what it could break, and the test that proves it works.
- Check every spec and PR against this file and `AGENTS.md`.
