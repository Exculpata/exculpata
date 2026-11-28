# Hybrid search ranking for named documents and multi-clause temporal questions (#281)

## Kickoff

- **Goal (user-visible outcome):** Recover the remaining 4 retrieval misses on the PostgreSQL hybrid retriever (`38/42` -> `42/42`, retrieval `30/34` -> `34/34`) without regressing any existing question, abstention, or pool-invariance invariant, so comparative and multi-clause criminal-defense questions reliably surface every named source and temporal clause in the top-8 packet.
- **Expected files to touch:**
  - `specs/hybrid-search-multi-document-ranking.md`
  - `src/case_intelligence/review_bench_v2.py`
  - `src/case_intelligence/review_quality.py`
  - `tests/test_review_bench_v2.py`
  - `tests/test_review_quality_regressions.py`
- **Acceptance check:**
  1. Synthetic regression tests in `tests/test_review_bench_v2.py` (named multi-line document heading in a comparative question + pool invariance) and `tests/test_review_quality_regressions.py` (multi-clause temporal question preserving clause-1 exact record) fail on base commit `6a4a6c1` and pass after the fix.
  2. Live PostgreSQL `python -m case_intelligence.defense_eval --mode stub --retriever postgres` achieves `42/42` (answer `42/42`, citation `42/42`, abstention `42/42`, retrieval `34/34`) with zero regressions in `--compare` against baseline `6a4a6c1` (`38/42`, retrieval `30/34`).
  3. All existing retrieval, temporal, quality-regression, Postgres defense-eval, import-boundary, ruff, and publication checks pass.
- **Issue link:** https://github.com/Exculpata/exculpata/issues/281

## Problem and root cause

Following #381 (ranked OR PostgreSQL lexical matching) and #484 (clause balancing and email header clock handling), live PostgreSQL `defense_eval` v2 scores `38/42` (retrieval `30/34`), missing 4 passages:
1. `dw-inspection-plan` (`hs-report-03`, rerank rank 8 -> final rank 15, `post_rerank`): `_exact_record_features` in `review_quality.py` applies the half-subject-term relevance gate (`subject_overlap * 2 >= len(subject_terms)`) across the combined subject terms of all clauses in a two-part temporal question (`"At what time did the route inspection end, and on what date did ..."`). A passage that fully answers the shorter first clause and supplies the exact clock time fails the combined half-subject gate and is demoted behind passages matching the longer second clause.
2. `dw-platform-contents` (`hs-report-06`, heading `"Parcel discrepancy"`), `dw-passage-observation` (`hs-note-21`, heading `"West passage note"`), and `ly-spool-contents` (`ly-cores`, heading `"Contents note"`): In comparative questions (`"How do X and Y differ about Z?"`, `"Compare A, B, and C..."`), `DeterministicReranker` treats words in a document's title/heading line identically to incidental body mentions and dilutes the second compared document's terms inside a long shared tail clause. As a result, documents explicitly named by heading in the question rank outside the top-8 packet behind body-text distractors.

## Approach

1. **Pool-invariant heading-match bonus in `DeterministicReranker` (`src/case_intelligence/review_bench_v2.py`):**
   - For multi-line candidates (`"\n" in candidate.text`), inspect the first non-empty line as a document heading when it contains between 2 and 8 non-stop tokens.
   - If those heading tokens appear as a contiguous subsequence of the query's non-stop tokens, award a deterministic, pool-invariant heading-match bonus proportional to the query scale (`max(len(query_terms), 4) * 0.65`, added into `(overlap + bonus + heading_bonus) * 100 + candidate.score`).
   - Depends strictly on `(query, candidate)` (preserves total pool invariance) and leaves single-line candidates and all existing fallback-reranker unit tests unchanged.
2. **Clause-aware relevance gate and RFC-5322 email header exclusion in `_exact_record_features` (`src/case_intelligence/review_quality.py`):**
   - Split multi-clause queries on `[?;]|\b(?:and|or)\b` into substantive clauses (`len(clause_terms) >= 2`).
   - Treat a candidate as relevant when it matches an explicit identifier, or satisfies the half-subject-term gate on the full query, OR satisfies the half-subject-term gate on at least one substantive clause (with at least 2 matched clause subject terms). Single-clause questions preserve the exact existing half-subject gate.
   - Exclude RFC-5322 email header blocks (lines before the first blank-line separator in `.eml` chunks carrying `From:`/`Date:`/`Subject:` headers, alongside `location_label == "Email headers"`) when scanning for clock precision so RFC-5322 `Date: ... HH:MM:SS` transport headers do not outrank body event timestamps.

## Vision check

Directly serves [VISION.md](../VISION.md) and [IDEAL.md](../docs/IDEAL.md): improves retrieval recall for real defense questions (contradictions, impeachment, chronologies) through general, pool-invariant scoring without allowlisting filenames, adding modes, or weakening abstention checks.

## What it could break

- `tests/test_review_bench_v2.py`: pool-invariance, clause-balancing exact scores (`[336, 186]`), stop-word/pair bonus (`35.0`), and tie-ordering tests must remain untouched and green.
- `tests/test_review_quality_regressions.py`: half-subject gate on single-clause temporal questions (`test_exact_record_precision_requires_half_the_subject_terms`) and identifier-prefix rejection must remain untouched and green.
- `not_in_record` abstention questions in `defense_eval` v2 must remain 100% abstained.
