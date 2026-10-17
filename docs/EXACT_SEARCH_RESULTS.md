# Exact document search

The matter's **Find sources** route enumerates documents using
`recordbench-exact-v2`. The existing **Semantic passage search** continues to
select ranked passages for discovery and answering; its candidate limit does
not constrain exact results. Exact search requires no embedding service,
reranker, model, or PostgreSQL retrieval index.

The default form asks for names, places, or details. Optional refinements let
reviewers include an exact phrase, leave out words, and choose a collection or
saved source set. [Nearby-detail controls](EXACT_SEARCH_PROXIMITY.md) add two
words or phrases, a maximum count of intervening words, and either/first-before
order. These fields treat words literally, including words such as
"and"; reviewers do not need Boolean syntax. A separate collapsed **Advanced
search** form accepts the grammar directly. Results lead with source names,
original-text excerpts with safely escaped highlights, page/section links,
and an **Open source** action. Counts and preparation gaps use plain language.
Each search form preserves its own mode when submitted with Enter. Advanced
search has its own collection and source-set controls.

## Current behavior

The route authorizes the matter before reading its population. Collection and
source-set filters intersect, including an empty intersection. Unknown and
foreign scopes fail closed. Matching and counting use only this scoped
population. Ready sources with searchable text are eligible; not-ready and
empty-text sources appear as separate exclusion counts. PDFs with missing or
unverified page coverage are excluded entirely, including for negation queries,
and a visible warning says their incomplete pages were left out. Exact counts
apply only to the eligible fully covered extracted population. A blank extracted
page cannot be distinguished from a page needing OCR by this stored metadata;
the exclusion is deliberately conservative. A source load failure
invalidates the scan rather than becoming a nonmatch.
DOCX, email, CSV, TSV and XLSX counts represent the exact number of extracted
sections. Their records must retain that count and consecutive section numbers
in extraction order. Missing, duplicate or misnumbered sections invalidate the
entire scan, including positive and negation queries, instead of producing an
apparently exact zero or an exclusion-only match. Text-file line counts and
image-frame counts are not interpreted as section counts.

AND, OR, and NOT apply across all extracted units of one document. For example,
`red AND bicycle NOT blue` rejects a document with `blue` on its final page,
even if the other words occur earlier. Phrases require consecutive normalized
tokens within one unit. The [grammar contract](EXACT_SEARCH.md) defines
normalization, punctuation, limits, and unsupported syntax.

Totals count documents after the whole population is evaluated. Sorting uses
case-folded source name and document ID; pages contain 25, 50, or 100 documents.
More than 100 matches remain browsable. Up to three units containing positive
query terms from a satisfied Boolean proof explain each result, with visibly
shortened text previews. One supporting unit per positive part of that proof
is preferred before filling the display cap; a failed OR branch does not
supply misleading highlights. These
preview limits never limit matching or document membership. Phrase previews
anchor on complete consecutive-token phrase occurrences, preserving original
Unicode text. Page links use the position in the extracted-unit sequence, so
unreadable PDF pages do not shift the linked match. Negation-only
matches explicitly state that an excluded word, phrase or combination was
absent. This also covers grouped negation such as `NOT (red AND bicycle)` when
one of those words is present, and alternatives between negated conditions.
DOCX previews
label extracted locations as sections, matching source review. Source links use the
current version's existing source-review token. Transcript links carry the
matching timestamp, server-visible extracted-unit ordinal for transcript pagination, and segment anchor.

Pagination links carry a fingerprint of the grammar, original/parsed query,
matter and selected scopes, source membership, names, states, versions, and
extracted text. Each page reruns the bounded scan. Any change produces HTTP 409
and asks the user to rerun; no stale page or silently mixed result population
is returned. This detects change between requests, not after a response is sent.
The result is a live view, not a frozen receipt. No save-result action or new
storage schema is introduced.

## Backend and operating contract

`ExactSearchBackend` is separate from hybrid answer retrieval. The initial
`ReferenceExactSearchBackend` reads authoritative extracted units in either a
CPU-only or PostgreSQL-configured deployment. It never delegates exact
membership to PostgreSQL web-search syntax or a top-k candidate list.

`ExactScanPolicy` is injectable product policy, independent of development-host
hardware. Defaults are 10,000 scoped documents, 10 million extracted characters,
128 million serialized input characters, 8 million serialized characters per
derived unit record, and a five-second cooperative processing deadline.
File-backed units stream through the [bounded reader](DERIVED_TEXT_READING.md):
input is charged after each bounded read, records are capped before JSON decode,
and decoded text is charged before retaining each unit. Oversized or unfinished
records fail the scan budget before decoding; a character limit reached partway
through a source stops without reading its remaining units. Deadline checks run
before and after reads and decoding, between sources, within tokenization,
within phrase evaluation, and through preview
generation for the displayed page. Previews are built inside the backend before
the result is returned; budget exhaustion there also suppresses the entire result. Exhaustion produces
HTTP 503 with **no exact total or partial results** and guidance to select a
smaller scope. The time budget is cooperative: one bounded source-file read or
record decode, Unicode normalization, and lock acquisition are not preempted. This is not a
hard wall-clock service guarantee. A scan holds the matter's source mutation
lock, captures its collection/set membership under the workspace lock, and
releases the workspace lock before matching. Other matters can continue using
workspace storage during the scan. Source changes in the searched matter wait
for the scan; membership edits during a scan affect the next request, where the
fingerprint detects a changed result population.

Synthetic file-backed regressions cover a result at its configured character
limit, overflow before an unread large tail, a slow read that expires before
decoding, serialized whitespace exhaustion, and oversized complete or unfinished
first records. Existing cross-unit Boolean proofs and proximity results retain
their semantics; invalid source tails still invalidate the entire scan.
Decoded unit numbers, text and displayed locator types are validated inside the
source-reading boundary. Corrupt locators produce the documented unavailable
response rather than a server error or an apparently exact partial total.
Line ranges reject an endpoint before their start while retaining valid
single-line ranges and zero-based legacy transcript offsets.
Pagination fingerprints include both media timestamp endpoints, even when line
locators and text remain unchanged; retiming a media projection invalidates the
previous result fingerprint and requires a fresh search.
Timed media projections must also have consecutive segment numbers, paired
timestamps, nondecreasing start times, strictly later endpoints and endpoints
within the producer's two-second duration allowance. These checks apply to
inline and file-backed projections; corrupt or mixed timed/untimed locators
make the scan unavailable before it can return misleading seek links. Equal
starts and overlapping segments remain valid. Wholly untimed legacy media
keeps its ordinary unit links, and PDF page numbering retains its separate
coverage rules.
Timed transcripts must also retain the exact integer segment count recorded by
the transcript installer. A syntactically valid projection missing its final
segment makes the scan unavailable, even when the remaining timestamps and
numbers are valid. This prevents both false exact zeroes and exclusion-only
matches caused by a lost segment. The count check applies to timed projections;
untimed legacy media retains its existing count interpretation. Synthetic
regressions use the production transcript projector and installer, remove the
last file-backed record, and check both positive and negation queries.

This initial adapter deliberately rescans each page. It is not the scalable
indexed backend. A production-scale adapter should use a consistent database
snapshot or versioned result cache, stable keyset pagination, cancellable query
execution, and index completeness/version checks. It must implement the same
grammar and document/unit boundaries, authorize and scope before counts, expose
unprocessed sources, and return all matches without semantic additions. Index
lag cannot silently turn into zero matches. Cache keys must include actor
authorization, matter, scope, grammar, query, and source versions; authorization
must be rechecked on each read. Persisting a frozen receipt later requires its
query, grammar/plan, scope, source versions and membership, plus schema migration
and backup/restore evidence. Those capabilities remain unbuilt.

## Validation and limits

Synthetic tests run the frozen known-answer corpus through the result service,
enumerate 137 matches with stable pagination, cover different-page NOT and phrase
boundaries, exclusions, invalid syntax, scope intersections, foreign-matter
canaries, source mutations, and budget/read failures. The route test forbids
calling the ranked retriever. Plain-form tests cover literal operator words,
include/phrase/exclude combinations, filter intersections, retained pagination
inputs, empty-input guidance, original-text previews, skipped-page links,
late phrase matches after isolated words, button-free advanced submissions,
transcript timestamps, conservative partial-PDF coverage, satisfied-branch
explanations, and a preview-stage deadline regression. Browser checks cover
the first-use form, a successful search, no-result guidance, keyboard submission
with visible focus, and a 390-pixel viewport without horizontal overflow.
The shared grammar's truth tables remain required.
Production ingestion adapters supply synthetic DOCX, email, CSV, TSV and XLSX
projections for section-count/numbering corruption tests. The DOCX route checks
the section label and follows its extracted-unit link into source review;
plain and advanced exclusion-only routes check the explicit absence explanation.
On macOS these DOCX fixtures run the production section parser in process to
avoid the existing child address-space limit incompatibility, then use the
normal ingestion/projection adapter. On Linux they use the extraction subprocess.

Real PostgreSQL indexed exact-search acceptance is pending; no PostgreSQL
exact adapter is claimed. The same authoritative scan is used when a PostgreSQL
answer backend is configured. Large-population latency, concurrent-user load,
and persisted-search restore acceptance require the future adapter and separate
evidence. No deployment or confidential-workload acceptance is implied.

Concurrent browser searches first check membership in a short synchronous
dependency, keeping storage work off the async event loop. Authorized requests
with validated search inputs then use non-queuing asynchronous admission before expensive scan dispatch:
one active request per authorized matter ID and at most four per app instance.
Extra authorized requests receive HTTP 429 with a short retry hint; inaccessible
matters return the same 404 whether idle or busy. This keeps waiting scans from
occupying every shared request worker; the scan/preview deadline and matter
source-version lock remain independent controls.

## September 9 cleanup on the accepted baseline

The Find sources and proximity components are rebuilt on the selected accepted
baseline without the deferred Reports integration. Typed controls, matter
membership, saved scope and query syntax are checked before scan admission.
Empty forms and invalid submissions keep their normal form/error responses
while another scan is busy; valid concurrent scans still receive a retryable
429 before scan worker dispatch. The authorization lookup remains a short
synchronous dependency, and slots use the authorized matter ID.

Transcript previews format the production transcript projection's millisecond
positions as recording timestamps and retain seek links and segment anchors.
A unit explaining several positive conditions shares its bounded excerpt
allowance across those conditions, including both ends of long proximity spans.
Ellipses mark omitted text. This remains a bounded reference scan, not an indexed
or persisted search receipt; all existing coverage exclusions and fingerprint
checks apply.

Empty media projections with a retained nonzero segment count invalidate the scan,
even when no timestamped record remains. Nonempty stored excerpt digests must
match the decoded text before matching; altered projections cannot supply new
evidence or hide the original text behind an exact total. Legacy units without
an excerpt digest remain readable. Synthetic regressions truncate a production
transcript to an empty array and alter text while retaining its stored digest.

Legacy media projections retain millisecond offsets in the line fields. Search
previews use those offsets for the timestamp label and recording seek URL, and
use the extracted passage position for the transcript segment anchor. Synthetic
route checks open primary and additional later-passage links, including a zero
offset, without treating a legacy unit number as a segment ordinal.

Transcript passage links carry a server-visible passage ordinal as well as the playback offset, so matching passages beyond the first 200 segments open on the correct page even at zero or overlapping timestamps. TXT projections must retain consecutive extracted chunk numbers; a missing middle chunk makes the exact scan unavailable instead of returning an exact zero or accepting a negated term.

Ready TXT ingestion persists the retained chunk count in the existing completed/total extraction-unit fields, while `page_count` remains the original line count. Exact scans verify that count, including the final record; whitespace-only omitted chunks do not become invented searchable units. Legacy TXT metadata used line progress counts: a complete final line range remains sufficient, while an unverifiable tail requires reprocessing to establish a chunk count. Timed transcript text must be nonblank, matching the ingestion invariant. Recording links accept the same two-second endpoint allowance as transcript ingestion, then bound player seek to the actual recording duration.

TXT line ranges must also match the extractor's 20-line block boundaries, stay
within the original line count, and advance without overlap. Aligned gaps from
omitted whitespace-only blocks remain valid. Missing, impossible or shortened
ranges invalidate the scan before any positive or negated exact total is shown.

PDF projections must not repeat a page record. Duplicate pages invalidate the
scan even when the distinct page numbers cover the entire source; they cannot
inflate matching-passage counts or contribute extra evidence. Recording overflow
guidance consistently describes matching transcript passages.
