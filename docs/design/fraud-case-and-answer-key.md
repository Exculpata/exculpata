# Synthetic fraud case and answer key

## Purpose and boundaries

This note defines a fictional case fixture and the versioned truth data used to
measure recall. It is a design contract for a later generator, not an
implementation or a product-behavior change. All names, identifiers, documents,
and events are synthetic. The corpus supports repeatable local-first evaluation;
it does not predict results on other material or promise an outcome.

## Case at a glance

The full-size fixture contains approximately:

- 40 people, of whom 8 to 12 participate in the planted scheme;
- 15 shell companies, plus ordinary operating businesses;
- 60 accounts at invented banks;
- 50,000 transactions;
- 2,000 emails, some with attachments;
- 300 native-text PDFs; and
- 300 image-only scanned statement PDFs, totaling about 1,200 page images.

Counts are targets rather than semantic facts. Generator metadata records the
actual counts. Non-scheme people, businesses, messages, and transactions provide
neutral noise without changing the planted case.

## Planted facts and ambiguity

The answer key identifies instances of each pattern rather than asking an
evaluator to infer them from amounts alone:

- layering chains with 3 to 6 transaction hops;
- round-tripping in which value returns to an originating entity;
- repeated transfers structured below a synthetic threshold;
- fan-in to one account and fan-out from one account;
- rapid in-and-out transfers;
- payments to shells for which the fixture supplies no business purpose; and
- dormant accounts that become active.

Emails, attachments, and statements also contain misleading or mutually
inconsistent claims. The key links each claim to decisive passages and the
facts that make the inconsistency measurable. A planted anomaly is a reference
label, not a conclusion that every superficially similar transaction is part of
the scheme.

People and organizations appear through nicknames, typographical errors, and
variant spellings. Some entities share synthetic addresses or phone numbers.
Depending on the source, account numbers appear in full, masked, or as the last
four digits. Mention-to-entity mappings state the intended merges; shared
attributes alone do not establish identity.

## Source formats

Mail is emitted as individual EML files and MBOX collections, including nested
attachments. Ledgers use CSV and XLSX with several documented column layouts,
sheet names, date formats, and debit/credit conventions. Statements include
native-text PDFs and image-only scanned PDFs. The scanned fixtures use the
existing synthetic rendering approach: render text to page images and assemble
them as PDFs with controlled skew, blur, and speckle. This introduces no new
runtime dependency.

Every generated artifact has a stable source id and relative path. A locator is
either a sheet and one-based row or a one-based page and normalized region box.
The normalized box is `[x0, y0, x1, y1]` in page coordinates from 0 through 1,
with the origin at the top left.

## Emitted records

The unavailable research prototype is described as emitting `sources.jsonl`,
`chunks.jsonl`, and `gold.jsonl`. The later generator should retain those record
roles where they fit:

- `sources.jsonl` inventories source artifacts, hashes, media types, and parent
  relationships for attachments;
- `chunks.jsonl` inventories extracted passages and their source locators; and
- `gold.jsonl` contains one answer-key envelope as defined below, or typed gold
  records carrying the same envelope fields if streaming requires it.

The prototype itself was not available while this note was written, so field
compatibility must be checked before implementation. Published corpus versions
are immutable. A shape change increments `answer_key_version`.

## Answer-key schema

The top-level JSON object has the following fields. `decimal-string` means a
base-10 string with two fractional digits so binary floating point is never the
authority for money. Timestamps are UTC RFC 3339 strings. All referenced ids are
unique nonempty strings within a corpus.

| Field | Type | Example |
| --- | --- | --- |
| `answer_key_version` | string | `"1.0.0"` |
| `corpus_id` | string | `"cedar-rill-ci-seed-17"` |
| `seed` | integer | `17` |
| `scale` | enum: `ci`, `10k`, `100k`, `1m` | `"ci"` |
| `entities` | array of entity rows | `[{"entity_id":"ent-001",...}]` |
| `accounts` | array of account rows | `[{"account_id":"acct-001",...}]` |
| `transactions` | array of transaction rows | `[{"transaction_id":"txn-001",...}]` |
| `money_paths` | array of money-path rows | `[{"path_id":"path-001",...}]` |
| `timeline_links` | array of timeline-link rows | `[{"link_id":"link-001",...}]` |
| `anomalies` | array of anomaly rows | `[{"anomaly_id":"anom-001",...}]` |
| `facts` | array of fact rows | `[{"fact_id":"fact-021",...}]` |
| `questions` | array of question rows | `[{"question_id":"q-001",...}]` |
| `dashboard_panels` | array of panel rows | `[{"panel_id":"flow",...}]` |

### Entity and mention rows

An entity row defines a true identity. Its `mention_ids` are the authoritative
true merges, including noisy variants.

| Field | Type | Example |
| --- | --- | --- |
| `entity_id` | string | `"ent-001"` |
| `entity_type` | enum: `person`, `organization` | `"person"` |
| `canonical_name` | string | `"Mara Venn"` |
| `scheme_role` | enum: `participant`, `nonparticipant`, `shell`, `operating_business` | `"participant"` |
| `mention_ids` | array of strings | `["mention-001","mention-014"]` |

A mention row is stored with extracted chunks and is referenced by the entity
row: `mention_id` (string, `"mention-014"`), `source_id` (string,
`"src-mail-003"`), `chunk_id` (string, `"chunk-009"`), `surface` (string,
`"M. Ven"`), and `start` and `end` (integer character offsets, `18` and `24`).

### Account rows

| Field | Type | Example |
| --- | --- | --- |
| `account_id` | string | `"acct-001"` |
| `owner_entity_ids` | array of strings | `["ent-001"]` |
| `bank_id` | string | `"bank-cinder"` |
| `canonical_number` | synthetic digit string | `"7712048816"` |
| `display_variants` | array of strings | `["****8816","8816"]` |
| `opened_at` | timestamp | `"2024-01-08T00:00:00Z"` |
| `closed_at` | timestamp or null | `null` |

### Transaction and locator rows

| Field | Type | Example |
| --- | --- | --- |
| `transaction_id` | string | `"txn-001"` |
| `posted_at` | timestamp | `"2024-04-03T14:10:00Z"` |
| `amount` | decimal-string | `"7425.00"` |
| `currency` | three-letter synthetic currency code | `"XCU"` |
| `from_account_id` | string or null | `"acct-001"` |
| `to_account_id` | string or null | `"acct-009"` |
| `memo` | string | `"materials 41"` |
| `locator` | ledger locator or scan locator | `{"source_id":"src-ledger-01","sheet":"April","row":42}` |

A ledger locator has `source_id` (string, `"src-ledger-01"`), `sheet` (string,
`"April"`), and `row` (positive integer, `42`). A scan locator has `source_id`
(string, `"src-scan-01"`), `page` (positive integer, `3`), and `region_box`
(four-number array, `[0.10,0.22,0.91,0.27]`). Exactly one locator form applies.

### Money-path rows

| Field | Type | Example |
| --- | --- | --- |
| `path_id` | string | `"path-001"` |
| `pattern_type` | enum: `layering`, `round_trip`, `structuring`, `fan_in`, `fan_out`, `rapid_in_out`, `no_business_purpose`, `dormant_then_active` | `"layering"` |
| `hop_transaction_ids` | ordered array of 3 to 6 strings for layering; ordered nonempty array otherwise | `["txn-001","txn-002","txn-003"]` |
| `origin_entity_id` | string | `"ent-001"` |
| `destination_entity_id` | string | `"ent-006"` |

### Timeline-link rows

| Field | Type | Example |
| --- | --- | --- |
| `link_id` | string | `"link-001"` |
| `event_a_id` | string | `"event-email-07"` |
| `event_b_id` | string | `"event-transfer-11"` |
| `relation` | enum: `enables` | `"enables"` |
| `source_ids` | nonempty array of strings | `["src-mail-003","src-ledger-01"]` |

### Anomaly rows

| Field | Type | Example |
| --- | --- | --- |
| `anomaly_id` | string | `"anom-001"` |
| `type` | same pattern enum as `pattern_type`, plus `inconsistent_statement` and `misleading_statement` | `"inconsistent_statement"` |
| `subject_ids` | nonempty array of entity, account, transaction, or event ids | `["ent-001","event-email-07"]` |
| `source_ids` | nonempty array of strings | `["src-mail-003","src-interview-02"]` |
| `explanation` | string | `"The two synthetic dates conflict."` |

### Fact rows

| Field | Type | Example |
| --- | --- | --- |
| `fact_id` | string | `"fact-021"` |
| `fact_type` | string | `"event_sequence"` |
| `value` | string, number, boolean, or null | `"event-email-07 enabled event-transfer-11"` |
| `subject_ids` | nonempty array of entity, account, transaction, or event ids | `["event-email-07","event-transfer-11"]` |
| `source_ids` | nonempty array of strings | `["src-mail-003","src-ledger-01"]` |

### Question and decisive-passage rows

Question shape follows the evaluation suite's distinction between required
facts, support, forbidden facts, and abstention.

| Field | Type | Example |
| --- | --- | --- |
| `question_id` | string | `"q-001"` |
| `question_type` | string | `"cross_document_chronology"` |
| `text` | string | `"Which event enabled transfer txn-011?"` |
| `required_fact_ids` | array of strings | `["fact-021"]` |
| `forbidden_fact_ids` | array of strings | `["fact-099"]` |
| `expect_abstention` | boolean | `false` |
| `decisive_passages` | array of decisive-passage rows | `[{"fact_id":"fact-021","chunk_id":"chunk-009","source_id":"src-mail-003","support_text":"Send after the notice."}]` |

A decisive-passage row has `fact_id` (string, `"fact-021"`), `chunk_id`
(string, `"chunk-009"`), `source_id` (string, `"src-mail-003"`), and
`support_text` (exact nonempty string, `"Send after the notice."`). Each required
fact has at least one decisive passage. An abstention question has no required
facts or decisive passages.

### Dashboard-panel rows

| Field | Type | Example |
| --- | --- | --- |
| `panel_id` | string | `"flow"` |
| `title` | string | `"Account flow"` |
| `fact_ids` | ordered array of strings | `["fact-021","fact-022"]` |
| `display_values` | object from stable display key to string, number, boolean, or null | `{"hop_count":3,"total":"7425.00"}` |
| `source_ids` | nonempty array of strings | `["src-ledger-01"]` |

This row states exactly which facts and values each dashboard panel should show.
It does not prescribe layout, ranking, or a product implementation.

## Determinism and scale

Given the same generator version, seed, and scale, generation order,
serialization, archive timestamps, workbook metadata, PDF metadata, and line
endings are fixed, producing the same bytes and hashes. Randomness comes only
from a seeded generator whose algorithm is versioned. Sorted iteration and
canonical JSON serialization prevent platform-dependent ordering.

The `ci`, `10k`, `100k`, and `1m` variants preserve the same entity identities,
planted transactions, paths, timeline links, questions, and decisive passages.
The numeric variants target their named transaction count. Each larger variant
adds deterministic neutral entities, documents, and transactions around the
same case, without duplicating or modifying planted facts. The manifest records
requested and actual counts so measured recall can be compared by scale.

## Prohibited generator inputs and outputs

The generator must never use real people's names, real banks, account-number
formats copied from a real institution, or places tied to real events. It must
not copy real case material or private deployment details. Invented values are
checked against a maintained synthetic namespace and generated from bundled
word lists, not network data. The design adds neither a license gate nor a PST
container.

## Open questions for the maintainer

These choices remain open and are not decided by this note:

1. Which invented currency code should all examples and fixtures use?
2. Should a corpus contain one synthetic threshold or multiple thresholds that
   vary by invented institution and date?
3. Should `gold.jsonl` contain one envelope or one typed record per line?
4. Which exact transaction and document counts define the `ci` variant?
5. Should neutral-noise ratios be fixed across source formats or only in total?
6. Which dashboard panels are in the first measured acceptance set?
7. Is a patch-version change sufficient for additive answer-key fields?
