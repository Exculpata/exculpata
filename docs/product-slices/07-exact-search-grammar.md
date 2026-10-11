# 07: Define and parse strict Boolean search

Status: implemented on `main` as
[#36](https://github.com/Exculpata/exculpata/pull/36). See
[exact search foundation](../EXACT_SEARCH.md). Depends on 00.

## Finding and outcome

The PostgreSQL lexical lane uses `websearch_to_tsquery('english', ...)`; the
CPU fallback scores token overlap. Neither is a shared strict Boolean contract.
The fallback returned `blue bicycle` and `red truck` for `red AND bicycle` in a
synthetic probe. Users need operators with predictable, documented meaning.

## Small implementation

Add a typed parsed-query representation and explicit grammar for terms, quoted
phrases, `AND`, `OR`, `NOT`, and parentheses. Choose documented precedence
(`NOT`, then `AND`, then `OR`) and implicit conjunction. Define case, Unicode,
punctuation, hyphen, stop-word, and document-versus-passage semantics. Exact mode
should not silently stem away a requested distinction. Keep original query text
alongside its parsed representation and a versioned interpretation.

Invalid or unsupported operators return an actionable parse error with a
location. Never silently fall back to semantic matching. This slice introduces
the parser and compiler contracts; the visible exact-search route follows in 08.

## Code and acceptance

Integrate beside `review_bench_v2.py::PostgresHybridBackend` and
`workbench.py::search`; isolate parsing in a focused module. Read the official
[PostgreSQL search-control documentation](https://www.postgresql.org/docs/17/textsearch-controls.html)
when implementing the backend; its web-search syntax is not the product grammar.

Use truth-table tests, precedence/parentheses combinations, apostrophes,
Unicode, escaped quotes, negation-only policy, query complexity bounds, and
malformed inputs. Expected results must come from fixture truth, not token
overlap. Define proximity/wildcards/fields as explicit unsupported errors until
09 implements them. No model or runtime schema change is required for parsing.
