# Assistant actions — brief (increment 5, proposed)

Status: **approved October 3, 2026** with the recommended answers below; each
increment (A, B, C) lands in its own PR under the
[review policy](../QUALITY_GATES.md#review-policy).
Scope: the matter assistant dock and the Ask page. No schema change in A or B;
C adds one bounded generation mode and needs its own evaluation.

## Outcome

The matter assistant helps a reviewer do the next useful thing, not only quote
sources: it offers actions that fit what is open, every write is a proposal the
reviewer accepts, and everything it drafts stays source-grounded under the same
checks as answers.

## Problems from the synthetic walkthrough

- Reviewers do not know what to ask; the three empty-state prompts ignore what is
  open (a source, an identity, the timeline).
- The assistant only cites. Turning an answer into work (notes, suggestions,
  questions for a witness) means copying text by hand.
- The collapse control is an icon-only chevron (`workbench_assistant.html`), easy to
  miss and unlabelled visually.
- Nothing helps prepare interview or discovery questions from the record.

## What exists today (baseline)

- Dock: ask with a source-set scope, saved chats, job status and recovery, cited
  claims with **Save passage to case notes** (saved as Suggested), and three
  suggested prompts that fill the box without submitting. Open/collapsed state and
  the selected chat are remembered per browser (localStorage).
- Answers: `GroundedGenerationService` asks the model for a fixed JSON shape
  (`answerable`, `claims`, `limitation`, `missing_information`); `_verify_text`
  keeps a claim only if it cites 1–4 passages of one kind, its quotes and numbers
  appear in them, content overlap is at least 60% (45% for transcripts) and no
  negation mismatch is found. Unsupported text is dropped with a notice; one
  repair pass; otherwise the answer is saved as not supported.
- Models: Qwen3.5-4B (portable) and Qwen3.5-9B (quality), pinned revisions,
  Apache-2.0. **No tool or function calling exists anywhere**; the closest analogue,
  the investigation planner, is deliberately limited to fixed-key proposals with no
  tools or scope authority.
- Suggestions inbox (3b), automatic discovery (3a) and the found-dates draft (3c)
  already turn passages into reviewable suggestions without a model.

## Decision 1 — who chooses an action (recommended: the interface)

**Recommended:** actions are offered by the interface from what is open and from
each verified claim; the model only *drafts text* inside the existing grounded
schema. No model-chosen tool use.

- Why: neither configured model has been evaluated for tool selection, and none of
  the codebase uses it. Model-chosen actions would need a new structured schema, a
  representative tool-selection evaluation and possibly a different model — under
  `AGENTS.md` that means an exact revision, license record, offline readiness test
  and evaluation before it can ship. The interface-offered design delivers most of
  the value with none of that risk.
- Alternative (not recommended now): let the model pick from a fixed action list
  via a constrained JSON field, behind evaluation. Revisit after A–C ship.

## Decision 2 — confirmation model

- Every write is a **proposal card**: what will be created, from which passages,
  with **Save** and **Dismiss**. Nothing is saved until the reviewer saves it.
- Everything saved lands **Suggested** (notes) or in the **suggestions inbox**
  (identities, dates); nothing confirms a claim, identity or event.
- Existing authorization, CSRF, matter membership, source guard and audit apply;
  each save records one audit event naming what it created.
- A proposal whose passages changed since it was drafted is refused on save with
  the existing "changed or unavailable" response.

## Decision 3 — grounding

- Drafted statements use the existing claim verification unchanged.
- Drafted *questions* (C) are not factual claims, so they get their own bounded
  check: each question must cite 1–4 passages it is about; any quoted text and
  numbers must appear in them; questions that cite nothing or quote text that is
  not there are dropped with the same omission notice. Wording is reviewer-owned
  once saved.

## Proposed increments (each its own PR, browser acceptance, synthetic tests)

**A. Dock basics (small, no model change).**
- A visible **Close** text button (keep the chevron as an icon beside the label).
- Context-aware suggested prompts. With a ready source open — "Summarize this
  source", "Who and what appears here", "What dates appear here"; choosing one
  first applies the existing **Ask using this source** single-source scope, so the
  question really is about that source. Elsewhere the general prompts remain.
  Prompts still fill the box; they never submit on their own. (As built: the dock
  is not shown on identity or timeline pages, so prompts for those pages are not
  part of A.)
- Keyboard and screen-reader pass on the dock (focus order, names, collapsed tab).

**B. Proposal cards on verified claims (no model change).** Beside the existing
**Save passage to case notes**:
- **Suggest people, things and dates from this passage**: runs the same
  deterministic extractor as automatic discovery on the cited passages and adds
  any new suggestions to the inbox (Suggested, provenance "from an answer's cited
  passage"); shows how many were added and links to the inbox.
- **Open in timeline draft**: when a cited passage contains a found date, link to
  that entry in the 3c draft.

As built: both are one action. Each claim offers **Suggest people, things and
dates from this passage**, which checks every cited passage with the rules saving
a passage uses (any change refuses the claim), then shows a proposal of what it
found with **Add to suggestions** and **Dismiss** (decision 2). Add re-checks
that the passages are the ones previewed and writes them in one transaction.
New identities are Suggested, created by the reviewer, and their first history
entry is "suggested from an answer passage"; the inbox marks them **From an
answer**. Occurrences share discovery's receipts, so neither path adds what the
other already found. The result names how many were added, links to the inbox,
and links to **Dates found in sources** on the timeline only when it added a date
(the draft has no per-date anchor).

**C. Draft interview or discovery questions (new generation mode).**
- From selected passages or a saved note set: "Draft questions for a witness" or
  "Draft discovery requests about this". Output: up to 8 questions, each citing the
  passages it is about, verified as in Decision 3, shown as a proposal card and
  saved as one Suggested note.
- Needs a fixed synthetic evaluation set meeting the pass bar under maintainer
  decisions below.

Deferred: proposing timeline events (needs an anchor decision, as found in 3c),
model-chosen actions, cross-matter anything.

## Maintainer decisions — October 3, 2026

The maintainer approved the recommendations, preferring usefulness and planning to
adjust after use:

1. **Interface-offered actions** (Decision 1). The model only drafts text inside
   grounded schemas; it does not choose tools.
2. **C is in scope**, after A and B. Pass bar on a fixed synthetic set: at least
   90% of drafted questions cite a relevant passage, and none quotes text that is
   not in its cited passage. The receipt is recorded in C's PR.
3. **Passage-derived suggestions keep their own provenance** in the suggestions
   inbox ("from an answer's cited passage"), distinct from automatic discovery.
4. **No additions or removals** to A–C for now.

## Acceptance (per increment)

Synthetic regressions for every write path (proposal → save → audit; changed
passage refused; dismissed proposal saves nothing), the dock journeys at phone and
desktop widths with no overflow, keyboard-only operation, and for C a recorded
evaluation receipt on the synthetic set. Follow the
[review policy](../QUALITY_GATES.md#review-policy) before merge.
