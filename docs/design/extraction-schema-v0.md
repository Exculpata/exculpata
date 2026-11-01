# Extraction schema v0

Status: design note for the 10,000-document pilot. This note defines a proposed
one-time read at document load. It does not change product behavior or select a
model.

## Boundary and output contract

The extractor proposes small, cited facts for later review. Its output is a JSON
array, with no prose, comments, trailing commas, unknown keys, or non-JSON numeric
values. Every object has the common fields below and exactly one type-specific
`data` object. All strings are UTF-8, IDs are opaque, offsets are zero-based
half-open Unicode code-point offsets, pages are one-based, and decimal quantities
are strings. The estimated average output budget is about 150 tokens per document.
The pilot will measure the actual distribution rather than treat this estimate as
a completeness limit.

| Common field | Type | Rule |
| --- | --- | --- |
| `type` | enum | `person`, `organization`, `account`, `amount`, `date`, `money_movement`, `event`, or `note` |
| `data` | object | Exactly the fields allowed for `type` |
| `confidence` | number | Inclusive range 0 to 1; a routing signal, not proof |
| `citation` | object | One resolvable source location as defined below |
| `schema_version` | string | Exact schema identifier, initially `extraction-v0` |
| `extractor_id` | string | Immutable extractor implementation and artifact identifier |
| `settings_digest` | string | SHA-256 of canonical extraction settings |

These short payload fields make up the v0 record types. Optional fields may be
omitted, but may not be `null`; `additionalProperties` is false at every level.

### Record type fields

| Type | Required fields | Optional fields | Meaning |
| --- | --- | --- | --- |
| `person` | `name` | `role` | A named person and an explicitly stated role |
| `organization` | `name` | `role` | A named organization and an explicitly stated role |
| `account` | `id` | `kind`, `owner` | An account identifier, without inferring ownership |
| `amount` | `value` | `currency`, `label` | A decimal quantity and its stated context |
| `date` | `value` | `precision`, `label` | Source text normalized to ISO 8601 when unambiguous |
| `money_movement` | `value` | `currency`, `from`, `to`, `date` | One stated transfer; absent endpoints stay absent |
| `event` | `summary` | `date`, `actor` | A concise stated occurrence, without added causation |
| `note` | `text` | none | A material fact that does not fit another v0 type |

Example rows below use synthetic IDs and structurally valid SHA-256
values. `citation.locator` is either an offset locator or a page-region locator.

```json
[
{"type":"person","data":{"name":"Avery Quinn","role":"sender"},"confidence":0.98,"citation":{"source_id":"email-01","source_version_id":"v1","locator":{"start":6,"end":17}},"schema_version":"extraction-v0","extractor_id":"extractor-build-17","settings_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
{"type":"organization","data":{"name":"Northwind Cooperative"},"confidence":0.96,"citation":{"source_id":"email-01","source_version_id":"v1","locator":{"start":41,"end":62}},"schema_version":"extraction-v0","extractor_id":"extractor-build-17","settings_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
{"type":"account","data":{"id":"SYN-2048","kind":"savings"},"confidence":0.97,"citation":{"source_id":"statement-01","source_version_id":"v3","locator":{"page":1,"region":[120,180,340,214]}},"schema_version":"extraction-v0","extractor_id":"extractor-build-17","settings_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
{"type":"amount","data":{"value":"1250.00","currency":"USD","label":"balance"},"confidence":0.99,"citation":{"source_id":"statement-01","source_version_id":"v3","locator":{"page":1,"region":[410,620,540,652]}},"schema_version":"extraction-v0","extractor_id":"extractor-build-17","settings_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
{"type":"date","data":{"value":"2026-10-14","precision":"day","label":"sent"},"confidence":0.99,"citation":{"source_id":"email-01","source_version_id":"v1","locator":{"start":25,"end":35}},"schema_version":"extraction-v0","extractor_id":"extractor-build-17","settings_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
{"type":"money_movement","data":{"value":"700.00","currency":"USD","from":"SYN-2048","to":"SYN-4096","date":"2026-10-13"},"confidence":0.94,"citation":{"source_id":"statement-01","source_version_id":"v3","locator":{"page":1,"region":[80,400,550,438]}},"schema_version":"extraction-v0","extractor_id":"extractor-build-17","settings_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
{"type":"event","data":{"summary":"Meeting moved to Friday","actor":"Avery Quinn"},"confidence":0.91,"citation":{"source_id":"email-01","source_version_id":"v1","locator":{"start":64,"end":87}},"schema_version":"extraction-v0","extractor_id":"extractor-build-17","settings_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
{"type":"note","data":{"text":"The sender requested a paper copy"},"confidence":0.88,"citation":{"source_id":"email-01","source_version_id":"v1","locator":{"start":89,"end":113}},"schema_version":"extraction-v0","extractor_id":"extractor-build-17","settings_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
]
```

## Citation resolution

Every row carries `source_id`, immutable `source_version_id`, and exactly one locator:

* Text uses integer `start` and `end`, with `0 <= start < end`.
* Fixed-layout material uses integer `page` and `region: [x1,y1,x2,y2]`, in the
  representation's recorded coordinate system, with positive area.

Resolution binds the cited source version and representation, validates bounds,
and recovers the exact supporting text or region. A row whose citation is absent,
ambiguous, outside bounds, on a different source version, or otherwise
unresolvable is rejected and never stored. Resolution does not fall back to a
current version or a matching passage elsewhere. Multi-location facts become
separate rows or a later schema revision rather than weakening this rule.

## Versioning and review history

Rows are immutable snapshots keyed by a generated row ID outside the extractor
payload. A run records its `schema_version`, `extractor_id`, and `settings_digest`
on every row, plus run time and input representation in the run receipt.

A changed schema version, extractor artifact, or extraction-affecting settings
digest creates a new run for each in-scope source version. Operational retries
with identical inputs reuse the run identity and must be idempotent. Old rows stay
queryable with their run and review state. A new row may explicitly `supersede` an
old proposed row after deterministic comparison. It never mutates or silently
replaces a row. If a human reviewed the old row, replacement requires a new human
decision, and the old decision remains in history. Reprocessing a changed source
version does not supersede facts from the earlier source version automatically.

## Skip and bounded-read rules

Coverage contains every discovered source version, including these outcomes:

| Outcome | Rule | Coverage reason |
| --- | --- | --- |
| Exact duplicate | Byte digest matches a resolved source version | `exact_duplicate`, with canonical source ID |
| Empty | No extractable content after deterministic parsing | `empty` |
| Boilerplate | Content matches a versioned deterministic boilerplate rule | `boilerplate`, with rule ID |
| Ledger | A supported tabular parser produced rows | `ledger_deterministic`; extractor reads only header plus a bounded sample to propose a column mapping |
| Under-read | Only a bounded portion was read | `partial`, with included and omitted ranges |

A proposed ledger mapping is not applied until validated. Ledger facts remain
deterministic parser output, not extraction rows. Parse failure removes the skip
and routes the document through ordinary extraction or escalation.

## Escalation

The first pass routes a document to the larger configured tier when any trigger
fires: row confidence below the pilot threshold, strict schema or citation
validation failure, length or fact density above calibrated limits, a money
movement above the configurable pilot threshold, or conflicting normalized values
for the same stated field. Threshold values and trigger evidence are versioned in
the settings digest. A failed second pass remains visible as failed coverage. It
does not silently accept first-pass rows.

Capacity planning estimates about 12 percent escalation, with an expected range
of 8 to 20 percent. This is an estimate, not a target. The 10,000-document pilot
will report escalation by trigger, overlap, document kind, and success or failure.

## Near-duplicates and quoted email history

Exact duplicates alone are skipped. For near-duplicates, a deterministic diff
selects one complete canonical read plus every differing range from each related
version. Email parsing separates the new message from quoted history using
versioned boundary rules. Previously read quoted blocks reference their canonical
read; new or changed blocks are read in full.

Coverage records canonical and differing ranges and their digests. The union must
span every input character as `read`, `canonical_reference`, or explicit
`unresolved`. Diff context overlaps both sides of each change, and a changed range
is never replaced by similarity alone. If alignment, parsing, or citation
translation is ambiguous, the affected version receives a full read or escalation.
These invariants prevent a differing fact from being silently dropped.

## Coverage and measured misses

An extraction miss is an answer-key fact supported by an in-scope source version
for which no valid extraction row matches the expected type, normalized value,
and resolvable support. A skipped, partial, unresolved, or failed document remains
in the denominator. Coverage says what was read; it does not claim that every fact
was found. Unsupported extractions are measured separately as false positives.

Elusion sampling compares stratified samples from the 10,000-document pilot with
spec a's answer-key schema. Sampling strata include document kind, skip reason,
escalation state, length, and near-duplicate handling. Reviewers adjudicate blind
to the extractor outcome. Reports give counts and measured recall with confidence
intervals, plus miss categories and sampling weights. The answer-key version and
adjudication state are pinned so reruns remain comparable.

## Prompt-injection boundary

Document text is untrusted data, including text that resembles instructions.
The extractor has no tools, network access, file access, or side effects. It can
emit only strict schema rows to a bounded response. Deterministic validation and
citation resolution run outside the extractor; invalid output is rejected. The
extractor cannot write coverage, review decisions, source content, or settings.

## Worked synthetic examples

**Fictional email.** Source `email-01` v1 contains: "From: Avery Quinn. Sent:
2026-10-14. To: Northwind Cooperative. Meeting moved to Friday. Please send a
paper copy." The first pass emits the `person`, `organization`, `date`, `event`,
and `note` example rows above, each with offsets into v1. A repeated copy with the
same byte digest is not read again; coverage records `exact_duplicate` and points
to `email-01`. A reply that adds "Use account SYN-2048" reads the canonical
message once and the new differing range, which produces a cited `account` row.

**Fictional statement page.** Page 1 of `statement-01` v3 shows account
`SYN-2048`, balance USD 1250.00, and a USD 700.00 transfer to `SYN-4096` on
2026-10-13. It emits the `account`, `amount`, and `money_movement` rows above with
page regions. The transfer exceeds the synthetic pilot threshold of USD 500.00,
so the document is escalated and coverage records `high_value_movement`. This
threshold illustrates routing only and does not set the production value.

## Open questions for Neil

These choices remain undecided:

1. What immutable identifier format should `extractor_id` use across runtimes?
2. Should v0 allow multiple citations per fact, or defer that to v1 as proposed?
3. Which confidence, length, density, and money-movement thresholds should the
   pilot start with, and should thresholds vary by document kind or currency?
4. What deterministic boilerplate rule set and ledger formats enter the pilot?
5. What precision and currency normalization vocabularies belong in v0?
6. What review action permits a new extraction to supersede a reviewed row?
7. What sample sizes and recall interval method should govern elusion reporting?
8. When should a near-duplicate cluster be rebuilt after a source version changes?
