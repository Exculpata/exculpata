# Email threading

The email-threading library reads synthetic or user-supplied EML and MBOX files
locally. It is local-first by default and is not wired into intake or product
behavior.

`read_eml` and `read_mbox` return immutable `EmailRecord` values. Parsing uses
the decoding and size limits from `extended_extract`; a malformed message is an
error record rather than an exception that ends a mailbox batch. MBOX reading
also has a 500-message default limit.

`group_threads` prefers explicit `Message-ID`, `In-Reply-To`, and `References`
links. It selects the nearest reference that exists in the parsed set, so a
broken reference does not discard a later valid link. When explicit headers are
missing, normalized subjects can link a message and the resulting `ThreadNode`
sets `weak_link` to `True`. Callers should not treat that fallback as equivalent
to an explicit header relationship.

`detect_quoted_history` identifies contiguous `>` lines, `On ... wrote:`
lead-ins, original-message separators, and forwarded-message blocks. Each
`QuotedSpan` contains half-open character offsets into the decoded body, the
exact sliced text, and a normalized message ID when the block contains one.
These deterministic patterns support measured recall; text outside a detected
span must not be assumed to be newly written text without further review.

The library does not persist records, alter extraction output, or connect
threads to intake. That integration remains follow-up work.
