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

## Check scanned PDF OCR on an installed node

The model API migration keeps document OCR in the app. For scanned PDFs, Poppler
renders selected pages and Tesseract recognizes their text on the node. An
installation using `--models none` can use this path. It requires no provider
URL, credential, matter grant or staged model service. Imported JPEG, PNG and
TIFF images also use local recognition through the image extractor.

The reference app image includes Poppler, Tesseract, and English and Spanish
language data. The generated app environment uses the following settings for
PDFs. On a new node the file is `<node-root>/config/exculpata.env`. Existing
nodes retain their selected app environment file.

```dotenv
CASE_INTELLIGENCE_OCR_MODE=expanded
CASE_INTELLIGENCE_OCR_LANGUAGE=eng
CASE_INTELLIGENCE_MAX_OCR_PAGES=500
```

Choose `selective` to recognize only pages with no native text, up to 25 pages
per PDF. Choose `expanded` to include pages with sparse text or evidence of
scanned images, up to the configured page limit. Set `off` to use only native
PDF text. The language setting also applies to standalone image recognition.

Run `./install doctor --root <node-root>` against the running installation. To
run just the PDF check inside the app container, use
`python -m case_intelligence.ocr_diagnostic`. It uses a packaged synthetic PDF
and a temporary directory. The result `{"ocr": "ok"}` means an exact phrase visible
only in the scanned image was found on page 2 of the same source version. A disabled result
reflects the running app's PDF OCR setting. If the check fails, verify the
app's Poppler and Tesseract installation and the configured language data,
then rerun the check. For a case document, its source status shows how many
pages returned text. Review the original when assessing a missing term.
[PDF extraction coverage](PDF_OCR_COVERAGE.md)

## Audio and video

Audio and video are playable even when transcription fails. Browser-incompatible
video is prepared asynchronously with FFmpeg, using remux or audio-only
conversion before full re-encoding when possible. Playback follows the active
transcript, and cited media timestamps can seek directly to the relevant span.

Before transcription, a bounded offline [recording check](MEDIA_PREFLIGHT.md)
looks for audio and likely speech. Videos without audio remain playable;
uncertain/no-speech results wait for an explicit decision and failed inspection
can be retried. Language is not assessed. The complete original recording is
submitted when transcription proceeds.

On fresh installations, media transcription and enrichment use operator-managed
HTTPS speech APIs (`CASE_INTELLIGENCE_ASR_*`, `ALIGNMENT_*`, `TRANSLATION_*`,
and `DIARIZATION_*`) behind the CONNECT relay, each gated by per-matter
owner/admin approval; see [model configuration](MODEL_CONFIGURATION.md),
[local speech APIs](LOCAL_SPEECH_APIS.md), and
[speech enrichment APIs](SPEECH_ENRICHMENT_APIS.md). Existing nodes that
retain the legacy bundled transcription queue (or migrate via
`--retire-bundled-models` while preserving private transcript history) report
inspection, submission, queue, transcription, alignment, diarization,
projection, summary, completion, degraded, and failed states. Exculpata indexes
timestamped segments and automatically generates a source summary after
projection when an answering model is granted for the matter. Users can export
transcripts, summaries, clips, citations, and reports.

If transcription stops after Exculpata has imported a transcript, choose
**Try again** on the failed source. Recovery rebuilds the source from its saved
passages and speaker labels, even when the transcription service is offline.
Corrections, citation timestamps, and the processor's recorded quality and
provenance stay with the original source version. If the transcript has yet to
be imported, recovery checks for an earlier processor job before resubmission.
On older retries that lost the processor job ID, Exculpata uses the job owner,
source digest, and byte count to locate temporary processor files. Source and
matter deletion wait until the processor can confirm cleanup.
Older releases recorded the degraded status only when the job finished. For a
job interrupted between import and completion on those releases, consult the
saved transcript warnings and quality details when reviewing the result.

Speaker diarization produces anonymous clusters, not identities. Reviewers can
open **Review speakers** beside the synchronized transcript or select a speaker
label on any passage. A deliberately confirmed correction applies to every
passage in that transcript with the same cluster and attempts to refresh search; merely
opening the review never confirms a label. The in-page save keeps playback,
scroll, and keyboard focus in place, while the non-JavaScript form carries the
current timestamp, transcript filters, page, and focused passage through its
redirect.

Transcript text and speaker corrections are saved before search updates. If an
update fails or configured search is unavailable, the response confirms the save
and the recording page keeps a **Search update pending** notice with **Retry
search update**. Retrying does not add another correction. If service recovery
requires a restart, startup reconciles the saved revisions automatically, even
when the original recording version is unchanged. Native source versions,
passage IDs, timestamps and citation validation remain unchanged; stale search
candidates are still rejected.

The per-source registry stores an optional digest receipt only after the matching
search transaction succeeds. Older registries without this receipt are accepted
and their transcripts are reconciled once when search is available. Backup the
workspace and matter registries together while writers are stopped, as described
in [storage and backup](STORAGE_AND_BACKUP.md); the receipt is derived state, not
an authority over saved human revisions.

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

Browser playback conversion requires Linux parent-death protection: its isolated
child exits with the owning application thread, including after abrupt application
death. Conversion fails closed when that protection is unavailable; the original
video remains available. Cancellation stops the conversion process group with
bounded waits. If stopping fails, playback reports `PlaybackChildStuck`, preserves
the temporary output and capacity reservation, and starts no further conversion
until restart.
