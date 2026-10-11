# PDF OCR selection and extraction coverage

PDF search covers the text that extraction retained. A searchable page does not
establish that all visible page content was read. This applies to older PDFs too;
opening a source or restarting the application never replaces its saved text.

## New ingestion

Native extraction remains in the bounded child process (500 pages, 250,000
characters per page, 5 million total characters, 10 CPU seconds and 15 seconds
wall time). The child requests a 512 MiB address-space ceiling. Where the
platform accepts that paired hard and soft limit, it is installed. Where the
kernel rejects a limit below the process's current virtual size, including
macOS, the child installs the lowest limit that kernel accepts plus the same
512 MiB as a growth budget, and lowers the hard ceiling to match. A tighter
inherited soft limit is not raised. If no finite limit is accepted, extraction
continues. A refused ceiling is not a damaged or malformed PDF. A helper
startup or other pre-parse failure is reported separately and is not described
as document damage. CPU, file-size, descriptor and wall-time limits still
apply. That process also inspects image placements without
decoding image pixels. It sums image bounding boxes clipped to the page
CropBox, including inline images and nested Form objects. Overlap, skew and
nonrectangular clipping can cause overestimation. The traversal stops at 50,000 operations, 256 image
placements, 64 Forms, depth 8 or graphics stack depth 64; unsupported or exhausted
inspection records unknown evidence.
Named pattern colors, Type3 fonts and soft masks also record unknown evidence
because their paint procedures can contain images outside the inspected
page/Form paths. Annotation appearances and non-link annotations likewise
record unknown evidence. Plain links without appearances retain known evidence;
annotation-reference inspection stops at 256 entries without decoding appearances.

Expanded OCR selects a page when it has fewer than 20 native alphanumeric
characters, images cover at least 10 percent of the page, or image evidence is
unknown. A long stamp therefore cannot suppress recognition of a large scanned
body. Small logos with useful native text do not ordinarily trigger OCR. These
are selection heuristics, not proofs that native text or recognized text is
complete or accurate. A substantial image with an existing text layer can still
be selected; native text always survives.

Selective mode retains its empty-text-page policy and 25-page cap. Expanded mode
uses the configured `CASE_INTELLIGENCE_MAX_OCR_PAGES`, clamped to 1–500. Disabled
OCR, selective-policy skips and page-cap skips are reported separately from
attempts. Rendering remains limited to a 2,600-pixel longest side and 16 MiB;
rendering and each recognition subprocess have a 20-second timeout. Automatic
orientation/layout recognition is tried first, with a block-layout fallback only when it
returns no alphanumeric text. Longer OCR is never used as a quality score.

Tesseract applies its own orientation estimate only when that estimate is strong.
Sparse text, or an upright stamp over a turned scan body, can leave it reading the
body upside down or sideways. That misreading still contains letters, so nonempty
output is not treated as evidence of accuracy. When at least a quarter of the
recognized words that native text does not already supply have Tesseract word
confidence below 50, the rendered page is retried at 180, 90 and 270 degrees.
A populated confidence table containing only native words also triggers retries:
an upright stamp alone does not establish that the scanned body was read.
Those retries share one additional 20-second timeout, measured again after each
pixel rotation so no retry starts once it has expired. All remaining orientations
are evaluated within that deadline: a confident fragment does not establish a
complete reading. A retry replaces the current reading only when it
has more words at confidence 80 or higher that native text lacks; words already
in native text, such as an upright stamp, do not count. Matching uses whole
alphanumeric terms and consumes each native occurrence at most once. A joined
term in either layer can match complete consecutive terms in the other layer,
bounded at 12 words and 256 characters. Partial substrings,
nonconsecutive terms and overlapping reuse of native occurrences are not discounted.
Competing readings are never combined. A retry that times out, fails or exceeds
a limit keeps the best completed reading and its outcome. Before recognition,
a dedicated child applies a 16 MiB regular-file size limit, retaining any tighter
inherited limit, then executes Tesseract. The parent uses the child's reported
effective limit and rejects either output file at that boundary, including a
child that reports success after a truncated write. File-limit overflow returns
a write error without a core-producing signal; the existing recognition timeout
still bounds the child. This bounds output-file growth while Tesseract writes,
including its word-confidence table; the existing text read limit still applies.
Without usable word confidences the first reading is kept without retries. PDF page
rotation metadata and pixels turned on an upright page are handled the same way,
because retries turn the rendered page.

Useful native text is retained verbatim as extracted. Distinct OCR lines are
appended on the same original page; whitespace/case-equivalent lines already
present are omitted. This does not reconcile near duplicates or resolve OCR
errors. Combined text shares the native page and document character budgets.
An OCR addition exceeding either budget is omitted with a limit notice, while
native text remains searchable. Page locators and original file bytes/digests
are unchanged.

## Coverage and recovery

Each newly extracted PDF's existing source status records searchable-page and
native-text counts, OCR attempts and returned text, disabled/selective/capped
skips, timeouts, failures, empty results and output limits. A returned-text count
means the tool produced text, not that the page was fully or correctly read.
Blank pages can produce empty OCR; that outcome alone cannot distinguish an
intentional blank from an unreadable scan.

Sources and the source viewer retain this status. Catalog-based PDF coverage
notices conservatively cover every catalog PDF, including historical sources,
and reach readiness, exact searches including zero results, questions,
investigations and their exports. Full-text review analyzes retained text.
When PDFs participated, original-content completeness remains unknown even when
every extracted range was processed. Answer coverage is partial even with zero
excluded files;
full-text analysis-gap counters still describe retained-text work, with an
additional original-PDF completeness caution. Review the original PDF before
relying on missing terms.

There is no new schema, source registry field or automatic backfill. Historical
PDFs receive a conservative coverage notice without fabricated OCR outcomes or
changed extraction/citation identity. Ready sources cannot be retried in place.
Historical answer and investigation views, and answer, investigation and Report
exports, receive a separate conditional PDF caveat. It survives removal of the
last PDF because the current catalog cannot establish historical absence of PDFs.
Their original
coverage receipts and citations remain unchanged, including a historical saved
complete status.
To obtain new extraction for an already-ready PDF, explicitly ingest it as a new
source (for example under a distinct name); existing references keep their old
source basis. Existing failed/no-text recovery still uses the explicit retry
operation, and source-byte verification remains mandatory.

## Regression evidence

The generated synthetic image-PDF suite exercises real ingestion, CPU OCR and
exact phrase search for long/short stamps, image-only pages, pattern-painted
images, annotation appearances, soft masks, native text, small logos, mixed
regions/pages and existing text layers.
Rotated scan bodies must reach exact search on the correct page and source
version at 0, 90, 180 and 270 degrees. The suite covers both PDF page-rotation
metadata and pixels turned on an upright page, sparse one-line bodies, and an
upright native stamp over a turned body. Before the orientation retry, the
existing 180-degree stamped fixture and four sparse or stamped pixel-rotation
cases returned misoriented text that exact search could not find. Blank-page
exclusions, native preservation and coverage cautions are also
checked, along with original bytes, digest, source version, page locators and
restart behavior. Outcome tests
separately inject timeouts, failures and resource caps to verify their reporting;
those tests are not recognition-quality evidence.

The real OCR suite requires the production Linux executable paths. Mac probes
with substituted local tool paths or relaxed unsupported resource limits are
portability diagnostics only. Final-head hosted Linux CI and separate supported
installation acceptance remain distinct. No synthetic OCR result establishes
model quality, exhaustive document reading, or hardware readiness.

In the local Mac real-tool probe, the upside-down synthetic body was selected,
but automatic orientation declined to rotate it and returned incorrect text.
Its native stamp survived and the coverage caution remained visible. The later
orientation retry recovers these synthetic Linux fixtures. It does not establish
recovery of arbitrary rotated scans, skewed pages, mixed-orientation bodies or
non-English text. When the selected orientation turns an upright stamp,
misread stamp lines can also be appended. The stamp itself remains
searchable from native text. Inspect the original when recognition is poor,
even when OCR returned nonempty text.
