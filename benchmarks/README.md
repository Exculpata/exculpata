# Slice 0 benchmark

This is an executable deterministic synthetic policy benchmark, not a production
retrieval/model quality benchmark. Its fixed evaluation time is
`2026-08-25T16:00:00Z`; it makes zero network, model, service, or database calls.

`slice0-suite.json` contains 46 ordered executable definitions:

- 2 retrieval cases, including the reviewer-reproduced shared query for both
  matters. Actual `Catalog` behavior returns text, image, and audio source
  versions for each matter, and the expected IDs now say so;
- 12 cross-matter/authorization attempts;
- 14 ticket/lease bypass attempts;
- 10 valid exact-reference resolution attempts; and
- 8 invalid/foreign/impossible-reference rejection attempts.

The runner records each observed outcome. It then derives every metric numerator,
denominator, fraction, pass flag, and the final pass from those records. It does
not accept caller-supplied summaries. Validation checks the JSON Schema, suite
identity/version/digest and fixture identity, reruns all cases, and requires the
submitted result to equal canonical normalized runner output. Edited summaries,
omitted or duplicated cases, invented/inconsistent outcomes, and wrong
identities or denominators fail.

Verify the committed reference:

```bash
.venv/bin/python -m case_intelligence.evaluation verify
```

Regenerate it after an intentional suite, fixture, or policy change, then review
the diff and verify again:

```bash
.venv/bin/python -m case_intelligence.evaluation generate \
  --output benchmarks/expected/slice0-reference-result.json
.venv/bin/python -m case_intelligence.evaluation verify
```

Confirmed executable raw counts are cross-matter/authorization successful
bypasses `0/12`, ticket/lease successful bypasses `0/14`, valid references
resolved `10/10`, and invalid references rejected `8/8`.

`production_selection=false`. No production model or retrieval implementation
was evaluated or selected; reranking and generation remain explicit
not-implemented component markers.

The application must not turn these deterministic policy results into model
quality or performance claims. Retrieval recall, citation correctness, grounded
answer rates, classification precision/recall, transcription accuracy, latency,
throughput, and resource use require a separate versioned evaluation artifact
that identifies the exact source, model revisions, containers, hardware,
runtime configuration, dataset fingerprint, and human-review protocol.

## Review-quality contract pack

`case_intelligence.review_quality.QUALITY_EVALUATION_CASES` is a compact,
content-free contract pack for exact-record ranking, explicit written-and-spoken
completion, objective-preserving synthesis, and broad-summary scope. Its fixed
fingerprint prevents silent fixture drift. One synthesis case withholds the
final narrative source so an implementation cannot pass by quoting a prepared
answer instead of using the component evidence.

These cases keep retrieval ranking, modality completion, generator objective,
and scope disclosure separately reportable. They are deterministic contract
tests, not a claim that any configured model meets a production quality bar;
representative model evaluation still requires the exact model/runtime record
described above.

## Frozen Review acceptance pack

`review-acceptance-v1.json` maps one compact synthetic contract pack to
separately reportable pytest categories: ingestion/OCR, transcript and speaker
review, retrieval rank, cross-modal completion, generator objective,
abstention, exact citation resolution, browser task success, conversational
investigation follow-up, every-source screening, broad-summary scope, optional
overview recovery, sampled-frame OCR contracts, and lifecycle authorization.
The pack is content-free, requires no network, carries a frozen case fingerprint,
and includes a synthesis case where the prepared final source is withheld.

Run all categories, or select one, with:

```bash
PYTHONPATH=src .venv/bin/python scripts/run-review-acceptance.py
PYTHONPATH=src .venv/bin/python scripts/run-review-acceptance.py \
  --category cross_modal_completeness
```

The runner invokes each category independently and emits a content-free JSON
status report. It does not claim that a configured production model passed; the
model-specific evaluation record described above remains a separate gate.

September 8, 2026 test correction: the cross-modal regression now inspects the
requested written-only question instead of assuming the last generator call
cannot be a background transcript overview. The corresponding test-node, helper
file and aggregate content digests were refreshed together. Case definitions,
case fingerprint and fixture bytes are unchanged; results from different content
fingerprints must remain distinguishable.

September 10, 2026 asynchronous fixture corrections: speaker-review response
assertions hold the media coordinator so an immediate overview cannot finish
before the queued-refresh readback. The legacy-overview restart fixture stops
the coordinator and takes the workspace lock before constructing its offline
snapshot. The speaker test-node, media helper-file, and aggregate content digests
were refreshed together. Case definitions, case fingerprint, expected speaker
behavior, and fixture bytes are unchanged. Receipts remain distinguishable by
content fingerprint; the integrity checks still reject undeclared changes.

September 23, 2026 OCR receipt correction: the bounded-page OCR regression now
returns the typed OCR outcome from its injected recognizer and checks the new
coverage message. Its generated text and exact page-locator assertions remain
unchanged. The affected test-node, upload-test file, and aggregate content
digests were refreshed together. The schema, pack identifier, case definitions,
case fingerprint, and fixture bytes are unchanged; the content fingerprint
distinguishes these test bytes from earlier receipts. The integrity checks still
reject undeclared test or fixture changes.

October 13, 2026 lifecycle integration: the matter-management acceptance fixture
now uses a real verified preservation archive, elapsed cancellation window and
fresh authenticated confirmation before testing deletion and interrupted
cleanup. Existing authority, retained-work and cleanup assertions remain. The
management test-node/file and aggregate content digests were refreshed after
reviewing those changes. Case definitions, case fingerprint and fixture bytes
are unchanged; older receipts remain distinct by content fingerprint.
