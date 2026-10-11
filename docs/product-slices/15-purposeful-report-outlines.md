# 15: Automatically compile useful reports from saved review

Status: implemented on `main` via
[#59](https://github.com/Exculpata/exculpata/pull/59)
([#44](https://github.com/Exculpata/exculpata/pull/44) /
[#56](https://github.com/Exculpata/exculpata/pull/56)). See
[report compilation](../REPORT_COMPILATION.md). Depends on 14; integrates
deeper synthesis from 13 later.

## User outcome

A reviewer should choose a Timeline, People/Places/Things report, or Topic brief
and receive a substantive draft built from saved AI assistance and human review.
Manual assembly of arbitrary sections or a blank outline is not the primary flow.
A first-time reviewer should understand how to begin without training.

Start from contextual Make a report actions or the Reports page. Preselect a
reasonable set of saved work, allow changes, ask for the topic when relevant,
and compile in the background. Open the result as a readable document with
source links, then let the reviewer edit, reorder, and export it.

## Portable contract

The report preserves the original source/version support, human notes and
adjudications, disagreements, missing evidence, and the scope of selected work.
Topic selection must not dump unrelated notes into the report. A model may
classify which original human records are relevant; it must not turn rewritten
classification prose into a source fact. Similar names remain separate mentions
unless independently reconciled.

Use the existing independently verified answer service and configurable server
budgets. Record omitted work and unavailable generation honestly. Reject stale
inputs before atomic save. Durable jobs need cancellation, retry, current
membership, lease fencing, idempotency, and backup/clean-restore evidence.
Technical provenance can be collapsed while reading and editing, but it must
remain attached to the exported document.

## Acceptance and limits

Synthetic tests must cover all three report types, relevant and unrelated human
notes, supported and unsupported source claims, changed inputs, duplicate
requests, cancellation/recovery, source links, human edits, Word/Markdown, and
long canonical excerpts. Walk the actual browser flow at desktop and mobile
widths. Evaluate the configured local model separately from deterministic tests.

This slice compiles selected saved work. It does not by itself review every page,
perform persistent entity reconciliation, or remove the upstream synthesis
bottleneck. Those are separate capabilities with their own evidence.
