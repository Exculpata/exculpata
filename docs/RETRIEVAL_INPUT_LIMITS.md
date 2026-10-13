# Extraction and model-input limits

Accepted source text can contain an extracted page, section, transcript segment,
or line group longer than a model-worker request permits. The retrieval client
splits these units into ordered spans of at most 20,000 characters for model
requests. Original extracted text, unit ordinals, source versions, excerpt
digests, page/section/line/timestamp locators and saved evidence remain unchanged.
Existing source registries and saved citations need no migration or re-extraction.
Original upload and extraction limits are unchanged.

The worker and clients share the existing limits: 48 embedding inputs or 40
reranking inputs per request, a 512-character reranking query, and a maximum
1,048,576-byte JSON request. Clients count the exact UTF-8 JSON bytes sent,
including escaping, separators and query overhead, and start another batch
before either the item or byte limit is reached. All fragment responses must
have valid counts and finite numbers, with consistent embedding dimensions,
before a document index replacement can begin. A failed later request does not
publish a partial document index.

One embedding vector remains attached to each original unit. For a split unit,
the client takes a character-weighted mean of its fragment vectors and normalizes
the result for cosine search. A unit within the limit retains its original
single-vector behavior. Reranking uses the highest fragment score for the
original candidate. Whitespace-only spans are retained in source evidence but
do not create model requests. This preserves citation identity, not exact
equivalence to an unbounded model input: fragment boundaries may divide context,
embedding pooling can dilute a localized match, and model token limits can still
truncate a character-bounded input. Synthetic contract tests establish transport
and evidence integrity; they do not establish learned-model recall or certify
complete recognition.

Worker input rejections (HTTP 413 or 422) produce a fixed, actionable message
without exposing the worker response or source content. Source preparation records
the rejection durably using the existing processing-stage field. The source
shows the administrator/re-import action instead of an unchanged-input retry,
including after restart; direct retry requests are rejected. Transient service
failures retain their existing retry behavior. Administrators should align the
application and retrieval-worker versions and limits before re-importing the
source. Native files remain saved until the reviewer chooses to remove them.
