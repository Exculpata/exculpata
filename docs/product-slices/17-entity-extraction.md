# 17: Improve entity discovery and reviewer-controlled reconciliation

Status: Done. [PR #79](https://github.com/Exculpata/exculpata/pull/79)
landed at `e9ab89c792b562c2abf27715d15b5f70d3216900` on September 12.
Slice 16 landed in #77. Storage/behavior contract: #78. See the
[cruise receipt](../EXIT_ALPHA_CRUISE.md#historical-post-17-receipt-superseded-snapshot) for exact-head
CI/review, the security-quota exception and local synthetic evidence.

## Finding and outcome

The pre-17 notebook patterns look for titled people, capitalized places after
selected prepositions, and a limited set of date formats. Notebook suggestions stop at
75 new items by default or 2,500 scanned units. They do not provide broad
people/organization/object discovery or a complete set of mentions.

## Small implementation

Run entity extraction incrementally over the durable unit coverage from 12,
saving all supported mentions rather than only new normalized labels. First
support untitled people, organizations, and explicit identifiers/objects on a
bounded synthetic corpus. Store extractor version, source support, and review
status. Keep failed/unprocessed units visible.

Add reviewer-confirmed alias links and merge/split with undo. Machine matching
proposes candidates; shared names, similar spellings, or transcript speaker
clusters do not establish identity. Preserve distinct mentions when rejecting
or reversing a suggested merge. Date normalization needs ambiguity and timezone
fields, not a guessed single timestamp.

## Code and acceptance

Build on entity/mention storage from 16 and existing source unit/analysis jobs.
Keep the extractor behind a replaceable interface; retain simple deterministic
identifier extraction where appropriate. A new learned model requires pinned
revision, licensing, offline readiness, and evaluation before adoption.

Measure mention recall and false merges for untitled names, aliases, same-name
people, multilingual/OCR variation, and more than 75 entities. Verify resume,
source replacement, review corrections surviving re-extraction, and merge/split
undo. Persistence changes need migration and clean-restore evidence.

## Implementation contract

See [entity discovery](../ENTITY_DISCOVERY.md) for the implemented processing,
reviewer decisions, deterministic recognition limits and recovery contract.
The brief above preserves the intended acceptance scope. The landed extractor
is deterministic with limited recall, not general multilingual NER. Visited-unit
coverage and bounded positive fixtures do not prove that every mention was found.
Entity records do not yet feed automatic Report compilation.
