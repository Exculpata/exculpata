# Coverage ledger and per-query coverage report

Status: design only. This note does not change product behavior or define a
migration. It describes a future record of what the system did and did not read,
and a report that lets a reviewer assess the scope of an answer's search.

## Design principles

- Coverage is evidence about processing, not a statement that relevant material
  was found or that an answer is correct.
- The ledger records source-version facts. A new source version gets its own
  entries rather than rewriting the history of an earlier version.
- A query report is an immutable snapshot tied to the query and its matter scope.
- Counts come from the catalog and coverage ledger. Producing a report never
  opens every source file.
- Unknown history remains unknown. Backfills must not invent extraction, OCR, or
  model-read events for older sources.
- Flags use the phrase **candidate for attorney review**. They do not state an
  outcome.
- Exclusion, failure, and partial reading remain visible even when an answer has
  citations or a search returned no matches.

## Coverage ledger

The logical ledger has one current summary per source version plus append-only
events for attempts. The summary supports bounded counting; events support audit
and error detail. Identifiers and timestamps are metadata, not source content.
The implementation should retain the existing source and citation identity.

### Source-version field table

| Field | Meaning |
| --- | --- |
| `matter_id` | Matter boundary used for authorization and aggregation. |
| `source_id` | Stable catalog source identifier. |
| `source_version` | Exact version to which all remaining fields apply. |
| `ingestion_state` | `received`, `processing`, `ready`, or `failed`, with recorded timestamps. |
| `ingested_bytes_digest` | Digest identifying the received bytes without copying content into the ledger. |
| `text_extraction_state` | `not_attempted`, `partial`, `complete`, or `failed`; complete means the configured extractor finished, not that it captured every visible item. |
| `extracted_unit_count` | Number of retained pages, sections, segments, or line groups. |
| `extracted_character_count` | Count of retained extracted characters. |
| `extraction_schema_version` | Version of the extraction output contract used for this source version. |
| `ocr_state` | `not_applicable`, `disabled`, `not_selected`, `attempted`, `partial`, or `failed`. |
| `ocr_engine` | Engine identifier when OCR was attempted. If Docling is evaluated, OCR is pinned to Tesseract or Nemotron OCR and the RapidOCR extra is excluded. |
| `ocr_attempted_ranges` | Page or range locators sent to OCR. |
| `ocr_returned_text_ranges` | Attempted locators for which OCR returned retained text; this is not a quality judgment. |
| `ocr_skipped_ranges` | Locators omitted because of policy, page caps, resource limits, or disabled OCR. |
| `model_read_state` | `not_requested`, `full`, `partial`, `skipped`, `failed`, or `escalated`. |
| `model_read_ranges` | Extracted-text locators actually supplied for model reading. |
| `model_read_omitted_ranges` | Extracted-text locators not supplied, with reason codes. |
| `escalation_state` | Whether a bounded read was escalated, declined, completed, or failed. |
| `record_types_extracted` | Versioned record-type identifiers attempted and produced for this source. |
| `error_codes` | Stable, non-content error codes by stage. |
| `error_details_ref` | Access-controlled reference to bounded operational detail. |
| `first_attempted_at` | Time the source version first entered the relevant processing pipeline. |
| `last_updated_at` | Time this source-version summary last changed. |

`full` means every eligible extracted-text range within the declared operation
was supplied under that operation's limits. It does not mean every part of the
original file was extracted, recognized, understood, or relevant. `partial`
requires omitted-range counts and reasons. `skipped` requires a reason such as
unsupported type, policy exclusion, source not ready, selected-source scope,
budget limit, or cancellation. `escalated` records the request and its eventual
state rather than treating escalation as a successful full read.

The event form also records an event identifier, attempt number, component
version, start and finish times, status, reason code, and the bounded counts or
locators changed by the event. Error detail must not include source text in
ordinary reports or logs.

## Per-query coverage report

The report is saved with the answer. It declares the search boundary before
summarizing activity. Later catalog changes do not silently rewrite a saved
report. A live view may offer a separately labeled current-state comparison.

### Query-report field table

| Field | Meaning |
| --- | --- |
| `report_schema_version` | Version of this report contract. |
| `query_id` | Identifier connecting the report to one saved query or answer. |
| `coverage_as_of` | Time at which the saved catalog and ledger snapshot was taken. |
| `matter_scope` | Matter identifier and, when used, selected source-set identity and version. |
| `search_lanes` | Lanes attempted, such as lexical, semantic, metadata, or record search. |
| `filters` | Applied date, type, custodian, tag, language, or other structured filters. |
| `eligible_source_count` | Catalog source versions inside the declared scope and filters. |
| `searched_source_count` | Eligible source versions reached by at least one declared search lane. |
| `model_read_full_count` | Sources whose eligible extracted text was fully supplied for the operation. |
| `model_read_partial_count` | Sources with some eligible extracted text omitted. |
| `model_read_skipped_count` | Eligible sources not model-read, grouped by reason. |
| `failed_source_count` | Eligible sources whose search, extraction, OCR, or model-read stage failed, grouped by stage and reason. |
| `pending_source_count` | Eligible catalog sources still uploading or processing at the snapshot. |
| `changed_during_query_count` | Sources added, removed, or version-changed after the initial boundary. |
| `record_type_coverage` | Record types attempted and produced, with source counts and schema versions. |
| `measured_recall` | Elusion-sampling method, sample size, misses, estimate, interval, and measurement date when available. |
| `limitations` | Ordered plain-language notices derived from nonzero partial, skipped, pending, changed, failed, and unknown states. |

One source can appear in more than one stage-specific limitation. Therefore the
report must not add model-read, OCR, and extraction counts as if they were
disjoint. Each displayed count names its denominator. Reason groups reconcile
to their parent count, and zero counts remain available in machine-readable
exports even when the interface suppresses them.

## Extraction coverage and measured recall

For each versioned record type, the report states which eligible source versions
were attempted, which produced records, which were skipped, and which failed.
Examples of record types might include dated events, named entities, or quoted
statements. A produced record is a candidate for attorney review, not an
established fact.

Elusion sampling measures misses in a defined sample of material that the
extractor did not flag. Report the population definition, randomization method,
sample size, adjudication procedure, observed misses, estimate, uncertainty
interval, and measurement date. Label the result **measured recall**. Do not
extrapolate it to other record types, schema versions, source populations, or
time periods. When no valid measurement exists, show "Measured recall: not
measured" rather than substituting a benchmark or a completeness claim.

## Wording rules

1. State the scope, activity, and limitation separately.
2. Use factual verbs such as `searched`, `supplied`, `skipped`, and `failed`.
3. Do not use `complete`, `comprehensive`, `all relevant`, `cleared`, or similar
   language to characterize an answer or review outcome.
4. `Complete` may appear only as a defined machine state for a bounded processing
   step, accompanied by its limit. User-facing prose says what was processed.
5. Describe every flag as a **candidate for attorney review**.
6. Call sampling results **measured recall**, never guaranteed recall.
7. If any eligible source is partial, skipped, pending, changed, failed, or has
   unknown history, the answer must show that limitation next to its coverage
   summary. Citations and zero-result searches do not override it.
8. Say "Review the original source when the distinction matters" rather than
   promising that retry, OCR, escalation, or manual review will resolve it.

## Example reports

### 1. All eligible extracted text read

> Scope: 24 catalog sources in Matter M-104, with no source filters. Lexical,
> semantic, and metadata lanes searched all 24 sources. Extracted text from all
> 24 was supplied for this query; no eligible source was pending, skipped, or
> failed. PDF extraction and OCR are bounded processes and may not capture every
> visible item in an original file. Three dated-event records are candidates for
> attorney review. Measured recall: not measured for this source population.

This is the report's bounded best state, but it does not call the answer or the
underlying material complete.

### 2. Partial read with skips

> Scope: 1,240 catalog sources in Matter M-208, filtered to messages dated from
> January through March. Lexical and semantic lanes searched 1,180 of 1,200
> eligible sources. Extracted text from 960 sources was fully supplied for this
> query, 220 was partly supplied because of the read budget, and 20 was skipped
> because processing was still pending. Results may omit material from those
> sources. Eleven quoted-statement records are candidates for attorney review.
> Measured recall for quoted-statement extraction: 92% (95% interval: 86% to
> 96%), based on adjudication of a 150-item elusion sample on 2026-10-15. This
> measurement applies only to the stated population and schema version.

### 3. Degraded by OCR failures

> Scope: 87 catalog PDFs in selected source set S-12. Lexical and metadata lanes
> searched retained text for 87 sources. OCR was attempted for 31 sources; it
> returned retained text for 22, failed for 6, and skipped capped pages in 3.
> Extracted text from 78 sources was supplied for this query and 9 had no usable
> text to supply. The answer may omit content that OCR did not return. Two
> timeline entries are candidates for attorney review. Measured recall: not
> measured. Review the original source when the distinction matters.

## Presentation and computation

- **Answers:** show a compact summary directly below the answer, with limitations
  visible without opening a detail panel. Detail expands to lanes, filters,
  denominators, reasons, record types, and measured recall.
- **Exports:** preserve the saved summary and full structured report in human-
  readable and machine-readable forms, including the snapshot time and schema
  versions.
- **Dashboard:** aggregate current catalog and ledger states, link counts to the
  affected source versions, and distinguish current state from saved query
  snapshots.

At 1 million sources, report computation uses indexed catalog and ledger
aggregations scoped by matter, filter, source set, state, stage, and reason.
Counters may be maintained incrementally or computed by bounded database
grouping. Query execution records the source-version boundary and updates only
the query's activity rows. No report path lists every source in memory, reads
source bytes, reruns extraction, or invokes a model merely to calculate counts.
Drill-down uses paginated catalog queries. Approximate counts, if introduced,
must be labeled approximate and must not hide nonzero failure categories.

## Per-query provenance and coverage export

The January 1 requirement is one exportable provenance and coverage record per query,
in machine-readable form plus a readable view. Extend the saved report with what
was searched, read, skipped with reason, failed, and still unread, tracing
every answer statement to its source spans. Record the model, runtime, and endpoint
used at each step so a later optional cloud provider fits without a format change.
The dashboard shows the same record, distinct from current-state aggregates.

The record must read the same on every hardware profile; only timings differ.
Profiles are sized by usable model memory: GPU VRAM or unified memory, CPU-only,
and multi-GPU, auto-detected with a user override and no hard caps.

## Open questions for the maintainer

These questions are intentionally undecided:

1. What retention period should apply to query activity events and saved reports?
2. Should the compact answer view show separate search and model-read counts, or
   one count plus an always-visible details control?
3. Which filter values may appear in exports, and which should be represented by
   an identifier or redacted summary?
4. What source mutation policy should freeze a query boundary when catalog state
   changes during retrieval?
5. Which confidence-interval method and minimum sample size should be standard
   for measured recall?
6. Who may adjudicate an elusion sample, and how should disagreements be stored?
7. Which record types enter the first extraction-coverage schema?
8. Should current dashboard counts be exact at 1 million sources, cached with an
   age label, or optionally approximate?
9. How much error detail belongs in reviewer exports versus restricted
   operational diagnostics?
10. Does escalation create a new query report or a linked revision of the
    original report?
