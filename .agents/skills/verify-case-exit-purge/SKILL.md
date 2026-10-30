---
name: verify-case-exit-purge
description: Verify changes to explicit case exits, cancellation, purge confirmation, restart, retry or ClosureStore stages using TLC and real-code synthetic traces.
---

# Verify case exit and purge

Coverage is synchronized with commit 1c59c8d02a2597dcf3971561c6f2dc24bf748d09;
the covered product files are unchanged since supplied base 0a06579. Read
`formal/case_exit_purge/README.md` for the transaction mapping and scope limits.

## Coverage record

Covered functions under `src/case_intelligence/`:

- `case_lifecycle.py`: begin_exit, cancel_exit, register_export, _ready,
  issue_confirmation, claim_purge, discard_confirmations_on_startup and
  validate_purge. External purge_cleanup effects are outside the model.
- `case_lifecycle_guards.py`: install_content_guards (Python conformance).
- `case_lifecycle_authority.py`: require_current_session_authority (synthetic
  persisted-session path in Python; provider authentication is not modeled).
- `lifecycle.py`: ClosureStore.request/advance/verify_archive/cancel and
  TrustedArchiveAdapter.verify/accepts. Ingestion transitions are outside scope.
- `workspace_store.py`: fail_matter_purge, recover_interrupted_matter_purges and
  complete_matter_purge; `case_downloads.py`: complete/receipt/invalidate.

S1 is export-only: purge needs registered export, digest and trusted delivery.
S1b covers intentional delete_without_export: explicit no-backup acknowledgment,
exact name, fresh mode-bound form and elapsed cancellation window. S1c fixes the
mode at begin_exit; only cancel plus a new exit/window changes it. S2-S6 check the
window, fresh exit-bound form, cancellation, stable retry identity and sequential
closure order. L1 assumes weak fairness of purge completion or failure/recovery.

Bounds: one durable matter, at most two exits, two modes, one absent/fresh/expired
form, two purge identities (second only in a mutant), Boolean evidence/inputs,
and one independent sequential ClosureStore run. Time is abstracted as crossing
the seven-day and ten-minute deadlines. Python checks exact endpoints. The model
assumes live authority, idle work, trusted export registration and intact
transactions. It does not prove archive bytes, cryptography, provider identity,
concurrent ClosureStore calls, transport ranges, external cleanup, physical
erasure or backups. Selected conformance traces are not a refinement proof.

## Run and interpret

Use Java 21 or newer on PATH and set TLA2TOOLS_JAR to a local jar. The default
pin is the TLA+ tools v1.8.0 rolling pre-release from
https://github.com/tlaplus/tlaplus/releases/download/v1.8.0/tla2tools.jar,
with banner TLC2 Version 2026.10.28.014557 (rev: 56d37a9) and SHA-256
`fc6878c603f0fdc18b9fe3e5bf51860cb90ab5b1154e3c371660c036d6bdd63e`.
The tag is rebuilt over time; a new download may differ. For another reviewed
build, set `TLA2TOOLS_SHA256=<its hash>` or explicitly `TLA2TOOLS_SHA256=skip`.
The runner announces overrides and warns on skipped hashing; otherwise it
requires the default pin. Record the actual banner/hash and any override used.
Do not download tools when the task prohibits network access.

Run `make formal-case-exit-purge` and `make formal-case-exit-purge-mutants`.
Run `.venv/bin/python -m pytest -q tests/test_case_exit_purge_conformance.py tests/test_case_exit_purge_consistency.py`
through any task-required logger and temporary-directory settings. Run fixture
policy checks when changing tests. `make formal-case-exit-purge-help` prints setup
help. Normal TLC must pass S1, S1b, S1c, S2-S6 and L1. All ten mutants must produce
nonzero TLC status and their expected invariant violation: S1b_name targets S1b,
S1c_mode_flip targets S1c, and the others target their namesake invariant.
Tool or syntax failures are not mutant kills. No CI job is supplied in this round.
Read TLC results with the pstack `/statechart` skill when available; otherwise
use the named action trace and README without claiming that skill was run.

Replay counterexamples with real repository calls, one model step per committed
transaction. Distinguish consumed confirmations from live forms, keep startup
invalidation separate from recovery, and retain retry's original purge_id.
For stored nonce corruption probes, use `fixture_write` and the existing store
interface, changing only the nonce field while keeping exit readiness valid.
Do not introduce raw cursor reads. If the trace cannot occur in code, correct
the abstraction and rerun both targets and pytest. If it reproduces against an
approved invariant, report the shortest synthetic trace with a strict xfail and
`reason="finding: ..."`. Do not weaken the invariant or fix product code in the
verification PR. Intentional no-export deletion is not an S1 finding; switching
mode without cancellation is an S1c finding.

## Update and prove detection

For intended changes, update action/variable references and Python traces
together, preserving the approved property meanings. Keep CaseExitPurge.cfg and
check.sh's mutant list synchronized; the consistency tests check both without
Java. Move the sync commit forward only after confirming the model describes
that revision's actual atomic steps and supported paths, then rerun all checks.

When scratch proof is authorized, save the exact original case_lifecycle.py
bytes and replace its exact-name compare with a casefold comparison. Run
`test_s1b_exact_name` through the required logger and verify the swapped-case
case fails. Restore the original bytes in a finally block. Repeat with a strip
comparison and verify both space cases fail. Restore bytes and rerun successfully;
record original/restored hashes and both red and green results. A scratch removal
of `nonce['mode'] != mode` must likewise fail
`test_s3_confirmation_bound_to_mode` in both modes, proving _ready did not mask
the nonce check. Never leave scratch edits in the deliverable.
