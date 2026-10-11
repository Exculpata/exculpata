# Email attachment coverage

Status: implemented and locally validated. The [review policy](QUALITY_GATES.md#review-policy)
applies before merge. No runtime activation is implied.

## Outcome and boundaries

Show attachment names/types with an explicit unprocessed-content notice in
newly extracted email headers. Treat attached messages and multipart attachments
as boundaries: their descendant text must not become parent-message body.
Every non-report `message/*` child is an attachment boundary, including unnamed
news, partial, HTTP, external-body and extension message types.
Related email selects its root by the `start` Content-ID or first child, inventories
other resources without reading them into the body, and rejects missing/ambiguous
roots, including explicitly empty root references. Surrounding Content-ID comments
and folding whitespace are normalized before matching and ambiguity checks.
The selected body may have its own filename or inline disposition. See
[related MIME root semantics](https://www.rfc-editor.org/rfc/rfc2387.html).
Inventory unnamed non-body parts too, including explicitly empty or whitespace-only
filename parameters. Keep container, MIME-part and decoded-body
limits. Attachment child sources, archive expansion and PST support remain
separate work.

Surface the coverage limitation in source review, matter search/answers and
saved answer and investigation exports. Email readiness means its extracted text is searchable;
it must not imply complete attachment coverage. Count email sources through the
existing matter-scoped catalog; do not read all source files to display coverage.

## Compatibility and recovery

Preserve existing stored passages and citations. Earlier extraction may contain
attached-message text mixed into parent body, and its attachment inventory may
be incomplete. The notice for existing email must acknowledge incomplete
attachment coverage without claiming that all attachment text is absent. Do not
silently reprocess saved evidence or change its citation basis. Review the
original email and attachments separately when completeness matters; a fresh
upload creates a distinct occurrence processed with the corrected extractor.

No registry fields, source IDs, source versions, schema or dependencies change.
Source rollback retains already saved extraction and its existing
citations. An older reader lacks the new general coverage notices. Saved answers
created before this change retain their historical coverage snapshot.

## Coverage contract

For a matter containing email, `partial_query` is true whenever queries are
otherwise available, even when every received source has searchable text.
`excluded_count` still counts excluded source files and remains zero for a ready
email-only matter. The notice describes the limitation without asserting that
all attachment text is absent from older extractions. It is conservative for
email without attachments, because old source records do not carry a complete
structured attachment inventory. No source-file read or new inventory is added
to readiness queries. Coverage links open all sources so ready email remains
reachable alongside sources needing attention, including after live status
updates in Review and Quick question. Dedicated processing-recovery actions keep
their existing destinations. Source review also shows the notice before searchable
content; new header units list each attachment boundary, with unknown names
shown as **unnamed**. Descendants of attached messages are not separately listed.

Parser-reported MIME defects, including malformed containers and body-transfer
decoding defects and defects or duplicate singleton MIME classification headers, fail
as a source-processing error with existing
**Try again** and removal recovery. Correct a damaged original before a fresh
upload. Earlier saved answers keep their historical snapshot; new answers,
including the legacy synchronous path, save current attachment coverage.
Coverage is retained in answer/conversation Markdown and Word exports and the
ordinary matter bundle. Investigations refresh coverage from the source catalog
after final evidence validation because later retrieval passes can include newly
uploaded sources. The saved result carries that completion-time coverage snapshot. Queued answers
and the synchronous compatibility path likewise refresh source counts and coverage
after generation so newly retrieved email cannot retain an earlier complete notice.
These counts describe current source availability, not a ledger of sources
searched. The first retrieval fingerprints the matter-scoped catalog
IDs, versions, content-basis digests, states, media types, processing jobs and
pending uploads not yet bound to a catalog source. An investigation retains this
first boundary through every pass, so a later search cannot erase a change that
earlier queries missed. A selected source set restricts the metadata stream to
its member documents and adds its identity and membership to the fingerprint,
captured under the control lock before retrieval begins. Unrelated sources and
unbound uploads outside that set do not invalidate its findings. Edits to other
sets do not change that scope boundary; foreign sets are refused. This streams metadata
without reading source bytes or materializing a second source list. If that boundary
changes during retrieval or generation, saved coverage is partial and explains that
newly available material may be absent and the question should be run again.
Equal-count source swaps and uncited version changes are detectable too. Final
counts, coverage and ordinary answer scope are refreshed under the existing source
mutation guard used for citation validation and durable result saving, with the
workspace control lock held from the coverage read through saving. This also
serializes pending-upload changes that do not yet have a catalog source, so a change
between generation and persistence cannot escape the notice. Queued work carries
the boundary only as internal completion metadata; storage strips the transient
research completion field before saving or exporting a finished result. Focused answers identify their actual cited support without
claiming to have searched every source in the completion-time count. Sources still
uploading or processing count as excluded and receive a plain recovery notice.
Playback/direct-review guidance remains present when recordings and pending
uploads coexist. An unchanged
complete availability state likewise does not mean an every-source review.
Research checkpoints retain one optional internal `retrieval_source_fingerprint`
field in their existing JSON. On recovery, a missing or changed boundary repeats
the bounded search plan and resets stale checkpoint/progress atomically while
retaining the job and plan; cancellation still wins. Older readers preserve the
field but do not apply the new recovery rule. No schema migration is needed and
the fingerprint is absent from staff status and completed/exported results.
Investigation results combine source/attachment coverage
with their focused-search caution; Markdown, Word, JSON and complete-bundle
investigation exports retain both notices. The second child of `multipart/report`
is machine report metadata when it is `message/*` and its subtype matches the
container’s `report-type`; this includes delivery,
read-receipt, feedback and extension reports. A filename or attachment disposition
still identifies an attachment. Returned messages and parts outside that matching
report context remain attachment boundaries. Current extraction
uses the human-readable report body and does not extract machine report fields.
See the [multipart/report definition](https://www.rfc-editor.org/rfc/rfc3462.html),
[read-receipt format](https://www.rfc-editor.org/rfc/rfc8098.html), and
[feedback report format](https://www.rfc-editor.org/rfc/rfc5965.html).
This change does not unpack attachments or provide a
new email application; use the original email to review attachments separately.

## Synthetic acceptance

```console
python -m pytest -q tests/test_email_attachment_coverage.py tests/test_answer_jobs.py tests/test_processing_awareness.py tests/test_work_product_exports.py
python scripts/browser-accept-email-coverage.py --chrome-binary /path/to/chrome --chromedriver /path/to/chromedriver --output /tmp/generated-email-coverage
python scripts/verify-email-coverage-rollback.py --previous-source /path/to/previous-checkout
make check
```

Six initial failing parser cases reproduced attached-text leakage, missing
inventory/coverage and malformed-container readiness. Eighty-nine parser/HTTP tests now
pass; the combined email, investigation and readiness selection passes 117 tests; including answer-job contracts passes 129. New failing
regressions reproduce lost investigation coverage and invented delivery-report
attachments before their corrections. Related-resource/root and mixed-navigation
regressions also fail before correction and pass afterward. Unnamed encapsulated
message cases reproduce leakage for five additional subtypes; corrected HTTP
upload, search, answer export and isolation checks also cover an attached news message.
Further regressions reproduce empty root references, decoding-time base64 defects
and an email indexed between live investigation passes; the corrected result
retains exact new-source support and updated coverage through every export. Both
ordinary answer paths also reproduce and correct a concurrent email upload before
retrieval, with exact support and answer/conversation exports checked. Additional
regressions reject malformed MIME type, disposition and transfer-encoding headers
and upload a normal text source during final generation in both answer paths and
investigations. The latter retain exact earlier support and a late-availability
notice in saved Markdown and Word exports. Equal-count swaps reproduce the
same missing notice before fingerprinting. Queued answer and investigation
regressions also pause immediately before saving and reproduce both a new upload
and an equal-count swap; the saved availability, scope and exports now agree.
Pending-upload regressions create a real resumable upload before bytes arrive,
during generation and final saving; all five paths preserve partial coverage and
exact earlier support. Four duplicate-singleton MIME header cases fail before
correction and now report ordinary processing failure. Recovery after the final checkpoint
repeats searches for both current and legacy checkpoints, resolves new source
support and preserves it in Markdown, JSON and Word. Empty filenames, commented
and folded Content-IDs, normalized ambiguity and matter-scoped version changes
have regressions. Text uploads between investigation passes retain a rerun notice.
Selected-set additions before search, during generation and before saving preserve
partial coverage and exact earlier support; recovery repeats stale scoped passes.
Mixed recording/upload notices retain playback guidance. Matching report data,
returned original messages and report-context mismatches have parser contracts. Fixtures
cover ordinary, unnamed, inline non-body, attached-message and attached-multipart
parts, HTML alternatives, MIME/body limits, exact parent passages, attachment-only
no-match searches, saved coverage and cross-matter access. The stopped-reader
script reproduces the old attached-message extraction, preserves every old
unit/digest/version on upgrade, checks corrected new extraction, reads both with
the older application and exports the saved notice, then verifies forward read
and unchanged original bytes. It also checks the optional research checkpoint
field through a stopped previous reader and current forward reader. No new storage contract requires migration.

September 8, 2026 acceptance: `make check` passes 1045 application tests with nine
optional skips, all 194 transcription tests, compilation, both Compose graphs and
publication inspection. All eight Chrome workflows pass: actual selected-file
upload/receipt/source inventory, desktop/narrow review, attachment-only no-match
search, exact parent support saved to notes, ordinary question submission and
saved answer download/reload, investigation notices and standalone evidence-ledger
download, two-tab mixed-coverage refresh/navigation, cross-matter denial, final bundle and owner closure
with unchanged external email bytes. Desktop and narrow screenshots were
inspected. The stopped-reader/forward-read drill also resolves the support token
created by the old extractor to the same exact old source version and passage.

Next action: follow the [review policy](QUALITY_GATES.md#review-policy) and let each deployment perform its own integration and
activation checks. Attachment child-source extraction remains a separate slice.
