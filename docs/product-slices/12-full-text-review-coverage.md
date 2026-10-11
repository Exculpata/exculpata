# 12: Process every extracted unit in a frozen source population

Status: implemented on `main` as **Review all extracted text** via
[#59](https://github.com/Exculpata/exculpata/pull/59) (the #47 / #58
family). See [full-text review](../FULL_TEXT_REVIEW.md). Depends on 00 and 10.

## Finding and outcome

`_process_review_decision` searches within each frozen source and sends at most
12 matching passages to classification. No-match sources correctly remain
unresolved, but visiting every source is still not reading every extracted unit.
A late decisive passage can be missed by passage selection.

## Small implementation

Add a resumable sequential review job over every eligible extracted unit in
the frozen source/version population. Batch deterministically within model
budgets, persist processed/failed/pending unit states, and retain citation-backed
findings before moving to the next batch. First deliver the coverage ledger and
unit findings; cross-source synthesis belongs to 13.

Keep extraction coverage separate from analysis coverage: unavailable OCR pages,
unprocessed attachments, and untranslated/untranscribed media remain visible.
Completion means all eligible units were attempted and each outcome recorded;
it does not mean every relevant fact was recognized. A source replacement
invalidates work derived from the old version rather than being silently mixed.

## Code and acceptance

Start with `workflow_jobs.py::ReviewCoordinator`, `_process_review_decision`,
source-unit storage, and review run/decision tables in `workspace_store.py`.
Preserve frozen criteria, source digests, and human adjudications.

Fixture: one source with more than 12 units, a decisive last-unit fact, a failed
unit, and a changed source during execution. Verify no silent unit omission,
bounded memory, restart without duplicate findings, cancellation, revocation,
and correct UI/export denominators. Add migration, backup/clean-restore and
rollback evidence for durable checkpoints. Real-model recall remains a separate
acceptance measurement.
