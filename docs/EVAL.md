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
Inspect the totals either way. A perfect stub card means the selected retriever
found every reference passage and the scorer accepted those quotes.
`tests/test_defense_eval.py` runs lexical stub mode inside the application
suite, so code changes exercise the harness without a GPU or a database.

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

The product full-text query ANDs the question's words. A long question often
matches no single passage, so the dense lane and the reranker decide the top
set. With the deterministic pair, that currently misses five reference
passages. Lexical mode still retrieves all 34. The difference is a measurement
of this path, not a failed harness.

```console
python -m case_intelligence.defense_eval --mode stub --retriever postgres \
  --output-dir /tmp/defense-eval-postgres
```

### Per-lane rank diagnostics

`--retriever postgres` also writes `lane_diagnostics` to every question row and
a summary to the card. The Markdown card adds a "Per-lane rank diagnostics"
section. The harness wraps the backend and reranker it passes to
`HybridRetriever`, so it records each lane's real output without changing the
search. For each reference passage it records the rank and score in these lanes:

- `lexical`: PostgreSQL full-text search (`ts_rank_cd`)
- `vector`: pgvector cosine search
- `fused`: reciprocal rank fusion. Ranks past the rerank limit are recomputed
  with the retriever's own formula, and the card checks that the recomputed
  order matches the recorded rerank input.
- `rerank`: reranker order, before score floors

It also records the final rank, which comes after the floors and the
exact-record and temporal reorder. Final rank needs one more search at the
production depth limit of 20. The harness runs that search only when a
reference passage is missing from the scored top set, and it does so after
latency is measured. `beyond depth` means the passage was not in those 20.

A lane's status is `hit` (inside the top set), `ranking` (returned but below
the top set) or `recall` (not returned). Each reference passage behind a
missed fact gets one reason, checked in this order:

1. `not_retrieved` (recall). Neither the full-text lane nor the vector lane
   returned the passage.
2. `ranked_out` at `fused` (ranking). Fusion placed the passage past the
   rerank limit.
3. `score_floor` (ranking). The production floors removed a dense-only
   candidate. Floors apply only with a healthy worker.
4. `crowded_by_near_duplicates` (ranking). Taking out the near duplicates
   ranked ahead of the passage would bring it into the top set. A near
   duplicate is a named suite near miss, or a passage with token Jaccard of
   0.5 or more against the reference passage.
5. `ranked_out` (ranking). The deciding lane is `rerank` when the reranker
   ranked the passage below the top set. It is `post_rerank` when the reranker
   placed it in the top set and the exact-record or temporal reorder moved it
   out.

Every number in this section is measured in that run. Nothing is estimated.

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
