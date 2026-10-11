# 18: Connect entities through reviewable events and assertions

Status: **Done — narrow manual events/assertions workflow.**
[PR #82](https://github.com/Exculpata/exculpata/pull/82) landed at
`19d57fbec29325ae9b5ab296e6a2adcb18a5e0ec` on September 12, 2026. The
[post-18 receipt](../EXIT_ALPHA_CRUISE.md#historical-post-18-receipt-superseded-snapshot)
records final-head acceptance and passed independent post-merge checks,
including the verified security-quota exception. Depends on landed 13 and 17;
contract [#80](https://github.com/Exculpata/exculpata/issues/80).
Automatic relationship inference and a visual graph remain outside this scope.

## Finding and outcome

`matter_analysis.py` compares shared pattern-matched references and near-identical
assertions with negation removed. These are useful leads, but they are not a
relationship model or comprehensive contradiction analysis. Shared appearance
in a document must not become a claimed real-world relationship.

## Small implementation

Add one focused relationship workflow: create an event or assertion with typed
entity roles, date/uncertainty, supporting passages, competing passages, and
review status. Let reviewers confirm, dispute, correct, and remove it. Show
these records on the entity page and chronology before building a graph view.

Machine proposals may come from the findings produced by 13, but must retain
original source support and the asserted speaker/source. Separate "this source
claims X" from "the reviewer accepts X". Keep co-mentions labeled as co-mentions.
No automatic inference of identity, guilt, intent, or causal relationships.

## Code and acceptance

Build on entity/mention records, `matter_analysis.py` findings, human review
status, source citations, and Reports/chronology export paths.

Synthetic acceptance contains two incompatible accounts of one event, an
unrelated same-name person, an uncertain date, and two co-mentioned people with
no stated relationship. Verify all interpretations remain distinguishable,
each assertion resolves to its support, corrections survive reruns, and exports
retain the disagreements. UI acceptance uses entity detail and chronology;
visual graph layout is a later slice. Supply schema migration, backup/restore,
and rollback evidence before release.

## Implementation contract

The manual workflow, source and revision boundaries, exports, limits and
migration/restore/rollback instructions are recorded in
[EVIDENCE_ASSERTIONS.md](../EVIDENCE_ASSERTIONS.md). The [review policy](../QUALITY_GATES.md#review-policy)
applies separately from local synthetic verification.
