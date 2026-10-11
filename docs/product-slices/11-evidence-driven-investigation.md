# 11: Let investigation follow evidence within a budget

Status: implemented on `main` as
[#66](https://github.com/Exculpata/exculpata/pull/66); validation
described in [the implementation guide](../EVIDENCE_DRIVEN_INVESTIGATION.md).
Depends on 00 and 10.
Configured-model quality evaluation remains separate from deterministic acceptance.

## Finding and outcome

`_research_plan` appends chronology, corroboration, conflicts, and gaps wording
to the same question. It does not revise searches from prior findings. Repeated
generic queries can retrieve the same small part of a large record.

## Small implementation

Keep the initial pass, then generate a small queue of follow-up searches from
supported names, dates, identifiers, unresolved questions, and competing accounts.
Record why each search was proposed and which source finding motivated it.
Deduplicate query/evidence work and stop on explicit pass/time/evidence budgets
or measured lack of new evidence. Let a reviewer inspect the plan and continue
from a checkpoint with an explicit additional budget.

The planner can propose queries only. The application owns scope, authorization,
budgets, and tool execution; text in an uploaded source cannot issue commands.
Use bounded structured proposals with rejection of unsupported scope changes.

## Code and acceptance

Start with `_research_plan`, `_process_research_job`, `workflow_jobs.py`, saved
research plans/checkpoints in `workspace_store.py`, and research progress UI.

Use a synthetic multi-step record where the first source names an identifier
needed to find a second source and a contradiction appears in a third. Verify
new evidence is reached, search reasons are saved, repeated proposals stop,
cancel/resume works, and changed sources invalidate dependent findings.
Evaluate the configured local model separately from deterministic planner tests.
Final synthesis remains explicitly bounded until 13; do not claim this change
alone reads the whole matter.
