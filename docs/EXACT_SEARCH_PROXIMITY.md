# Find nearby details in sources

In **Find sources → Narrow your search**, enter a **First detail** and **Second
detail**, then choose how many words may appear between them and whether their
order matters. Each detail is a literal word or phrase; no expression syntax is
required. Choose **Apply filters** or press Enter. Include/exclude words,
collections, and saved source sets continue to apply to the same full source
population. Proximity-only searches can leave the main words field empty.

Advanced search accepts `NEAR/n` in either order and `BEFORE/n` in the written
order. For example, `"red bicycle" BEFORE/4 depot` finds that phrase before
`depot`, with at most four intervening word tokens. With `NEAR/4`, the depot can
appear on either side. The v2 plan records the original and normalized query,
`recordbench-exact-v2` grammar, `recordbench-words-v1` tokenizer, direction,
maximum intervening words, operand phrases, and extracted-unit boundary.

## Exact positional meaning

Both operands must match distinct, non-overlapping occurrences in the same
extracted unit. A unit is the parser's existing page, section, or transcript
segment. Proximity never crosses unit boundaries, including adjacent pages or
transcript segments. This is word distance, not physical layout or elapsed time.

Distance is the number of tokenizer words between the end of the earlier full
operand and the start of the later full operand. Operand words do not count
toward the gap. A gap of zero means adjacent operands; distances from 0 through
100 are supported. `red NEAR/0 red` needs two red occurrences. Overlapping phrases
cannot satisfy each other: `"red blue" NEAR/0 "blue green"` does not match
`red blue green`, but it matches `red blue blue green`.

The existing tokenizer applies Unicode NFC and case folding, retains attached
combining marks, internal apostrophes and ASCII hyphens, and treats other
punctuation as separators. Punctuation and whitespace add no word tokens;
letters and numbers do. There is no stop-word removal, OCR layout cleanup,
word rejoining, language-specific segmentation, or correction of OCR mistakes.
An OCR line break between two complete words separates the words normally;
`bi-` followed by a line break and `cycle` does not become `bicycle`. Page numbers
and other OCR artifacts present as letters/numbers count as extracted words.
The [shared tokenizer contract](EXACT_SEARCH.md) applies to both phrases and
proximity.

Proximity binds the two adjacent word/phrase operands before NOT, AND, and OR.
For example, `NOT red NEAR/2 bicycle` excludes documents containing that pair;
`red NEAR/2 bicycle NOT depot` also excludes a depot anywhere in the document.
Group complete proximity pairs with parentheses. Chaining proximity operators,
using Boolean groups as proximity operands, negative/fractional distances,
`NEAR` without a distance, `W/n`, `PRE/n`, `WITHIN`, and `ADJ` fail explicitly.
Wildcards, fuzzy search, and field expressions remain unsupported. Literal
operator words can be quoted; the plain form quotes them automatically.

Existing v1 queries can be parsed explicitly with their original grammar. V1
rejects proximity and retains its previous literal-word behavior, including the
word `before`. The browser now generates v2 queries. No saved-search schema or
frozen-result receipt is introduced; existing result fingerprints include the
new versioned plan and reject changes between pages.

## Matching, previews, and operating limits

Proximity streams sorted occurrence spans through forward scans, retaining
only the current pair rather than complete occurrence lists or a Cartesian
product. Single-word literals use one shared token-set lookup per unit; only
phrases and proximity require positional scans. The exact service still scans the entire authorized
population before returning totals, paginates all matches, rejects incomplete
scans, and exposes unavailable or tokenless sources separately. Positive
proximity previews anchor on a satisfying pair and highlight its complete span,
including intervening words. Previews retain one explaining occurrence per
positive condition rather than every repeated match in a large unit. Long spans show both ends with an explicit
omission between them. Preview length and the three-unit display limit do
not limit matching or membership.

The shared authoritative exact backend is used regardless of the ranked answer
retriever's CPU/PostgreSQL configuration. A routing regression verifies that
PostgreSQL-ready flags do not change proximity membership or invoke ranked
retrieval. This is not live PostgreSQL index parity: no indexed exact adapter is
implemented. The scan budget is 10,000 documents, 10 million extracted
characters, 128 million serialized input characters, 8 million serialized
characters per derived unit record, and a cooperative five-second processing
deadline. File-backed reads and bounded JSON decoding participate in that
deadline; serialized limits apply before decoding and extracted characters are
charged before retaining each unit. See [result budgets](EXACT_SEARCH_RESULTS.md).
Cancellation checks run through token scans and occurrence pairing. Source I/O
and normalization remain cooperative limitations, as documented in the
[exact result contract](EXACT_SEARCH_RESULTS.md).

## Synthetic validation

The SHA-256-fixed fixture at
`tests/fixtures/synthetic/exact-proximity/v2/corpus.json` records invented sources
and expected document membership. Tests cover gap boundaries 0/1/5/100, ordered
and unordered phrases, repeated/overlapping operands, punctuation, numeric OCR
artifacts, decomposed Unicode, unit boundaries, document NOT, grammar versions,
unsupported input, query limits, and normalized-plan round trips. A separate
randomized brute-force oracle checks 300 small token populations against the
position matcher. Full enumeration retains all 137 matches across pages.
Route tests preserve proximity fields in pagination, enforce source-set scope,
fail incomplete plain input, and keep the ranked retriever out of exact search.

A generated local measurement scanned 300 documents containing 1,932,000
characters and found all 300 matches in 0.72 seconds, including displayed-page previews. A separate 100,000-token
repetition case matched in 0.07 seconds. These are observations on the development
host, not latency guarantees for another machine or a concurrent workload.
Browser validation covers plain controls with keyboard submission, the order
selector, advanced expressions, original matching excerpts, source links, and a
390-pixel mobile viewport without horizontal overflow.

```console
PYTHONPATH=src python -m pytest -q tests/test_exact_search.py tests/test_exact_search_results.py tests/test_exact_search_proximity.py
```

No schema, stored-source format, model, deployment, wildcard, or metadata-field
behavior changes are included.

The portable browser acceptance script starts a loopback-only app with temporary
synthetic sources and writes screenshots plus a receipt containing the tested
checkout commit and whether its working tree was clean. It ignores inherited
Exculpata backend/storage environment settings inside the acceptance process
and uses an unavailable model plus deterministic retrieval. Install Selenium and supply matching Chrome/ChromeDriver
binaries for the host; run from the checkout under test:

```console
python scripts/browser-accept-exact-search.py --chrome-binary /path/to/chrome --chromedriver /path/to/chromedriver --output /tmp/exact-search-acceptance
```

The September 9 correction scans both operand orientations together, retaining
only recent occurrences within the maximum gap and operand width. An early
reverse-order match therefore does not exhaust an irrelevant tail first.
Legacy grammar v1 retains its original reserved-token pattern, including ordinary
standalone `W`, `PRE`, and `BEFORE` words. Synthetic regressions cover these
compatibility cases, bounded incremental matching, and mixed-condition previews.
