# Drafting witness questions and discovery requests

Increment C of the [assistant actions brief](product-slices/assistant-actions.md).
Under each generated answer, the assistant dock and the full conversation offer
**Draft from this answer's passages: Questions for a witness** and **Discovery
requests**. The configured local generator drafts up to eight questions from the
passages the answer cites; nothing is saved until the reviewer chooses **Save to
case notes**, which adds one Suggested note.

## What is sent and what is shown

- **Input.** Only the answer's distinct cited passages (up to 12, within the usual
  evidence limits), checked together in one bounded pass with the rules saving a
  passage uses, plus the reviewer's question that the answer replied to as the
  topic. If any cited passage changed or is unavailable, nothing is drafted. The
  model reads each passage's display text (up to 6,000 characters). Passages are
  read under the source and workspace guards, which are released before the model
  is called.
- **Model contract.** A dedicated JSON schema (`questions`: up to eight, each with
  `text` and one to four `evidence_ids`) and prompt in
  `src/case_intelligence/question_drafting.py`. Source text and the topic are
  treated as untrusted data. The model does not choose actions or tools.
- **Check (decision 3).** A question is shown only if it cites one to four of the
  supplied passages, shares at least one content word with them, and every quoted
  span and number in it appears in a cited passage. Leading list markers such as
  "1." are removed first, and possessives are not treated as quotations.
  Questions that fail are dropped and the draft says some were omitted; if none
  pass, nothing is shown and the reviewer can try again.
- **Proposal and save (decision 2).** The dock shows a proposal card that takes
  focus, lists each question with its sources, and offers **Save to case notes**
  and **Dismiss**. While a save is pending, Dismiss and the draft buttons are
  held, so the card never says nothing was saved while the save may succeed. Without JavaScript, and from the full conversation, the draft is
  its own page with the same choices. Saving checks every question again against
  the answer's current passages; if a cited passage changed or a question no
  longer passes, nothing is saved. The note is a Suggested `note` with origin
  `answer`, the questions and their sources as its body, the cited passages as its
  references (up to 12), and the answer as its origin. Saving the same draft again
  does not add a second note. Wording is the reviewer's to edit once saved.
- **Audit.** `answer.draft_questions` records each successful draft and the number
  of questions shown; `notebook.capture_questions` records each save. Both
  routes require the session CSRF token and current matter membership. No schema
  or storage change is involved.

## Evaluation and pass bar

[`benchmarks/question-drafts-v1.json`](../benchmarks/question-drafts-v1.json) is a
fixed synthetic set of 10 cases (five witness, five discovery), including two
machine-transcript cases. Each case pairs passages about the topic with an
unrelated distractor and lists the passages a relevant question may cite. Its
SHA-256 is pinned in `question_draft_evaluation.py`
(`7674d1da108db0dffe5392a7ca841c483e09e9ec220838a1afac5b5f99a5ea95`); changing
the set means a new file and a new recorded fingerprint.

The maintainer's pass bar, applied to the questions a reviewer would see:

- at least 90% cite a relevant passage;
- none quotes text that is not in its cited passages; and
- so that dropping questions cannot pass on its own, every case shows at least
  three questions.

The receipt also counts quotations the raw model output invented before the check
removed them.

Run it against the configured generator (the same `CASE_INTELLIGENCE_GENERATOR_*`
settings the application uses; nothing is selected or downloaded):

```bash
.venv/bin/python scripts/evaluate-question-drafts.py --output question-drafts-receipt.json --require-model
```

Without a configured runtime the receipt records `model_gate: outstanding`.
`tests/test_question_draft_evaluation.py` checks the set and the scoring with
deterministic clients; `tests/test_question_drafts.py` covers the check, routes,
saving and refusals; the `assistant-dock` browser journey covers the dock card
with a deterministic synthetic drafting client.
