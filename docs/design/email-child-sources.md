# MBOX intake and email attachment child sources

Status: design only. Product behavior does not change with this note.

## Goal and invariants

This design makes each message in an MBOX mailbox and each processable email
attachment independently reviewable. Each child has its own stored bytes,
extracted text, citations and coverage state. The mailbox or email remains a
citable parent. Parent, child and thread connections are relations, not copied
text.

The design preserves these evidence invariants:

- admitted source bytes are immutable after hashing;
- every citation resolves to one stored source version and a locator within its
  representation;
- derived text and indexes remain separate from source bytes; and
- creating children never changes the identity or citation basis of a parent.

This design extends the attachment-boundary behavior in
[Email attachment coverage](../EMAIL_ATTACHMENT_COVERAGE.md). It does not make
attachment text part of the parent email body.

## Terms and stored identities

A **parent** is an admitted MBOX, standalone email or attached message. A
**child** is a message split from an MBOX or an attachment materialized from an
email. An attached message can be both a child and the parent of its own
attachments. A **source version** identifies immutable admitted bytes. An
**occurrence** records an intake event, including a fresh upload of bytes already
known to the system.

Each child stores its ordinary `source_id` and immutable `source_version_id`, the
owning matter and occurrence, the parent `source_version_id`, a canonical child
path, the SHA-256 digest and byte length of its exact bytes, its materialization
rule, and its processing and coverage state.

The canonical path is structural, not based on a filename, Content-ID, subject,
Message-ID or sender. Those fields are untrusted, optional and non-unique. For an
MBOX message it is `mbox/message/<ordinal>`, with a zero-based ordinal in mailbox
order. For a MIME attachment it appends `mime/<part-index>` for each edge from
the message root, using zero-based sibling indices from the parsed MIME tree.
For example, `mbox/message/1/mime/2/mime/0` is stable even when names are missing
or repeated. Parser normalization must not reorder MIME siblings.

The child identity input is the tuple:

```text
(parent source_version_id, canonical child path, child bytes SHA-256)
```

A versioned, domain-separated encoding of that tuple is hashed to derive the
child `source_id`; the immutable child bytes derive the corresponding
`source_version_id` under the ordinary source-version rule. The exact encoding
and algorithm must be specified before implementation. It must include field
lengths and the identity-scheme version, rather than ambiguous concatenation.
Within one parent version, the tuple is unique. Reprocessing with the same parser
contract upserts the same child and relations. A changed parent version, path or
child digest produces a different identity.

The child digest covers its exact stored bytes. For a MIME part, these are the
decoded transfer payload after successful MIME transfer decoding, not extracted
text or a reserialized message. For an attached message, the stored payload is
the decoded message payload supplied by the enclosing part. Extraction
provenance records the decoder and parser contract. A decoding failure creates a
coverage state and no citable child bytes.

## MBOX splitting

The admitted MBOX remains the parent with its original bytes, source ID, source
version and current citation behavior. A bounded streaming splitter identifies
message boundaries without rewriting the mailbox. It creates one message child
per complete message, in mailbox order. Each message child stores its raw message
bytes, zero-based ordinal, half-open start and end byte offsets in the original
MBOX, and the separator and escaping convention needed to verify that selection.

Mailbox offsets are provenance locators back to the original, not the character
offsets used by citations. A verifier must be able to read the parent byte range
and reproduce or validate the child's stored bytes under the recorded MBOX
convention. A malformed boundary, truncated message or unverifiable range is
reported in coverage rather than guessed.

Intake enforces limits before committing a family. At minimum, limits cover MBOX
input bytes, message count, individual message bytes, cumulative materialized
child bytes, MIME depth, parts per message and extracted text. Limits are
configuration with recorded effective values, not identity inputs. Initial
numeric values remain an open question. Exceeding a limit stops further
expansion, retains safely admitted sources, and records which remaining scope was
not processed. Implementation must define an atomic commit boundary so a retry
cannot duplicate children.

## Worked synthetic example

Consider `mailbox.mbox`, admitted as source version `V0`, containing three
synthetic messages:

| Ordinal | MBOX byte range | Canonical path | Contents |
| --- | ---: | --- | --- |
| 0 | `[0, 840)` | `mbox/message/0` | text message |
| 1 | `[840, 4200)` | `mbox/message/1` | body, PDF at MIME part 2, attached message at MIME part 3 |
| 2 | `[4200, 5100)` | `mbox/message/2` | text message |

Let the exact message digests be `D0`, `D1` and `D2`. The three identities derive
from `(V0, mbox/message/0, D0)`, `(V0, mbox/message/1, D1)` and
`(V0, mbox/message/2, D2)`. Reprocessing `V0` with the same parser contract
resolves those same identities.

Message 1's PDF bytes have digest `DP`, so its identity derives from
`(message-1-version, mime/2, DP)`. Its attached message bytes have digest `DN`,
so that identity derives from `(message-1-version, mime/3, DN)`. If the attached
message contains an attachment at its first MIME part, that grandchild uses the
attached message's version and `mime/0`; ancestry is recovered through relations,
not by placing all ancestry into one ID. The PDF and attached message each have
their own text, processing state, coverage and citations. Neither attachment's
text is copied into message 1 or the MBOX.

A second upload of the same MBOX creates a new occurrence under the existing
fresh-upload rule. Identity deduplication, if retained, must not collapse
occurrence, custody or audit records.

## Family and thread relations

Relations are typed edges between source versions:

- `contains_message`: MBOX parent to message child, with ordinal and byte range;
- `has_attachment`: email parent to attachment child, with canonical MIME path
  and safe display metadata;
- `attached_message`: the specific edge for a `message/*` child that may itself
  have children; and
- `in_thread`: message source version to the thread identity produced from spec
  k's thread key.

Implementation must reuse spec k's exact thread-key contract. This note does not
define or infer a replacement thread key. Thread membership does not create a new
evidence source or imply every message in a thread is present. Relations are
matter-scoped, carry provenance and are idempotently upserted. Deleting or
quarantining a family member does not copy, merge or rewrite another member's
text.

## Extraction and citations

Each successfully materialized child enters the ordinary extraction, indexing,
review and citation pipeline using its own source version. A citation to a child
binds the child's `source_id`, `source_version_id`, representation, content
digest and locator. Resolution reads the child's stored bytes or a derived
representation verified against that version. It never resolves through the
parent byte offset alone.

MBOX and standalone email parents remain citable exactly as before. Existing
parent extraction units and saved citations continue to resolve against their
historical source version. New child citations do not redirect, supersede or
repair a parent citation. Relations may help navigation, but are not part of
citation resolution and are not evidence by themselves.

## Coverage and failure states

Coverage is recorded per expected child boundary and summarized for the family.
A child that is unsupported, rejected or fails processing remains visible with a
stable reason code and plain explanation. Reasons include unsupported media type,
malformed transfer encoding, parser failure, malware detection, scanner
unavailability, encrypted content, size or count limit, excessive nesting and
missing bytes. The record distinguishes a known child with no admitted bytes
from an admitted child whose extraction failed.

Coverage must not claim all attachment content was reviewed when a child is
pending, excluded, quarantined or failed. It reports bounded facts: discovered,
admitted, searchable and incomplete children. Retrying a transient failure
updates processing state without changing identity. A content change requires a
new source version rather than editing stored bytes.

## Limits and safety boundaries

Every materialized child passes the same validation, capacity and malware policy
as a direct upload before extraction or indexing. Malware scanning is per child,
including an attached message, before its content becomes searchable. A detected
or unscannable child is quarantined or blocked under the existing fail-closed
policy while the parent retains its own scan decision. Coverage exposes the
blocked child without exposing unsafe bytes.

Nested `message/*` parts expand only within configured depth, part-count, byte
and time budgets. A cycle guard keys on the active ancestry of source-version
digests; a repeated digest is reported rather than recursively expanded.
Globally repeated content is not itself an error because separate occurrences
and MIME positions remain meaningful.

Archive files inside attachments are stored as attachment children but are not
expanded by this design. PST intake and PST containers are out of scope. Archive
expansion is out of scope. This note adds no license gate. If a later
implementation uses Docling, OCR is pinned to Tesseract or Nemotron OCR and the
RapidOCR extra is excluded.

## Compatibility, migration and rollback

This is additive for new processing only. Existing email and MBOX records are not
silently reprocessed, and their stored extraction, source IDs, source versions
and saved citations do not change. A fresh upload creates a new occurrence under
the same rule used today. Child creation must be gated by an explicit processing
contract version so historical records cannot acquire children merely because a
reader or worker was upgraded.

Migration adds relation, provenance and coverage data only for newly processed
occurrences; it does not rewrite source identities or citation rows. Rollback
stops creating and displaying child relations while retaining admitted child
sources and their audit history. No source ID or saved citation changes during
migration or rollback. A previous reader continues to resolve all pre-existing
citations; child citations require a reader that understands those ordinary
stored sources, even if it cannot display family navigation.

## Open questions for Neil

These questions are intentionally not decided by this note:

1. What initial limits apply to mailbox bytes, message count, individual message
   bytes, cumulative children, MIME depth and processing time?
2. Should over-limit intake commit a bounded prefix of safely admitted messages
   or fail the new family atomically before any child becomes visible?
3. Which versioned encoding and hash namespace derives child source IDs, and
   should the identity scheme be shared with other container formats?
4. Should transfer-decoded bytes be retained alone, or with the encoded parent
   range when an exact contiguous range is available?
5. Which `message/*` subtypes may recursively become parents rather than terminal
   children?
6. How may users request child creation for an existing email without silent
   reprocessing: explicit re-upload only, or a separately authorized migration?
7. What retention and deletion behavior applies when a parent is removed but a
   child has saved citations or review work?
8. Which family coverage counts and reason labels appear in the first interface,
   and which remain export-only metadata?
9. May a fresh occurrence reuse scanned immutable child bytes while recording a
   distinct scan decision and occurrence audit trail?

## Implementation acceptance checklist

- [ ] Identity derives from parent version, canonical MIME path and exact child
  digest, and repeated processing is idempotent.
- [ ] A three-message MBOX produces three sources and preserves original ranges.
- [ ] A PDF and nested message become distinct children; the nested message can
  parent its own bounded children.
- [ ] Parent, child and spec k thread membership are relations without copied
  attachment text.
- [ ] Child citations resolve to child bytes while parent citations stay valid.
- [ ] Existing records are not silently reprocessed; a fresh upload creates a
  distinct occurrence.
- [ ] Every unsupported, failed, pending or blocked child has coverage and a
  reason.
- [ ] Limits, recursion guards and per-child malware scans fail closed; PST and
  archive expansion remain out of scope.
- [ ] Migration and rollback leave every source ID and saved citation unchanged.
- [ ] Neil's open questions are resolved in an implementation specification, not
  implicitly in code.
