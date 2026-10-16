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
  The prompt targets three distinct, relevant questions, allows one or two when
  no further supported angle is produced, and permits additional useful angles
  up to the existing eight-item limit. No padding from unrelated passages or invented records.
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
  one cited passage (not assembled across two), as does every factual number. Every word
  containing a factual digit (a date, a time, an identifier such as `K7`, `K-7`, `K_7` or
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
  Transcript items must explicitly frame what the recording/transcript appears
  to say (or ask about what a record says), and must not substitute an absent
  witness/interviewee identity. Transcript discovery requests include `if any`.
  For passages with multiple labelled speakers, a question about their accounts
  must keep a specific label. A further audit rejects quotations borrowed from a
  different speaker, detectable cross-speaker details, and overlapping claims
  that lose their negation or condition. Generic references to another cited
  document do not become transcript claims solely because both are cited.
  These wording checks are not a complete speaker-attribution proof.
  A deterministic post-verification audit also rejects detectable unstated
  identities/pronouns, presupposed possession/actions and uncertainty converted
  into assertions. These conservative text patterns can miss paraphrases or
  reject valid wording; they are not proof of entailment. Human review remains
  necessary. Identical wording is deduplicated even across different citations.
  Exact cited source names and locations are allowed in explicit references
  such as `from [source name]` or `listed at [location]`. Their words do not
  support an identity elsewhere in the question. A narrow typed-locator rule
  recognizes exact `Row N`, `Page N`, `Line N` and `Paragraph N` document locations and exact
  `HH:MM:SS` transcript positions from cited evidence. They may identify the
  cited source in a reference such as `record for Row 18` or `recording at 00:03:12`.
  An exact named document followed by `file, specifically Line 4` (or another
  supported supplied locator) is also a source reference. The exemption applies
  only to that occurrence; it does not make a second `4` a supported count.
  A named source cannot borrow another source's location. An altered locator,
  arbitrary metadata format, uncited source location, or a second occurrence of
  those digits as a count/event time remains subject to excerpt verification.
  Explicitly located references also cannot borrow a quotation, digit or detected
  content detail from another cited passage outside those locations.
  Metadata quotations remain unsupported unless present in the excerpt.
  A question beginning `What does the machine transcript ... report Speaker N
  said` also frames the machine account explicitly. Its speaker, negation,
  conditional and quotation checks still apply. In the narrow speaker term
  comparison, passive `was emailed` supports the act of sending; the noun
  `email` alone does not. This does not resolve pronoun antecedents between
  speakers or permit another speaker's details.
  Local references to records
  and what `they contain` or `their entries` do not invent a person's pronoun.
  The audit distinguishes a temporal `when [name] left` clause from an assertion
  that someone possessed an object. Narrow paraphrase patterns (such as
  `verify`/`check` and camera-recording noun order) are deduplicated while keeping
  different people, roles, literal quotations, numbers and negation distinct.
  If fewer than three questions survive, at most two top-up calls ask only for
  additional distinct questions, using the original passages and retained list.
  Repair feedback identifies the unsupported digits/named details, applicable
  drop reason, typed locators and original labelled transcript turns for the
  rejected candidates' citations. It asks the model to remove unsupported
  qualifiers, preserve each speaker's negation/condition, and choose a distinct
  supported angle. It does not rewrite candidates, transplant citations, or
  treat dates/places from another passage as proof of a record relationship.
  Asking `which speaker` is not a replacement for preserving the actual label.
  Feedback, source text and prior model attempts remain untrusted user data.
  The cap is configurable per profile. Each attempt retains its eight-item schema
  limit and the displayed list remains capped at eight. The internal receipt
  exposes every raw candidate across attempts to the unchanged quotation scorer,
  plus attempt boundaries, call timings, configured policy and every drop reason.
  Retention diagnostics report each attempt's raw/retained counts (including
  empty batches), drop reasons, and the remaining gap against the three-question
  target. An unmet target is reported, never filled automatically.
  A malformed attempt is recorded, never silently replaced. This internal receipt
  type cannot be supplied by model JSON; ordinary draft JSON keeps its original
  strict shape and eight-item limit. No questions are fabricated to reach a minimum.
  An optional model judge can only veto deterministic survivors. Invalid, missing
  or unavailable judge decisions reject the affected candidates and are recorded.
  The judge cannot change text/citations or resurrect a deterministic rejection.
  Each existing judge call also receives earlier retained questions, so it can
  veto paraphrases across batches without another call or revising earlier decisions.
  Deterministic batch exclusions stay excluded after judging; a veto cannot
  resurrect a duplicate or display-limit item that the judge never evaluated.
  Version 2 accepts one or two grounded questions from a usable case, with a
  plain explanation. It distinguishes clean short output, removed duplicates,
  filtering, failed/incomplete model responses, and no surviving questions.
  A short draft does not prove the source intrinsically supports no more questions.
  These changes require a fresh real-model receipt and do not turn earlier failed
  receipts into passes.
- **Proposal and save (decision 2).** The dock shows a proposal card that takes
  focus, lists each question with its sources, and offers **Save to case notes**
  and **Dismiss**. Short drafts carry a plain quantity explanation in both the dock
  and full-page proposal. Filtering and failed/incomplete generation have distinct
  explanations even when some grounded questions remain reviewable. Only absent
  or whitespace-only excerpt text receives a no-usable-source empty state, without
  calling a model or offering Save. Numbers, punctuation, poor OCR and transcript
  placeholders do not trigger that exception. Empty generation on usable passages
  remains an error. While a save is pending, Dismiss and the draft buttons are
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
`_TIMEOUT_SECONDS`, `_TOP_UP_CALLS` (integer 0–2, default 2),
`_JUDGE_GROUNDING` (boolean, default false), and `_JUDGE_TOKENS` (positive integer,
default 1,200). `<PROFILE>` is the uppercase policy name, such as `QUALITY`.
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
same citation/text checks. A draft uses at most one planning request, three
question batches (initial plus two top-ups), and—only when enabled—one judge
request per batch. Each call has its own timeout; total draft latency may be the
sum of those bounded calls. Transport/parse failures are recorded in the receipt;
the bounded top-up policy may recover, but no failed attempt is removed. Planning
failure cannot consume the final answer allocation. This does not promise that
model output or the optional judge is correct.

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

The owner-approved **v2 quantity policy** replaces the v1 three-question minimum
([decision](https://github.com/Exculpata/exculpata/pull/142#issuecomment-6154342607)).
The pass bar, applied to the questions a reviewer would see:

- at least 90% cite only relevant passages (a question that also cites a distractor does not count);
- none quotes text that is absent from its cited passages;
- every usable case shows at least one grounded question, with three as a target;
- failed, malformed or truncated drafting/judging responses fail the case, even
  when some useful grounded output survives. Optional planning failures remain
  diagnostic; planning cannot spend the final answer reserve.

Short output is accepted because it remains useful and grounded, not because a
model declares a source "thin." No model-declared sufficiency flag can bypass the
schema or checks. Rejected candidates and empty batches remain in the receipt.
Only absent or whitespace-only excerpt text qualifies as `no_usable_content`.
This explicit empty state is not useful output; an entirely empty set cannot pass.
Every case in the unchanged frozen set has usable text, so zero survivors in any
frozen case still fails. Filtering does not establish lack of usable source content.

New receipts use `exculpata-question-draft-evaluation-v2` and include
`acceptance_policy` with the decision, rationale, failure rule and superseded
format. `pass_bar.target_questions_per_case` is 3 and
`minimum_questions_per_usable_case` is 1; the old
`minimum_questions_per_case` field is removed. `cases_below_target` is diagnostic,
while `failed_cases`, `cases_without_grounded_questions` and
`cases_without_usable_content` distinguish failures and explicit empty sources.
Every case has an `outcome` (status, useful-output flag, source usability,
shown/target counts, generation completeness, problems and a plain explanation).
Retention renames `minimum_questions` to `target_questions`, `shortfall` to
`target_gap`, and top-up feedback `remaining_supported_questions` to
`remaining_target_questions`. These names do not change budgets or the two-top-up
maximum. Rich sources can still produce more than three questions.

Earlier v1 receipts keep their original format, three-question pass bar and failed
result. They are not rescored, overwritten, or represented as v2 runs. The latest
v1 [receipt at `22117ff`](https://github.com/Exculpata/exculpata/pull/142#issuecomment-6154217188)
remains failed (22 shown, 21 relevant, zero unsupported quotes; crate-records had
zero survivors), with separately unavailable loaded-artifact binding. A fresh
evaluation against the new commit and policy is required. Model pins, fixture
bytes/fingerprint, relevance threshold, quote checks, grounding checks, profile
capacities and maximum top-ups are unchanged.

The raw unsupported-quotation count remains diagnostic; the zero-quotation pass
bar still applies to shown questions. Malformed replies and transport failures
are recorded per case and cannot be labelled source insufficiency. Surviving
grounded output remains reviewable but does not make a failed case complete.

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
adapters, quantity policy, question generation/audit/judge modules and the scorer. It also records the model's content digest as Ollama
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

## Explaining a failed candidate

Add `--explain` to the evaluator command above. Each case includes an
`explanation` array, and a compact copy is printed to stderr; stdout remains one
strict JSON receipt. Candidate IDs such as `a1q2` identify the attempt and original
candidate. Explanations report the first failing production check, unsupported
numbers or quotations, and conflicting speaker terms where applicable. Categories
separate empty batches, quotation failures, source/span mismatches, relevance,
duplicates and other checks. Relevance here is the frozen set's scoring annotation,
not a new production rejection rule. Existing raw output and drop fields are kept;
explanations do not change retry prompts, model budgets or decisions.

Reproduce the three synthetic failures from the public
[#247 receipt](https://github.com/Exculpata/exculpata/pull/142#issuecomment-6167585456)
without a configured generator:

```console
.venv/bin/python scripts/evaluate-question-drafts.py \
  --replay tests/fixtures/question-drafts-recorded-247.json --explain \
  --output /tmp/question-drafts-offline-replay.json
```

Use a new output path for each run. Replay reads only the named synthetic raw
batches and the benchmark passages. It preserves both empty top-ups, recalculates
all deterministic decisions, and never reads runtime configuration or calls a
model. It excludes historic scores, judge decisions and model identity. The
result uses `model_gate: not_run`, `model_judge_executed: false`, and a separate
`deterministic_checks_passed` field; `--require-model` always exits nonzero for a
replay. A failed deterministic replay also exits nonzero. It is a regression aid,
not a replacement for a fresh real-model receipt.

The [#248 receipt](https://github.com/Exculpata/exculpata/pull/142#issuecomment-6179462369)
has all ten cases in `tests/fixtures/question-drafts-recorded-248.json`; use that
path with the same replay command and a fresh output path. Its six meeting
candidates had no judge calls: four source-account questions still require
speaker attribution, while the other two are identical questions about additional
transcript records. A bounded whole-question grammar recognizes those uncertain
record/version requests only when their descriptive name and every listed
timestamp identify the same cited recording. This source reference can establish
relevance without borrowing words from a speaker's account. Added assertions,
quotations, unknown positions and extra citations do not qualify. Ordinary
speaker questions retain their attribution, negation and condition checks.
When document IDs are supplied, they must agree; equal filenames cannot combine
different recordings or a known document with an unidentified one.

The separate `question-drafts-judges-248.json` fixture contains only the actual
public judge responses and historical outputs. Tests reapply those responses to
the same candidate IDs in the nine other cases and compare their retained text,
citations, outcomes and relevance annotations. They do not assign a recorded
verdict to the newly surviving meeting question. A separate fake-judge unit test
checks veto behavior; it is not model evidence. A fresh real-model gate is still
required on the changed implementation.

The recorded failures exposed an unrecognized named-document locator phrase,
a missing explicit machine-transcript reporting form, and `sent`/passive
`emailed` being treated as unrelated speaker terms. The repairs leave the
negation-losing meeting candidate and cross-speaker requests rejected. No broad
whitespace, pipe, cell, quote or transcript normalization was needed. The fixed
benchmark, model pins, generation budgets, target/minimum policy and accuracy
thresholds are unchanged.
