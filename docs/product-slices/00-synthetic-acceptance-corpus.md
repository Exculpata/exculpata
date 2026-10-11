# 00: Seed a shared product acceptance corpus

Status: corpus and reference matcher are on `main` as
[#36](https://github.com/Exculpata/exculpata/pull/36). See
[exact search foundation](../EXACT_SEARCH.md). PostgreSQL search-job
acceptance remains pending. No dependencies.

The `postgres-integration` Quality gates job exercises the existing hybrid
backend's database tests without skips. It does not implement an exact-search
adapter or satisfy this slice's shared-corpus PostgreSQL acceptance.

## Finding and outcome

Existing tests cover many boundaries, but the observed Boolean behavior and
review bottlenecks need known-answer product examples. A passing synthetic
generator test does not measure real model recall or production capacity.
Reviewers need a small repeatable way to tell whether each slice improves the
work they are trying to do.

## Small implementation

Add a versioned synthetic fixture manifest and a small initial text corpus:
two similarly named people, competing accounts, terms separated across units,
one crucial late passage, and one intentionally unavailable source. Record
expected exact-search document IDs, evidence locations, relationships that may
be suggested, and relationships that must not be inferred. Reuse existing
fixture and review-acceptance machinery where possible.

Keep expected results independent of current implementation output. Mark known
gaps explicitly in the acceptance report; do not turn them into silent passes.
Each subsequent slice adds only the fixtures it needs. Larger generated corpora
and multimedia model evaluation are later acceptance layers.

## Code and checks

Start with `benchmarks/review-acceptance-v1.json`, `scripts/run-review-acceptance.py`,
`tests/test_review_acceptance_pack.py`, and synthetic fixture conventions.

Acceptance: fixtures have stable identities and hashes; expected results can be
checked manually; the same corpus runs in CPU and PostgreSQL search jobs; it
includes a foreign-matter canary. Record exact match recall/precision separately
from review evidence coverage and human correction agreement. No latency or
million-document capacity claims follow from this small fixture.

No runtime schema or model change is needed.
