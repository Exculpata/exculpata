# 08: Return a complete, scoped exact-search result set

Status: exact-result service is on `main` via
[#59](https://github.com/Exculpata/exculpata/pull/59)
([#42](https://github.com/Exculpata/exculpata/pull/42) /
[#53](https://github.com/Exculpata/exculpata/pull/53)). See
[exact search results](../EXACT_SEARCH_RESULTS.md). Real PostgreSQL indexed
acceptance remains pending; no PostgreSQL exact adapter is claimed.
Depends on 07.

The `postgres-integration` Quality gates job exercises the existing hybrid
backend's database tests without skips. It does not implement an exact-search
adapter or satisfy this slice's shared-corpus PostgreSQL acceptance.

## Finding and outcome

`HybridRetriever` merges lexical and dense candidates and returns at most 20
passages. This is evidence selection for answering, not enumeration of all
documents matching an expression. Semantic-only candidates can survive if
their score thresholds pass, even when a lexical condition excludes them.
The Sources list has a separate metadata substring filter.

## Small implementation

Add an explicit Exact search path using the parsed grammar from 07. Match
documents across their extracted units, return exact totals and stable paginated
document results, and display matching passages as explanations. `NOT blue`
must apply at the documented document scope, including when `blue` occurs on
a different page. Phrase matching must respect defined adjacency and boundaries.

Keep semantic search separately labeled. A reranker may reorder exact matches
but cannot add a nonmatch or remove matching documents from the browsable set.
Apply matter authorization and collection/source-set scope before matching and
counting. Show excluded/unprocessed sources separately from zero matches.

## Code and acceptance

Start with `workbench.py::search`, `review_bench_v2.py`, source catalog filters,
and Sources/search UI. Keep the answer retriever as a separate consumer.

Run the same known-answer suite against CPU and real PostgreSQL. Check over
100 matching documents, stable pagination, terms on different pages,
cross-matter canaries, source mutation, and no-result exclusions. Persist query,
grammar version, scope, and source versions when saving a result set; use or
extend existing source-set persistence with migration/restore evidence as needed.
Do not describe a changing live index as a frozen search receipt.
