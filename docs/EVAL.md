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
| Citation support | Each cited quote occurs in the named passage and includes a complete reference support span. Every required fact has at least one such citation. An unrelated header or partial quote from the right passage does not count. |
| Abstention correctness | The answer abstains exactly when the reference answer is not in the record. |
| Latency | Wall-clock milliseconds for retrieval plus answering. |

Retrieval hit is reported beside those scores. It is true when a reference
passage for every required fact was among the passages given to the answerer.
Questions whose answer is not in the record have no reference passage, so
retrieval is `n/a` for them and they are left out of the retrieval rate.

JSON reports `unsupported_citation_count` per question and in the aggregates.
It counts supplied citations that fail support, not missing citations. Missing
support is listed separately under `missed_facts`. The citation support rate is
the fraction of questions satisfying the complete support rule, not the fraction
of individual citations that pass.

An optional LLM judge is available only in configured mode. Its verdict is
labeled `optional_llm_judge` and is excluded from the deterministic totals.
The judge is the configured generator's raw structured response. It is not a
second verified rubric.

## The versioned set

[`benchmarks/defense-workflow/v2/suite.json`](../benchmarks/defense-workflow/v2/suite.json)
is the default suite, `defense-workflow` version `2`. It reuses the synthetic
Harbor Street text records in [`demo_data/harbor-street/`](../demo_data/README.md),
keeps the version 1 machine transcript, and adds a second synthetic matter,
Lumen Yard, under `benchmarks/defense-workflow/v2/extensions/`. Version 1 remains
loadable at `benchmarks/defense-workflow/v1/suite.json` and is not edited when
the question set grows.

Version 2 has 42 questions across both matters. Each question names one or more
near-miss passages: same matter, not a reference passage, and, where the
question has a reference span, that span does not occur in the near-miss text.
Citing a near-miss fails citation support. The types are fact lookup,
cross-document chronology, contradictions between witness statements,
impeachment material, media-transcript questions, and questions whose correct
answer is not in the record.

The native-text PDFs and the intentionally unreadable Harbor Street note are
not passages. Names, places, dates, and identifiers are invented. No real
person, case, host, or employer is represented.

The suite stores a SHA-256 for every passage and a fingerprint over the suite
body. Loading fails if a passage byte changes or the fingerprint does not
match. Do not edit a published version to match an observed model. Add a new
suite version, with its own file and fingerprint, when questions, reference
facts, or passage bytes change.

### Adding questions

1. Create a new version directory; preserve published versions. Use only original
   synthetic text and give each passage a unique id, matter id, relative path,
   locator, evidence kind (`document` or `transcript`) and SHA-256 of its bytes.
2. Give each question a unique id, one of the six types, its matter id and text.
   For answerable questions, add `required_facts` with an `answer_text` and at
   least one `supports` entry naming a passage and an exact `support_text` span.
   Add `forbidden_facts` for known wrong alternatives. For `not_in_record`, set
   `expect_abstention: true` and leave `required_facts` empty.
3. For version 2 and later, include same-matter `near_misses` that are neither
   reference passages nor contain a reference span. Retain all six types in the
   suite. Review the answers and distractors independently of model output.
4. Set `suite_version`, calculate the new fingerprint with
   `case_intelligence.defense_eval.suite_fingerprint(payload)`, and store it as
   `fingerprint`. That helper excludes the fingerprint field itself. Run
   `python -m case_intelligence.defense_eval --mode stub --suite PATH --output-dir OUTPUT`
   and review both cards. The loader verifies passage hashes, reference spans,
   distractors and the suite fingerprint. A model score is not grounds to edit
   the reference answers.

## CI stub mode

Lexical stub mode makes no model or network call. The reference answerer quotes each
reference span when retrieval returned that passage, and abstains when it did
not.

```console
python -m case_intelligence.defense_eval --mode stub --output-dir /tmp/defense-eval
```

The command writes `scorecard.json` and `scorecard.md`. It refuses to replace
an existing scorecard unless `--force` is set. Exit 0 means the harness
finished without evaluation errors, even if every quality score is zero.
Exit 2 means the suite, arguments, retrieval, output or model call could not be
evaluated. Captured runtime errors retain a card with a nonzero `errors` count
and still fail the command. A rejected model response is a scored failure,
reported as `rejection` and in aggregate `rejections`, not a harness error.
Parsed objects that fail the answer schema use the `GenerationRejected` scorecard
category; they remain unavailable to product callers. Connection failures and
unreadable response envelopes still count as harness errors.
Unexpected uncaught errors also fail the process.
Inspect the totals either way. A perfect stub card means the selected retriever
found every reference passage and the scorer accepted those quotes.
The stable-score unit fixture supplies fixed retrieval results and a fixed
clock. Actual lexical and PostgreSQL retrieval tests check execution, isolation,
cleanup and accounting, not a fixed number of correct answers or missed ids.

### Automatic CI evidence

The `defense-eval` job runs on every PR, including documentation-only PRs. It
retains JSON and Markdown scorecards and writes the main-to-candidate delta to
the job summary. No GitHub comment is needed. The lexical stub card requires no
database; the comparison uses the job's disposable PostgreSQL/pgvector service
with deterministic adapters, without GPU inference or model downloads.

Both product checkouts use the same candidate evaluator and pinned question set.
This lets the baseline run even before this evaluator exists on main, and keeps
scoring changes out of the product comparison. The baseline is the candidate's
exact first parent (`HEAD^1`), including the previous commit for a push to main.
Any event-provided base must equal that parent; equal baseline and candidate
revisions fail as a harness error. Provenance records both commits and the shared
evaluator. Each checkout gets a separate environment installed from its own
`requirements/application.lock` with `--require-hashes`. Provenance records each
lock path, SHA-256 and interpreter. The two runs import product code separately,
and the delta is candidate minus main. A missing baseline, failed run or malformed
card fails the job; a negative or positive quality delta does not. Partial
artifacts are retained on failure and must not be described as complete results.

This mock comparison measures production PostgreSQL retrieval under deterministic
adapters. It does not measure a configured model, generation quality, learned
embeddings or learned reranking. Answer-generation PRs receive the same bounded
mock summary, but need a separate configured run to establish a model effect.

## Retrieval

`--retriever lexical` is the default. It ranks passage text inside the
question's matter with deterministic token overlap. It does not open a
database, call a model, or use the network. CI on every pull request uses this
retriever.

`--retriever postgres` runs the same search the workbench uses for a PostgreSQL
index. Each passage is stored as one text unit. `HybridRetriever` then runs
PostgreSQL full-text search, pgvector cosine search, reciprocal rank fusion,
and the reranker, including the exact-record and temporal reorder used on that
path. Set `CASE_REVIEW_POSTGRES_TEST_DSN` to a disposable database. The
scorecard records the retriever name, index, embedder class, and reranker
class. It does not record the connection string.

When `CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL` answers `/health` with both
models loaded, this mode uses the remote embedding and reranker adapters and
the production dense and rerank floors. Otherwise it uses
`DeterministicEmbeddingAdapter` and `DeterministicReranker`, which is the
GPU-free pair, on the real PostgreSQL index. The postgres-integration job runs
`tests/test_defense_eval_postgres.py` on that GPU-free pair. Rows are stored
under a `ci-matter-` id derived from the synthetic matter label and deleted
when the run finishes.

For plain questions, the product full-text query ORs PostgreSQL's parsed,
stemmed terms and ranks matches with `ts_rank_cd`. Passages can match some
question terms without containing every term. Queries containing double quotes
or parsed negation retain the existing `websearch_to_tsquery` behavior, including
phrase matching and AND between unquoted terms. The dense lane, fusion, reranker
and subsequent reorders still contribute to the final top set. The number of
missed reference passages is an observed measurement, not a fixed test
expectation or a failed harness.

```console
python -m case_intelligence.defense_eval --mode stub --retriever postgres \
  --output-dir /tmp/defense-eval-postgres
```

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

## Comparing two scorecards

`--compare A.json B.json` reads two finished scorecards and prints per-category
deltas of B minus A. It does not run retrieval or a model, and it cannot be
combined with `--mode`. Exit 0 means both cards were readable. A negative delta
is still exit 0; the table is how a pull request shows a regression. Questions
present on only one card are listed and left out of the rates. When the
fingerprints differ, the report says so before the deltas.
Shared ids whose types changed appear under "Changed question types". Each
card contributes to its own category totals, while overall totals still include
all shared ids.

```console
python -m case_intelligence.defense_eval \
  --compare /tmp/defense-eval-before/scorecard.json /tmp/defense-eval-after/scorecard.json
```

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

The default ranker is deterministic lexical overlap over the versioned
passages, scoped to the question's matter. `--retriever postgres` grades the
PostgreSQL hybrid index and reranker on the same questions. Configured answers
go through the product generator and verifier. Use `--compare` to put the
category deltas in the pull request.

## Scoring limits and follow-ups

Normalized substring matching is not semantic entailment. Equivalent paraphrases
can fail, and inclusion of reference words does not establish that every claim
is true. Citation scoring deliberately requires complete reference spans, so a
valid but differently worded answer can fail. The configured path scores the
product verifier's accepted answer, not unfiltered raw model output. Recognized
machine-transcript caution prefixes and unsourced verification notices remain
in the displayed answer but are not treated as quoted source text. It therefore
does not estimate real-world hallucination rates, general legal accuracy or
production readiness. Abstention in stub mode is reference-aware. Wall-clock
latency depends on the runner and is not a hardware capacity benchmark.

The independent chronology (#280), production-summary (#282), inconsistency
(#283) and review-candidate (#289) fixture adapters are follow-ups. This change
does not import those branches or claim their workflows are evaluated here.
