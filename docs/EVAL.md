# Defense-workflow evaluation

This harness grades a generator on a fixed synthetic discovery review. The same
questions, reference facts, and passage bytes are used for every run, so a
model change or a retrieval change can be compared instead of described as
"better."

It does not download a model, start a service, or select a production default.
A finished scorecard is a measurement. It is not a release threshold, and a
perfect stub score is not a model result.

## What is scored

Each question receives four deterministic scores:

| Score | Rule |
| --- | --- |
| Answer correctness | Every reference fact appears in the answer, and no forbidden fact appears. Comparison is case-insensitive with collapsed whitespace. |
| Citation support | Every span the answer attributes to a passage occurs in that passage, and every reference support span is cited from a passage that contains it. |
| Abstention correctness | The answer abstains exactly when the reference answer is not in the record. |
| Latency | Wall-clock milliseconds for retrieval plus answering. |

Retrieval hit is reported beside those scores. It is true when a reference
passage for every required fact was among the passages given to the answerer.
Questions whose answer is not in the record have no reference passage, so
retrieval is `n/a` for them and they are left out of the retrieval rate.

An optional LLM judge is available only in configured mode. Its verdict is
labeled `optional_llm_judge` and is excluded from the deterministic totals.
The judge is the configured generator's raw structured response. It is not a
second verified rubric.

## The versioned set

[`benchmarks/defense-workflow/v1/suite.json`](../benchmarks/defense-workflow/v1/suite.json)
is suite `defense-workflow` version `1`. It reuses the synthetic Harbor Street
text records in [`demo_data/harbor-street/`](../demo_data/README.md) and adds
two extension files under `benchmarks/defense-workflow/v1/extensions/`:

- a machine transcript of a recorded statement by the fictional Vela Quenrix
- a one-page distractor from a separate fictional matter, Lumen Yard

The native-text PDFs and the intentionally unreadable note are not passages in
this version. Names, places, dates, and identifiers are invented. No real
person, case, host, or employer is represented.

Question types:

- fact lookup
- cross-document chronology
- contradictions between witness statements
- impeachment material
- a media-transcript question
- questions whose correct answer is not in the record

The suite stores a SHA-256 for every passage and a fingerprint over the suite
body. Loading fails if a passage byte changes or the fingerprint does not
match. Do not edit version 1 to match an observed model. Add a new suite
version, with its own file and fingerprint, when questions, reference facts,
or passage bytes change. Compare scorecards only when the fingerprints match.

## CI stub mode

Stub mode makes no model or network call. The reference answerer quotes each
reference span when retrieval returned that passage, and abstains when it did
not.

```console
python -m case_intelligence.defense_eval --mode stub --output-dir /tmp/defense-eval
```

The command writes `scorecard.json` and `scorecard.md`. It refuses to replace
an existing scorecard unless `--force` is set. Exit 0 means the harness
finished. Exit 2 means the suite or the arguments could not be evaluated.
Inspect the totals either way. A perfect stub card means the lexical retriever
found every reference passage and the scorer accepted those quotes.
`tests/test_defense_eval.py` runs this mode inside the application suite, so
code changes exercise the harness without a GPU.

## Configured model

Configured mode uses the same environment as the workbench answer path. Set
`CASE_INTELLIGENCE_GENERATOR_BACKEND` to `ollama` or `openai`,
`CASE_INTELLIGENCE_GENERATOR_URL` to that service's origin, and
`CASE_INTELLIGENCE_GENERATOR_MODEL` to a model the service lists. Optional
`CASE_INTELLIGENCE_GENERATOR_API_KEY`, `CASE_INTELLIGENCE_GENERATOR_TIMEOUT`,
and `CASE_INTELLIGENCE_GENERATOR_DISABLE_THINKING` behave as they do for
ordinary answers. See [model configuration](MODEL_CONFIGURATION.md).

```console
python -m case_intelligence.defense_eval --mode configured --output-dir /tmp/defense-eval-model
```

`--profile portable` or `--profile quality` records the pinned generator id,
revision, and license from `config/models.json` on the scorecard. The scorecard
stores the backend and model name. It does not store the endpoint. The run
still requires the generator to be available; a profile flag does not start or
download anything.

`--judge` asks that same generator for a raw verdict after the deterministic
score is fixed. It cannot be combined with stub mode.

```console
python -m case_intelligence.defense_eval --mode configured --judge \
  --profile portable --output-dir /tmp/defense-eval-model
```

`--top-k` defaults to 8 and cannot exceed 12, which is the number of evidence
slots the answer schema accepts.

## How to read the scorecard

The markdown card leads with the suite version, fingerprint, and mode. The
table has one row per question: answer correctness, citation support,
abstention correctness, retrieval hit, and latency. Deterministic totals follow.
The LLM-judge section says "Not run" unless `--judge` was set. The misses
section names facts that were not cited and citations whose support span was
not in the cited passage.

The JSON card has the same totals plus, for each question, the retrieved
passage ids, cited passage ids, answer text, and any judge object. `scoring`
is `deterministic`. `measurement_only` is true. `release_threshold` is null.

Use the card this way:

- A drop in retrieval hit, with the fingerprint unchanged, means the ranker
  no longer placed a reference passage in the passages sent to the answerer.
- A drop in answer correctness, citation support, or abstention correctness
  under `--mode configured` is a grade of that generator and of
  `GroundedGenerationService` verification.
- Latency is the harness clock for that question, not a capacity claim.
- Judge verdicts can disagree with the deterministic totals. The totals do
  not move when the judge disagrees.

The harness ranker is deterministic lexical overlap over the versioned
passages, scoped to the question's matter. `run_eval` accepts a replacement
retriever, so a retrieval change can be graded on this set. Configured answers
go through the product generator and verifier. This version does not execute
the PostgreSQL hybrid index; that path remains covered by its own acceptance
tests.
