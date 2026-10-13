# Hierarchical investigation synthesis

New investigations synthesize persisted slice-11 pass findings in two stages:
issue-level groups, then matter-level sections from those issue groups. This is
one reviewer question, with supporting and competing accounts kept as separate
source-backed claims. Saved search gaps and competing accounts remain unresolved
until the reviewer checks the originals. The final answer combines the verified
matter sections rather than selecting another twelve passages for one answer.

The first input adapter is the investigation's own persisted findings. It does
not silently import unrelated full-text runs or expand the original matter or
source-set boundary. The separate [full-text input adapter](FULL_TEXT_SYNTHESIS.md)
binds one terminal run, its exact criterion, original-source scope and frozen
human-decision revision. It reuses this engine and inspector without combining
runs or raising these limits. Complete originals above 6,000 characters are
explicitly omitted before admission; their prefixes do not substitute for the
whole unit. Full-text ledgers also remain separately available in Reports.

## Original support through both stages

Every derived claim retains confirmed parent claim IDs and original support tokens. Those
tokens resolve to the existing matter, document, source version, location, unit
and excerpt-digest ledger. Only source-verified finding claims and separately
sourced limitations enter the next stage. Free-form introductions, missing-answer
prose and verification notices are never promoted into evidence.

Each generation receives original source passages plus explicitly non-evidentiary
working context describing its parent findings. The existing source-support
verifier checks generated claims against those originals. Saved intermediate
claims are checked again before reuse; unknown versions, changed input hashes,
invalid parents or missing original support refuse the checkpoint. Final saving
and exports validate the complete chain, and existing source-version, availability,
membership and source-set checks still apply. Word, Markdown and JSON exports
include the issue/matter records and original source references.

These deterministic checks detect unsupported numbers, quotations, negation and
insufficient source overlap. They do not establish semantic truth. Model-quality
acceptance remains a separate test of decisive evidence recall and false claims.

Version 2 separately accounts for finding survival. Each generated claim names
only the input findings whose complete sentence wording it retains, with all
of those findings' original support. Whitespace and case changes are allowed;
sharing a source unit or matching some words is insufficient. Each node lists
`omitted_parents`, and the final receipt partitions admitted saved findings into
`retained_findings` and `omitted_findings` by checking the final claims directly.
This catches an account and its dropped correction even when both cite the same
unit. It also catches loss within a combined intermediate claim.

This is a conservative wording check, not semantic entailment. Grounded
paraphrases remain usable output, but their parent retention is unconfirmed and
counted with omissions. The answer, inspector and exports disclose that limit.
Any unconfirmed parent or final finding makes the synthesis partial, even when
all groups ran and every original unit is cited. Rejected generated statements
remain a separate count; the original-source grounding checks are unchanged.

## Frozen limits and partial results

For investigation inputs, version 1 preserves the existing retrieval limits: at most 72 selected original
units, 12 selections per search, and the existing search/time budget. Synthesis
has its own fixed receipt:

- At most 12 issue groups and 12 matter sections, with up to four parent claims
  per group. A claim's original support stays together.
- At most 12 original inputs, 6,000 characters per original, and 48,000 original
  characters per call. Grouping conservatively fits the character budget.
- 1,200 output tokens per generation call, using the existing generator adapter.
  This is a section budget, not a whole-report output cap.
- 32 generation requests, including charged interrupted requests. The existing
  bounded generation service may make its ordinary verification repair calls
  within each request; these are not additional synthesis groups.
- 900 wall-clock seconds from first synthesis start, including restart downtime.
  The deadline is checked between requests; an in-flight call can finish later.

The saved receipt identifies omitted groups, rejected findings, finding omissions, uncited original
units, characters outside synthesis packets, and the stopping reason. Partial
processing stays visible in the answer, checkpoint inspector and exports. A
completed bounded group traversal does not mean all relevant facts were found.
Units and character budgets do not measure original pages read or whole-matter
coverage. An exhausted synthesis requires a new investigation; extending search
budget does not replenish synthesis requests or reset its deadline.

## Checkpoints, backup and rollback

`plan.synthesis_version` identifies the hierarchy workflow and
`result.hierarchical_synthesis.version` identifies its checkpoint format in the
existing research-job JSON columns. New checkpoints use version 2; the plan and
full-text input receipt remain version 1. No SQL migration is needed.
Historical plans keep their previous synthesis behavior. Each completed issue or
matter node is checkpointed before the next call. Requests are durably charged
before dispatch, so cancellation or a crash cannot refund them. Restart reuses
complete validated nodes and repeats only uncommitted work within the remaining
budget. New searches or source changes invalidate derived nodes without refunding
synthesis spend or its first-start deadline.

Version 1 results remain readable and exportable with their original historical
receipts; the inspector identifies that finding retention was not measured.
Resuming validated version 1 work upgrades its parent accounting in memory and
saves version 2 at the next guarded checkpoint. It reuses generated claims and
keeps the original start time and spent requests. It never silently treats the
old packet-wide parent list as finding-survival evidence. Unknown formats and
tampered version 2 parent/omission receipts are refused.

Take the consistent backup described in [STORAGE_AND_BACKUP.md](STORAGE_AND_BACKUP.md)
before upgrading. Older writers do not understand the new synthesis checkpoints;
finish or cancel active work and restore a matching pre-upgrade backup for a
rollback. Preserve the upgraded backup and exported post-upgrade work separately.

`tests/test_hierarchical_synthesis.py` distributes support and contradiction over
24 synthetic units, rejects corrupted intermediate provenance, proves pre-dispatch
charging and restart reuse, exercises explicit partial results, and restores an
online SQLite backup into a clean database before resuming and exporting JSON,
Markdown and Word. Integrity and foreign-key checks are included. These plumbing
tests do not substitute for representative local-model evaluation.

`tests/test_synthesis_finding_accounting.py` adds the shared-unit correction,
per-stage omissions, conservative paraphrase accounting, tampered receipts,
version 1 resume and persisted page/export regressions. The hierarchy evaluator
requires all 24 fixed source statements with their original citations and complete
admitted-finding accounting; preserving only the first and last accounts fails.
Controlled evaluator tests cover both 2/24 failure and 24/24 success without
loading a model. The dated model receipt below is historical and is not rerun
by these deterministic tests.

## Local-model acceptance receipt — September 12, 2026

The cross-group matter-stage evaluation used the already staged native
`qwen3.5:4b` Q4_K_M candidate, Ollama 0.33.3 on Apple M4, manifest
`sha256:2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd`.
The installed artifact verifier rehashed the manifest and blobs before serving;
the artifact's license is Apache-2.0. This is an Ollama distribution digest,
not a claim of equivalence to a Hugging Face revision or a model swap.

The fixed corpus contains 24 saved source findings, with decisive support in
unit 1 and explicit contradiction in unit 24, plus an unsupported saved finding.
The production hierarchy and source verifier ran six issue groups and six
cross-group matter sections. The result retained all 24 originals (24/24), both
decisive accounts (2/2), and no injected claim. Inspection of the 24 displayed
claims against this fixed corpus found zero false claims: each matched its
cited source statement exactly. It spent 12 generation requests in 102.74 seconds.
The rejected unsupported finding correctly kept the receipt marked partial,
even though no original unit was uncited and all admitted groups completed.

Reproduce with the existing staged artifact; this command does not download:

```console
PYTHONPATH=src python scripts/evaluate-hierarchical-synthesis.py \
  --model qwen3.5:4b \
  --expected-digest 2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd \
  --disable-thinking --output /tmp/hierarchical-model-evaluation.json
```

The harness explicitly uses the installed native evaluation setting
`think=false`, 8,192 context tokens and 1,200 output tokens. The initial probe
with the current application's default Ollama wire request returned no structured
answer. The evaluation-only switch does not change application adapter defaults
or advance the held Mac integration. This receipt qualifies the small corpus
through the hierarchy and verifier under the stated model settings; it does not
qualify arbitrary long packets, other portfolios, Linux/GPU throughput, or a
private deployment. Deterministic focused acceptance separately passed 214 tests,
including research, generation, report-basis, exports and backup/clean restore.

### Review corrections

Late source availability continues to produce the existing partial-coverage
notice without discarding valid cited work. Each cited original is still
revalidated, and a cited source leaving the selected set refuses the synthesis.
Recovery rechecks the saved availability fingerprint and rebuilds changed inputs.

Investigation pages and JSON/Markdown/Word exports explicitly retain the machine
transcript caution. A missing or changed required caution fails provenance
validation. Partial notices list rejected finding IDs and count omitted generated
statements, including when every admitted group completed. Hierarchical portable
JSON references now include the frozen `version` and a stable `citation_id` derived
from the original locator identity; each claim joins to the exported source ledger
using that identifier. Internal action tokens and routes remain excluded.

Synthetic concurrent-upload and source-set regressions cover late additions and
removal of cited scope. A persisted synthetic transcript exercises page rendering,
all three export formats, warning validation, and claim-to-version joins. Its
media-inspection and transcription providers are controlled fixtures; this is
output/provenance acceptance, not audio-model qualification.
The expanded post-review focused suite passed 307 tests, including the existing
email/late-availability coverage cases and the new output regressions.
Interrupted runs also expose their validated issue/matter checkpoints and charged
budget on the details page. Unverifiable checkpoint text is hidden with an
explicit recovery notice rather than rendered as a saved finding.

Final saving also rechecks every cited document against the selected source set
inside the workspace write transaction. A source removed after processing but
before completion prevents both the succeeded state and conversation result
from being committed; unrelated source additions remain coverage disclosures.
