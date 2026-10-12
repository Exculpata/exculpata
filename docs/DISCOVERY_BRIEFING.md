# Deterministic discovery briefing

`case_intelligence.briefing.build_briefing` assembles an immutable `Briefing`
from an authorized matter's existing records. The assembler includes fixed-template
suggested questions and adds no Home component, feature flag, model call, storage
or schema. Legacy uploads link to a read-only, item-specific upload review page
for matter members; this page grants no upload or cancellation authority. Assembly
never reads original bytes or extracted text.

Callers supply the existing workspace, ordinary `EntityService`, and
`IntakeReceipts` for that workspace. The entity service must have its existing
metadata-only `current_sources` checker. `source_metadata=source_store.get`
retains original recorded extraction reasons when the catalog's presentation
label is less specific. Optional `inventory` entries are
`RecordedDocumentDate(document_id, version_id, value, basis="document_date")`.

The four stable section keys are:

- `arrived`: source-catalog counts by file type and top-level folder, including
  files at the root, and the earliest/latest explicitly recorded document dates.
- `people_places`: most frequent suggested people and places whose extraction
  identities and occurrences were created by automatic discovery. Counts group
  exact names and types for presentation; identities are never merged. Manual,
  answer-triggered, dismissed and human-confirmed identities are excluded.
  A suggested automatic identity that a reviewer retyped as a place remains
  eligible; the current deterministic extractor has no separate place rule.
- `dates`: busiest calendar days as stated, from every page of
  `EntityService.date_draft`. Ambiguous dates retain their literal spelling and
  unresolved calendar order. Times do not establish a timezone or UTC ordering.
- `unread`: incomplete intake, pending processing, extraction failures and held
  recordings, with recorded reasons. Durable ingest/media job states take
  precedence over optimistic catalog readiness. Searchable sources with recorded
  coverage notices remain listed with those notices, including partially read PDFs
  and sources with no searchable text. Skipped selection rows retain
  browser-report attribution and separate filename-check reasons. A receipt
  pointing at an already-listed current source failure is counted once;
  historical selections, unrecorded metadata and legacy uploads remain separate.

Every line has matter-relative links to existing source viewers, cited
passages, source lists, entity/date review pages or the appropriate intake
receipt page. Items never uploaded have no original source to open; their links
open the receipt or upload review instead. Groups include up to three sample
source links. Unavailable historical mentions never receive an active passage
link; an available sample is preferred, otherwise retained reference review is
linked. Source availability is checked again by the destination.

`BriefingLine.kind`, `value`, `count` and `suggested` retain structured data
separately from display text. Both people/place and found-date lines explicitly
say **Suggested**. Their ranks count retained non-dismissed automatic mentions,
including historical support, not proven identities or established events.
Date-draft eligibility preserves that service's existing human-review statuses.
The source-linked coverage caveat also states that readiness and recorded
failures do not establish complete reading of every page, image, attachment or
media segment; existing source extraction notices remain authoritative.

## Suggested questions

`Briefing.questions` contains up to five immutable questions, each explicitly
labeled **Suggested** and carrying its input row's source links. These use fixed
templates and already-read briefing data; no model, extra source read, saved
question or UI is added.

Selection rotates through people, places and mentioned dates in that order.
Within each group, rows sort by descending mention count, then literal subject
and source link as stable tie breakers. This selects prompts, not a chronological
sequence. Person/place questions ask
“What do the records say about …?” Date questions ask “What do the records say
about the mentioned date …?” Ambiguous date spellings remain literal. Questions
do not combine independently ranked names and dates or assume an event occurred.

Only positive-count Suggested rows with an available same-matter passage link
qualify. Retained historical references alone are insufficient. Empty subjects,
non-printing characters and completed questions exceeding 2,000 characters are
excluded without truncating source labels. Candidates that the existing Ask
router would treat as another task are excluded too, checking both the subject
and completed question; embedded quotes or operators must not silently change a
suggested question into another task.

Whitespace-normalized, case-insensitive subjects within each fixed template are
deduplicated before applying the five-question cap. A duplicate or ineligible row does not consume a
slot. Sparse matters may therefore have fewer questions or none. Tests cover
selection, deterministic ordering, deduplication, limits and unavailable support.

## Reviewer objective (C3)

After the existing eligibility, deduplication and five-question selection, the
short C1 reviewer objective can change only the order of those selected questions.
The assembler reads only `objective` from the current matter's purpose record
under its existing membership, source-guard, workspace-lock and external-change
checks. The stored C1 bound remains 500 Unicode characters / 1,024 UTF-8 bytes.
The longer background field and C2 source roles are not read for steering.

A question matches when its complete structured person/place name or literal
stated date appears in the objective. Both strings collapse whitespace and use
Unicode case folding; adjacent Unicode word characters (letters, numbers or
underscore) disallow a match. Punctuation is literal within the label and can
delimit it. For example, `Review JORDAN   SAMPLE.` matches `Jordan Sample`, but
`Jordans Sample`, `Jordan Samples` and `Jordan` do not. Dates are not parsed or
reformatted. There is no stemming, alias resolution, fuzzy/semantic matching or
interpretation of the objective as instructions. The question template is never
part of the matched subject.

Matching questions move before nonmatching questions; both groups preserve their
original order, regardless of subject order or repetition in the objective.
Question membership, text, Suggested labels and links remain exact. A named
subject outside today's selected five cannot add or replace a question. Sections,
counts, omitted totals and coverage also remain exact. Absent, empty,
whitespace-only, cleared or unmatched objectives produce the same unsteered
output byte for byte on the same records. No objective text is copied into a
question, answer, model prompt or descriptor. No model call or additional source
byte/extracted-text read is introduced; every existing ceiling still refuses a
partial briefing. C4 and answer/Ask behavior remain outside this increment.

For a synthetic example, suppose today's five selected questions concern
`Alex Example`, `Harbor Square`, `2024-05-06`, `Jordan Sample` and `2024-05-07`,
in that order. An objective of `Review JORDAN   SAMPLE.` moves only Jordan's
existing question to the front; the other four retain their order. Naming
`Riley Placeholder`, who is outside those five, adds or replaces nothing.
Clearing the objective or using only that unmatched name restores the exact
unsteered output for the same records.

The [C4 workflow explanation](EXIT_ALPHA_CRUISE.md#c4-connect-the-existing-workflow)
connects this behavior to Home orientation and source roles without changing it.
Home's longer background text is reviewer orientation; **Background document**
is a separate source designation. Neither is used to steer the briefing.
Home explains the existing five-question limit and retains the instruction that
choosing a question fills the composer but sends nothing until submission.

`tests/test_briefing_objective.py` exercises synthetic set/edit/clear fidelity,
matching, stable groups, the unchanged five-question population, background/role
independence, read boundaries, concurrent changes and ceiling failures. The Home
browser journey saves objectives through the native form, compares complete
unsteered markup after clearing, and selects steered questions without submitting.

## Document-date availability

Current Exculpata source catalogs and inventories do **not** record semantic
document dates. Catalog `added_at` is arrival time; filesystem `stable_mtime_ns`
is file identity metadata and can be upload time. Dates found in text are
mentions. None is substituted for a document date.

Without explicit semantic inventory metadata, the briefing reports the date
range unavailable. Supplied dates must be canonical ISO calendar days, have
`basis="document_date"`, and match a current catalog document ID and version.
Invalid, stale, wrong-basis or conflicting records are excluded with a linked
coverage explanation. The range reports only dated sources and states how many
sources lack qualifying dates. This supports future recorded metadata without
inventing current availability or adding storage.

## Consistency, limits and validation

Assembly checks membership before reading and before returning, holds the
existing source guard and workspace lock, and rejects a concurrent external
SQLite change. It does not nest a transaction around services that own theirs.
Catalog rows stream through one metadata query; current ingest and media jobs
each use one catalog-bound bulk read, avoiding per-source queries and repeated
library-wide facet calculations. Receipt pages are fully consumed and checked for missing
rows before any summary is returned.

Reads refuse inputs exceeding 100,000 rows in a bounded inventory. Found-date
reads have a separate 5,000-mention ceiling because the existing service
recomputes calendar ordering for each 25-row page. Exceeding either ceiling
raises `WorkspaceProblem`; no partial briefing is returned. By default, display
shows five ranked groups per person/place type, five date groups, and fifty
incomplete items. All underlying counts remain complete and omitted display
counts are explicit. Limits can be reduced or raised within the API's documented
validation bounds (1–25 ranked entries, 1–100 incomplete entries).

`tests/test_briefing.py` builds synthetic processed matters using the same
application and automatic-discovery flow as `test_automatic_discovery.py`. It
checks all four sections, counts, actual link destinations, semantic dates and
missing/stale metadata, empty matters, failure reasons, historical support,
pagination and display limits, read ceilings, authorization, unchanged storage,
absence of extracted-text reads, and external-change detection.
