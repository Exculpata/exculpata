# Source text, saved work and exports

Admitted TXT, CSV and email text can contain nonprinting control characters.
These characters must not turn a source save into a server error or make an
otherwise completed generated answer repeatedly fail when saved.

The `controls-to-spaces-v1` presentation policy starts with an already extracted
passage or saved prose. It replaces each C0 control except
tab, LF and CR, each DEL/C1 control, surrogate code point, and U+FFFE/U+FFFF with
one ordinary space. Replacement never joins adjacent words. Other Unicode,
including combining marks, zero-width joiners and non-joiners, remains intact.
This is a display and derived-prose policy, not an extraction correction or a
claim that a control character had a particular meaning in the original.

The existing TXT extractor has its own line semantics: CR/LF and the boundaries
recognized by Python `splitlines()` (including VT, form feed, U+001C–U+001E and
NEL) delimit extracted lines, which are joined with LF. Those characters can
therefore already be represented as LF in extracted passages. This correction
retains that extraction behavior and its historical line-count limits; it does
not reinterpret decoded original files using the presentation policy. A future
CR/LF-only extraction rule needs explicit versioning/reprocessing treatment so
old saved digests and locators cannot silently acquire a different basis.
That versioned TXT extraction/reprocessing work remains an open follow-up in
[issue #109](https://github.com/Exculpata/exculpata/issues/109); this
bounded save/export correction does not close it.

This presentation correction leaves original uploaded bytes, file digests,
existing extraction text, source versions, passage
digests, support tokens and locator offsets unchanged. Notebook, Report and
analysis citation snapshots now retain exact bounded source text, including
leading/trailing whitespace and its Unicode normalization form. A display copy
must never be hashed or substituted into an original-reference comparison.
Generation verification and the additional recorded-context original-span check
continue to use their original inputs. Full-text frozen records and their digest
basis are unchanged.

New derived prose (answer summaries, case notes, Report sections, analysis
summaries and every-source rationale) is checked against its existing size bound
before applying the policy. It retains the existing NFC/line-ending/edge-space
handling for prose. Identifier, identity and authorization validation remains
strict. Invalid Unicode in a source snapshot receives a controlled error and a
direction to review the original instead of silently rewriting the reference.

Existing saved messages, citations and source records are not migrated or
rewritten. HTML presentation applies the same policy at the template boundary,
while preserving HTML escaping. New notes and Reports made from older saved
answers apply the policy to their prose and preserve original citation text.
Expected source-save errors return to the source with a recovery message;
background-answer validation errors retain the specific failure instead of
replacing it with a generic retry suggestion. Existing draft recovery and
source-change checks still apply.

Known service-authored confidence notices use the
[generated-answer presentation rules](GENERATED_ANSWER_REVIEW.md) before control-character
projection. Structured JSON may present those notices with current wording while
source excerpts, source versions, excerpt digests and citation authority remain
exact. Existing saved payloads are not rewritten.

DOCX serialization applies the policy before XML escaping, including metadata.
In the document body, CRLF and bare CR become LF, each represented by one explicit
Word line break. A separate note reports that line-ending normalization when it
occurs. Body tabs use explicit Word tab nodes, with one-inch tab stops in the
larger title style to retain visible word separation. Metadata
retains supported characters exactly; carriage returns use XML character references
so an XML parser cannot silently change them to LF. Literal text such as `&#13;`
remains literal. Both normalization notes count toward the existing export limit.
Before building paragraph runs, document XML or the ZIP package, DOCX export also
checks a 250,000-run limit and a 100 MiB total uncompressed XML limit. The byte
budget includes escaped UTF-8 text, explicit tab/line-break runs, paragraph and
package markup, metadata and normalization notices. The preflight counts in
bounded chunks without constructing the expanded XML. This prevents tab-heavy
or heavily escaped text from turning an otherwise admitted text payload into an
unbounded allocation or a highly compressed oversized document. The existing
10-million-character text limit is retained; ordinary text within both budgets
keeps its existing representation. A rejected Word export directs the reviewer
to a smaller selection or Markdown without changing sources or saved work.
Markdown and work-product CSV exports use the same presentation policy. DOCX and Markdown
include a note when this serialization changes text; CSV keeps its existing
table shape. Structured JSON and full-text ledger CSV retain exact source
snapshots and ledger text; these structured records are not display copies. The common
DOCX serializer covers answers, conversations, notebooks, Reports, bundles,
investigations, every-source checks, full-text reviews and transcripts. A
portable work-product bundle remains distinct from an importable node backup.
Transcript Markdown uses the common text escaping and replacement notice while
retaining its existing export capacity and timestamp layout. Transcript TXT,
SRT, VTT and CSV also apply the presentation policy;
CSV formula protection follows that projection. Transcript JSON retains the
saved text exactly, including in bundles, and export never updates transcript
revisions, original machine text, timestamp locations or digest bases.

## Regression evidence and limits

Synthetic HTTP regressions reproduce the original TXT source-note HTTP 500,
failed generated-answer state and rejection of an older saved answer's Report
conversion. They exercise successful saves, repeated answer requests, original
byte/digest checks, older payloads, controlled error recovery and DOCX XML parsing.
CSV and email cases use a clean-result scanner stand-in solely to reach the
application's admitted-text workflows. Controlled generation copies synthetic
source passages; it is not real-model quality evidence.

Further regressions cover bounded text and ordinary Unicode, research/analysis/
every-source saves, exports of synthetic legacy records through each common
DOCX path, and exact saved citation recovery using SQLite online backup followed
by a clean database restore and integrity check. No schema migration or source
storage change is required. This bounded control-store restore does not replace
the full installed-node backup and recovery drill.

Small patched-capacity regressions check tab/CR/LF expansion and cumulative runs
before paragraph allocation, exact serialized-byte and run boundaries, metadata,
normalization notices and unchanged ordinary text. They exercise the same limits
without constructing a large document or changing the frozen acceptance cases.

The serializer defect can be reproduced with synthetic legacy records containing
XML-invalid controls. That demonstrates serializer reachability and parser
failure; it does not establish corruption of a production DOCX. Native Mac
media-preflight limitations and exact-candidate hosted Linux results are recorded
in the pull request. Supported installation, scanner, real-model, hardware and
full-node recovery acceptance remain separate gates.
Legacy transcript route regressions seed a synthetic admitted source and saved
revision, then verify individual downloads and bundled Markdown/JSON against
the unchanged stored transcript and original bytes. They do not establish that
today's recording intake or transcript edit validators admit these controls.

This implements R4/revised F5 in
[the independent review tracker](https://github.com/Exculpata/exculpata/issues/109).
