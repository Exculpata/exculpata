# Drafting witness questions and discovery requests

Increment C of the [assistant actions brief](product-slices/assistant-actions.md).
Under each generated answer, the assistant dock and the full conversation offer
**Draft from this answer's passages: Questions for a witness** and **Discovery
requests**. The configured local generator drafts up to eight questions from the
passages the answer cites; nothing is saved until the reviewer chooses **Save to
case notes**, which adds one Suggested note.

## What is sent and what is shown

- **Input.** Only the answer's distinct cited passages, including those supporting a
  source-backed limitation shown with the answer (up to 12, within the usual
  evidence limits), checked together in one bounded pass with the rules saving a
  passage uses, plus the reviewer's question that the answer replied to as the
  topic. If any cited passage changed or is unavailable, nothing is drafted. The
  model reads each passage's display text (up to 6,000 characters). Passages are
  read under the source and workspace guards, which are released before the model
  is called. Afterwards they are read again under the guards with the reviewer's
  membership; if access was revoked or any passage changed during the call, the
  draft is not shown.
- **Model contract.** A dedicated JSON schema (`questions`: up to eight, each with
  `text` and one to four `evidence_ids`) and prompt in
  `src/case_intelligence/question_drafting.py`. Source text and the topic are
  treated as untrusted data. The model does not choose actions or tools. Machine
  transcript passages are labelled as such, and the prompt says transcript wording
  and speaker labels such as "Speaker 1" are not established events or identities,
  so questions about them should ask what the recording appears to say without
  leading.
  The prompt asks for four to six distinct, relevant questions and at least three
  when supported, with no padding from unrelated passages or invented records.
  It preserves attribution, conditional statements and the difference between
  an unestablished fact and an established opposite. Nearby objects do not prove
  possession; unidentified transcript speakers do not become identified witnesses.
- **Check (decision 3).** A question is shown only if it cites one to four of the
  supplied passages, shares at least one content word with them, and every quoted
  span (of any length, in double or single quotes, including angle, low-9, CJK
  corner, fullwidth, reversed and ornamental quotation marks, with any other
  unpaired quotation punctuation, such as an ornament, treated as unclosed; quotations
  are paired with a stack, so an outer quotation is checked whole, including any
  quotation nested inside it, whatever marks each uses) appears as whole words within
  one cited passage (not assembled across two), as does every number. Every word
  containing a digit (a date, a time, an identifier such as `K7`, `K-7`, `K_7` or
  `5A`, an ordinal such as `4th`) must also appear
  as a whole word in a cited passage, so an altered identifier is dropped, and so
  is an internal passage label such as `S1` written without brackets, unless the
  passage itself contains it. Leading list markers such as "1." are removed
  first. A single quote (straight, or either curly mark) opens a quotation wherever it
  does not follow a letter, digit or another apostrophe, so possessives and contractions outside a
  quotation are not treated as quotations, while a quotation containing one
  (`'it's red'`) is checked whole. Curly and straight apostrophes compare equal. A
  word-initial elision such as `’90s`, `'em` or `'til` is prose, not an unclosed
  quotation, but quotations are also checked as if it were an opener, so it can
  never hide one.
  A quotation mark that is never closed fails the check, since its quotation cannot
  be compared with the passage.
  Questions that fail are dropped and the draft says some were omitted; if none
  pass, nothing is shown and the reviewer can try again. Questions that differ only
  in case, spacing or punctuation (underscores included) count once.
  A gendered pronoun absent from all cited excerpts is rejected. Repeated
  questions with the same cited basis and only a small added qualifier are
  collapsed; substitutions, distinct opening questions and negation remain
  distinct. These are narrow text checks, not a semantic relevance or entailment
  guarantee. Review the actual passages for unsupported assumptions and uncertainty.
  If the first model draft retains fewer than three questions, the adapter makes
  at most one repair request for a complete replacement, with the original
  passages and feedback about the required checks. It never pads a draft or
  samples repeatedly until the gate passes. The replacement is checked normally;
  fewer than three supported questions still fails the unchanged evaluation gate.
- **Proposal and save (decision 2).** The dock shows a proposal card that takes
  focus, lists each question with its sources, and offers **Save to case notes**
  and **Dismiss**. While a save is pending, Dismiss and the draft buttons are
  held, so the card never says nothing was saved while the save may succeed. The
  conversation picker and New chat are held until every pending draft and save
  (from any open card) is known, so the dock is never replaced before a result
  shows. A dock
  refresh that arrives meanwhile (for example when an answer finishes) waits for
  those saves, and for any draft in flight, and keeps their results and case-notes
  link; an open, unsaved draft stays open in the refreshed dock, and if newer
  messages would push its answer out of the dock's recent messages, that answer is
  kept at the top of the refreshed thread. Switching to another conversation never
  carries a draft into it. No save or draft
  starts while a refresh is in flight. While
  a replacement draft is pending, the open card's Save and Dismiss are held, so no
  save is lost when the card is replaced. Without JavaScript, and from the full conversation, the draft is
  its own page with the same choices. The draft carries a basis, a digest of the
  exact passages it was made from, and saving requires the answer's current
  passages to match it, so a corrected transcript that keeps its passage identity
  still refuses an older draft. Saving also checks every question again against
  the current passages; if a cited passage changed or a question no longer passes,
  nothing is saved. The note is a Suggested `note` with origin
  `answer`, the questions and their sources as its body, the cited passages as its
  references (up to 12), and the answer as its origin. Saving the same draft again,
  with the same questions citing the same passages, does not add a second note. Wording is the reviewer's to edit once saved.
- **Audit.** `answer.draft_questions` records each successful draft and the number
  of questions shown; `notebook.capture_questions` records each save. Both
  routes require the session CSRF token and current matter membership. The
  passage selection, draft and save handlers, and those routes live in
  `src/case_intelligence/answer_question_drafts.py`. No schema or storage
  change is involved.

## Question generation profiles

Question drafting has its own generation policy in `question_generation.py`.
It does not change ordinary answering or source-classification defaults, select
a model, alter model pins, or impose a model-size limit.

Set `CASE_INTELLIGENCE_GENERATOR_QUESTION_PROFILE` (default `portable`). Initial
capacities are configurable defaults, not maximum supported hardware sizes:

| Policy | Thinking | Planning tokens | Final answer tokens | Ollama context | Per-call timeout |
| --- | --- | ---: | ---: | ---: | ---: |
| portable | off | unused | 1,200 | 8,192 | existing generator timeout |
| quality | on | 4,096 | 2,400 | 32,768 | 300 seconds |
| large | on | 8,192 | 4,096 | 65,536 | 600 seconds |

Override a selected policy with
`CASE_INTELLIGENCE_GENERATOR_QUESTION_<PROFILE>_THINKING` (`0`, `1`, `false`,
`true`), `_THINKING_TOKENS`, `_ANSWER_TOKENS`, `_CONTEXT_TOKENS`, and
`_TIMEOUT_SECONDS`. `<PROFILE>` is the uppercase policy name, such as `QUALITY`.
Additional lowercase identifier names inherit quality defaults and accept the
same overrides. Token/context values must be positive integers and timeouts
positive and finite; these settings are not clamped to the portable capacities.
An OpenAI-compatible server controls its context window through its own model
configuration; the context request override applies to Ollama.

The existing explicit `CASE_INTELLIGENCE_GENERATOR_DISABLE_THINKING=1` opt-out
also forces question thinking off. It retains its existing effect on other
OpenAI-compatible generation calls; no new default is applied to other features.

Thinking-enabled drafting uses two sequential local requests: a bounded planning
request with thinking enabled, then a request with thinking disabled and its own
final-answer allocation. A planning length stop cannot consume that allocation.
Earlier model notes are labelled untrusted and are not evidence, not returned to
the reviewer, and not saved to the matter. The final answer still passes the
same citation/text checks. This costs two requests, plus at most one replacement
request if too few questions pass, and does not promise that
either model output is correct. Transport errors still fail explicitly.

This separate request is the answer reserve: neither adapter pretends that
adding tokens to a shared thinking/completion limit reserves a final answer.
Runtime/model compatibility and quality must be evaluated on the chosen profile;
unit transport tests are not real-model or larger-hardware acceptance.

## Evaluation and pass bar

[`benchmarks/question-drafts-v1.json`](../benchmarks/question-drafts-v1.json) is a
fixed synthetic set of 10 cases (five witness, five discovery), including two
machine-transcript cases. Each case pairs passages about the topic with an
unrelated distractor and lists the passages a relevant question may cite. Its
SHA-256 is pinned in `question_draft_evaluation.py`
(`7674d1da108db0dffe5392a7ca841c483e09e9ec220838a1afac5b5f99a5ea95`); changing
the set means a new file and a new recorded fingerprint.

The maintainer's pass bar, applied to the questions a reviewer would see:

- at least 90% cite only relevant passages (a question that also cites the
  distractor does not count);
- none quotes text that is not in its cited passages; and
- so that dropping questions cannot pass on its own, every case shows at least
  three questions.

The receipt also counts quotations the raw model output invented before the check
removed them. A raw reply whose citations are malformed is scored as shown
nothing, rather than stopping the run.

Run it against the configured generator (the same `CASE_INTELLIGENCE_GENERATOR_*`
settings the application uses; nothing is selected or downloaded):

```bash
.venv/bin/python scripts/evaluate-question-drafts.py --output /tmp/question-drafts-receipt.json \
  --require-model --profile portable --runtime-profile /tmp/runtime.json
```

`/tmp/runtime.json` is the runtime-profile declaration described in
[CLAIM_EVALUATION.md](CLAIM_EVALUATION.md): the model artifact digest (for Ollama,
its immutable tag digest) and the selected profile's exact upstream model,
revision and license from `config/models.json`. A profile that is malformed or
contradicts the pin is refused before any model call. Keep it, and the output
receipt, outside the checkout, or the checkout counts as having uncommitted changes
and the receipt is unbound.

Without a configured runtime the receipt records `model_gate: outstanding`.

The receipt is bound to what ran. It records the Git commit, whether the checkout
had uncommitted changes, and SHA-256 hashes of the drafting check, the model
adapters and the scorer. It also records the model's content digest as Ollama
reports it, observed before and after the run; a model name alone is mutable. The
`model_pin` records the selected profile's upstream model, revision and license,
the model catalog's SHA-256, and the declared artifact; like the claim
evaluation, that linkage is an operator declaration, not an attestation of loaded
weights. The gate is `passed` only when the pass bar is met on the pinned set,
from a clean checkout at a recorded commit, with the same digest before and after,
equal to the artifact the runtime profile declares. A passing score without that
binding is recorded as `unbound` with its reasons, and a runtime that reports no
digest cannot pass. No hostname, user, endpoint or path is recorded. The receipt is
strict JSON: text that is not valid UTF-8 is escaped, and non-finite numbers from a
runtime that ignores the schema are kept as text. It is published atomically at a
new path; an existing or concurrently created file is never replaced.
`tests/test_question_draft_evaluation.py` checks the set and the scoring with
deterministic clients; `tests/test_question_drafts.py` covers the check, routes,
saving and refusals; the `assistant-dock` browser journey covers the dock card
with a deterministic synthetic drafting client.
