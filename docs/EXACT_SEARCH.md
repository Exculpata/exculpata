# Exact search foundation

This change implements the shared grammar and reference matcher for product
slices 00 and 07. It does not switch the existing browser search to exact mode.
The ranked answer retriever still has its existing behavior. Slice 08 adds a
separate [exact result service and UI](EXACT_SEARCH_RESULTS.md), using this grammar
for complete, scoped document enumeration with bounded scans.

## Grammar versions

The current default is `recordbench-exact-v2`, adding [word proximity](EXACT_SEARCH_PROXIMITY.md).
Explicit `parse_query(..., grammar_version="recordbench-exact-v1")` retains the
original grammar and rejects proximity.

`recordbench-exact-v1` supports words, quoted phrases, `AND`, `OR`, `NOT`, and
parentheses. Operators are case-insensitive; quote a literal operator word such
as `"and"`. Adjacent operands imply AND. Precedence is NOT, AND, then OR.

| Query | Meaning within one eligible document |
| --- | --- |
| `red AND bicycle` | Both terms occur, possibly on different extracted pages/units |
| `"red bicycle"` | Consecutive word tokens in one extracted unit |
| `bicycle NOT blue` | Bicycle occurs and blue occurs nowhere in its extracted units |
| `(red OR blue) AND bicycle` | Bicycle plus either color |
| `NOT blue` | An eligible document with extracted word tokens and no blue token |

Negation-only queries are defined over the caller's authorized eligible
population. Missing, failed, and unextracted sources are not inferred to match.
The reference matcher does not perform authorization or discover documents.
Every backend must apply matter and selected-population boundaries before
matching and counting. Empty/tokenless documents never match, including NOT.

Text and queries use Unicode NFC, case folding, and curly-to-straight apostrophe
normalization, with NFC applied again after folding. Words retain attached
Unicode combining marks, internal apostrophes and ASCII hyphens; `RB-101` is
different from `RB 101`. Other punctuation separates words inside document text
and quoted phrases. There is no stemming or stop-word removal. Phrase adjacency
does not mean byte equality, and phrases do not cross extracted-unit boundaries.
This word-token contract does not promise language-specific segmentation.

Unquoted punctuation must be quoted rather than silently reinterpreted. Dash
negation, wildcards, fuzzy search, and field syntax are unsupported
and return a position-bearing error. Inside quoted phrases, only quote and
backslash may be escaped. Control/hidden-format characters are rejected.

Limits are 512 query characters before and after normalization, 128 grammar tokens, and 16 nested parentheses
or NOT levels. Parse errors contain an explanation and character location,
without echoing matter query text. The serialized plan records original text,
normalized expression, grammar version, tokenizer version (`recordbench-words-v1`),
and a typed operator tree. This is a
backend contract, not executable SQL and not permission to access a source.
Normalized expressions omit redundant grouping and use implicit AND so
serialization does not add grammar depth or tokens at those input limits.

## Ask intent classification

`case_intelligence.ask_router.classify(text)` is a pure helper for a future
shared Ask entry point. It returns a frozen `RouteDecision(kind, reason)` with
one short reviewer-facing explanation. It performs no search, model call or I/O,
and is not connected to a UI or HTTP route.

Routing precedence is:

1. `exact` when the trimmed input successfully parses and its structure contains
   a quoted phrase, uppercase Boolean operator (`AND`, `OR`, `NOT`) or proximity
   expression. Adjacent plain words and grouping alone remain questions. The
   router detects uppercase operator tokens outside quoted phrases in the original
   text because the parser retains explicit `AND` provenance but not operator case.
   A phrase alone does not establish exact intent inside a question or an ordinary
   non-exhaustive request: `Who mentioned "red bicycle"` and
   `Show texts between "Dana" and "Lee"` stay questions, with or without `?`.
   Interrogative and auxiliary prefixes, explanation/summarization verbs and
   retrieval verbs identify these requests, including polite wrappers and
   `I need to know`, `I want to understand` or `I need an explanation`. Uppercase
   Boolean and proximity expressions still take precedence (`who OR what`,
   `show AND texts`, `did NEAR/3 witness`). Quoted enumeration retains exact
   precedence (`find every document containing "red bicycle"`).
   Exact-search parsing and matching semantics remain case-insensitive and unchanged.
2. `every_source` for case-insensitive positive requests to enumerate matching
   records, such as `find every email`, `list all`, `show me all of the reports`,
   `check every source`, or `I need to find all records mentioning bicycles`. Retrieval
   verbs must begin the request, optionally with `please` before or after
   `can/could/would/will you`; `review`, `enumerate`, `pull` and `pull up` are also
   supported. `pull all`, `show me each report` and `list every report where
   the witness describes the bridge` work in lowercase too.
   `I need` and `I want` can introduce a population directly only when it ends
   the request. Filtered populations require a retrieval verb, including forms
   such as `I need to find` or `I want you to list`. This avoids guessing whether
   a suffix requests collection or another operation: `I need all documents
   about bicycles deleted` remains a question. Populations include generic
   records, media nouns and every extension advertised by the source upload UI,
   including forms such as `all audio files`, `every matching PDF document`,
   `all DOCX files`, `every .xlsx file`, and `all PNGs`.
   A bare population
   must stand alone (`all documents`, `every report?`). Filtered bare phrases
   such as `all documents about bicycles` remain questions: without a retrieval
   verb, they cannot reliably be distinguished from the subject of a
   statement such as `All documents about bicycles agree`. This conservative
   boundary avoids guessing from a verb list; add `List` or `I need to find` to request
   enumeration. Casual uses of `all`, interrogative wrappers, negations, and
   conditional mentions do not qualify through an incidental retrieval phrase.
   A population immediately followed by a statement predicate (`is`, `are`,
   `was`, `were`, `has`, `have`, `had`, `seem(s)` or `agree(s)`) stays a question: `Show all reports
   are consistent` asks about a proposition, not an exhaustive collection.
   Unquoted cancellation or deferral cues also stay questions: a new sentence
   starting with `No`, `but do not`/`but don't`, or `only if`/`only when`/`only after`.
   For example, `pull all documents? No, just summarize.` and `Pull every source
   only if I say so` do not request exhaustive review now. Quoted filter text
   cannot activate this guard, and negative filters such as `Find every report
   where they said not to search all sources` still enumerate.
3. `question` otherwise, including empty input, whose reason says nothing was
   typed. Parse failures fall through to these natural-language rules, so an
   unbalanced quote alone does not select exact search.

The existing parser is the sole authority on exact syntax, including its limits
and punctuation rules. `What does "red bicycle" mean` is now a question whether
or not it ends in `?`; quoted topic words no longer turn the surrounding prose
into literal search terms. `list all documents with "red` is `every_source` after parsing fails.
The helper does not extract or repair fragments of a malformed query.
Legal-search shorthand such as `w/5` is unsupported by the parser and remains a
question; supported proximity uses `NEAR/n` or `BEFORE/n`. No matching semantics
or syntax were added to `exact_search.py`. One-word inputs, bare Bates-style
identifiers, uncertain fragments and typos without recognized syntax stay
questions; the router does not autocorrect them into an exhaustive review.
Lowercase or mixed-case `and`, `or` and `not` do not establish exact intent in
Ask. `Did Smith and Jones meet on Tuesday` is `question` with or without `?`;
`show me all the texts between Dana and Lee` and `List each and every document`
are `every_source` with or without `?`. `Smith AND Jones` and `Smith NOT Jones`
are `exact` when the whole input parses. Outside natural-language requests,
quoted phrases still establish exact intent inside lowercase Boolean expressions.
For example, `"red AND blue" and green` selects exact search because of its quoted
phrase; the uppercase `AND` inside that phrase is literal text, not an operator.

`tests/test_ask_router.py` covers synthetic intent examples, ambiguous inputs,
precedence, immutable decisions and explanations. This foundation does not
claim that enumeration, source coverage or cited answers have run.

### Frozen reviewer-language benchmark

[`benchmarks/ask-router-v1.json`](../benchmarks/ask-router-v1.json) contains 238
synthetic inputs with independently authored expected kinds and one-line
rationales. It covers people, places, dates, events, Boolean/proximity/phrase
searches, exhaustive commands, imperatives, quoted questions, typos, single
words, Bates-style identifiers and names containing `and`. Ambiguous fragments
prefer `question`; the set includes negative controls for accidental expensive
enumeration. The test pins the complete file's SHA-256:
`64fae76af910dee404dbae35a4073e492cfb224e7309671c445075518d21bb43`.

The same frozen inputs were measured against main revision
`61c4bbd8027c192b87180071cc0c0c0ff96e3131` and the M1-Q1 router changes:

| Expected kind | Before | After |
| --- | ---: | ---: |
| `exact` | 64/64 (100.00%) | 64/64 (100.00%) |
| `question` | 79/111 (71.17%) | 111/111 (100.00%) |
| `every_source` | 49/63 (77.78%) | 63/63 (100.00%) |
| Overall | 192/238 (80.67%) | 238/238 (100.00%) |

The 46 corrected cases are 28 quoted questions/ordinary requests, 14 `pull`
or `pull up` exhaustive requests and four accidental enumeration decisions.
Accidental `every_source` selections fell from four to zero. There are no
remaining misses in this version. The initial whole-percent floors were
100/71/77 by kind and 80 overall; after the fixes, all floors are ratcheted to
100%, exceeding the 95% overall target and preserving 100% on exact syntax.
The benchmark also independently forbids accidental `every_source` decisions.

Run the pinned benchmark and print counts and accuracy per kind:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q -s tests/test_ask_router_benchmark.py
```

These results measure deterministic classification on a synthetic regression set,
not observed reviewer traffic, retrieval completeness or answer quality. The
router remains local, pure and unconnected to the Ask UI; the benchmark selects
no engine and uses no model, network, source material or configuration.

## Known-answer corpus and validation

`tests/fixtures/synthetic/product-foundation/v1/corpus.json` contains invented
documents, unit identifiers/hashes, manually specified search results, a
foreign-matter canary, an unavailable source, separate same-name people,
competing accounts, and a decisive fourteenth unit. A separate SHA-256 fixes
the corpus bytes. Changes require a deliberate fixture/version review.

`tests/test_exact_search.py` checks truth tables, document versus phrase scope,
late exclusions, exact normalization, malformed/unsupported queries, bounded
parsing, serialization, and known fixture results. Run:

```bash
PYTHONPATH=src python -m pytest -q tests/test_exact_search.py
```

CPU browser and PostgreSQL result-service acceptance remain explicitly pending
in the corpus. A reference matcher pass does not establish database parity,
model recall, or deployment scale. Existing frozen review-acceptance packs
remain unchanged. No runtime schema, model, or deployment configuration changes.
