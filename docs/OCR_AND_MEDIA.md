# OCR and media

Uploads are streamed into a private staging boundary and scanned before they
become matter sources. Exculpata extracts PDF and DOCX structure, email and
spreadsheet text, and bounded image content. PDF native text is retained. When
OCR is enabled, selected pages are rendered with Poppler and recognized with
Tesseract. Selective mode processes empty-text pages; expanded mode also selects
pages with little native text, substantial image placements or unknown image
evidence, so a text stamp does not suppress OCR of a scanned body.

XLSX shared-string references must use ASCII decimal integer syntax and identify
an existing entry with a nonnegative index. Negative, empty, malformed or
out-of-range indexes fail extraction instead of substituting unrelated cell text.
A retained failed upload has no searchable units and can be retried or removed
through the usual source workflow. Existing extractions and their citation
identities are not silently rewritten.

OCR page, pixel, byte and time limits protect the shared node. Source status
records attempts and returned text separately from disabled/selective/capped
skips, failures, timeouts, empty results and output limits. Searchable text or a
successful OCR attempt does not establish that every visible part of a page was
read correctly. Review the original PDF and its
[extraction coverage](PDF_OCR_COVERAGE.md), including when a search finds no
matches. Historical extraction and citation identities are not silently replaced.

Audio and video are playable even when transcription fails. Browser-incompatible
video is prepared asynchronously with FFmpeg, using remux or audio-only
conversion before full re-encoding when possible. Playback follows the active
transcript, and cited media timestamps can seek directly to the relevant span.

Before transcription, a bounded offline [recording check](MEDIA_PREFLIGHT.md)
looks for audio and likely speech. Videos without audio remain playable;
uncertain/no-speech results wait for an explicit decision and failed inspection
can be retried. Language is not assessed. The complete original recording is
submitted when transcription proceeds.

The bundled transcription queue reports inspection, submission, queue, transcription,
alignment, diarization, projection, summary, completion, degraded, and failed
states. Exculpata indexes timestamped segments and automatically generates a
source summary after projection. Users can export transcripts, summaries,
clips, citations, and reports.

Speaker diarization produces anonymous clusters, not identities. Reviewers can
open **Review speakers** beside the synchronized transcript or select a speaker
label on any passage. A deliberately confirmed correction applies to every
passage in that transcript with the same cluster and refreshes retrieval; merely
opening the review never confirms a label. The in-page save keeps playback,
scroll, and keyboard focus in place, while the non-JavaScript form carries the
current timestamp, transcript filters, page, and focused passage through its
redirect.

The transcript overview is optional and independent from playback, transcript
review, search, and export. If it fails, the media page shows a staff-readable
cause category, the number of automatic recovery attempts used, the three-attempt
automatic limit, and a deliberate retry action. A manual retry remains available
after that automatic limit so an operator can recover after local answering is
restored without retranscribing the recording.

Keep the transient transcription root out of backup and indexing systems. The
matter-owned original and projected transcript remain governed by the matter’s
own lifecycle; the standalone worker queue is deleted after delivery/expiry.

## Sampled video-frame text: bounded foundation, not yet searchable

Exculpata does **not** currently search visual-only text in video. The portable
codebase now includes a disabled-by-design sampled-frame OCR coordinator and
synthetic contract tests. It caps the number of frames, spreads a capped sample
across the recording, bounds decoded bytes, pixels, OCR text, and per-step time,
continues after individual frame failures, and retains the decoder-reported
presentation timestamp and frame number. Its result explicitly remains
non-searchable. Sampled-frame OCR would recognize text on selected frames only;
it would not check every frame and would not provide general scene, person, or
object understanding.

The next executable slice is:

1. Add an offline FFmpeg/Tesseract adapter that emits one bounded raster per
   planned seek, reads the decoded presentation timestamp and frame number, and
   enforces the coordinator's output and subprocess time limits.
2. Persist recognized text only as a derived unit of the authorized matter,
   document, and source version. Rebuild and purge it with that version; never
   retain temporary rasters.
3. Index those derived units with an evidence-kind marker. Resolve every result
   through a matter-scoped support token that opens the original video at the
   exact timestamp and displays the frame number and sampled-coverage warning.
4. Add isolation, source replacement, export, complete matter purge, citation
   resolution, malformed-media, timeout, and browser acceptance tests. Enable
   the capability in staff UX only after all of those gates pass.
