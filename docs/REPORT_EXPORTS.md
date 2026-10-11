# Saved Reports and complete matter exports

Current contract, September 5, 2026. This is the source of truth for saved
Report inclusion in downloaded matter bundles. Shared edit/conflict behavior
is defined separately in [Report editing](REPORT_EDITING.md).

Every saved draft and final Report, including an empty Report, appears under
`reports/` in the complete matter work-product ZIP. Markdown and Word files
preserve the title, purpose, status, ordered sections, manual edits, and source
appendix. `manifest.json` adds `report_count` and `reports`; each inventory row
contains title, status, section count, update time, and Markdown/Word member
paths. The existing bundle schema remains v1 with additive inventory fields.
The generated `matter-report` cover document is separate from saved Reports.

Indexed safe filenames prevent collisions between identical or differently
capitalized titles. Exports contain work product and source references, without
original document, recording, or clip bytes. They can be opened in ordinary
editors; this does not promise Exculpata package import or archival storage.

## Completeness and recovery

The bundle reads Report headers, sections, and citations in one SQLite snapshot.
Concurrent edits appear either before or after that snapshot, never as mixed
header/body revisions. Reports are validated using the same source-version,
passage, location, and clip checks as individual Report exports. Each saved
Report is resolved once inside the source mutation boundary, then both formats
are rendered there; Word does not repeat the potentially corpus-wide lookup
already performed for Markdown. The response
lease prevents matter closure while an export is being prepared or delivered.

No complete bundle is returned if any saved Report cannot be included safely.
Limits are 500 Reports, 10,000 total Report/header/section/citation rows, 32 MiB
of saved Report text before rendering, and the existing aggregate bundle byte
limit including both formats. Nothing is silently truncated. Download Reports
individually before closing a matter when the combined export exceeds a limit.
For stale source support, open the Report and resolve the unavailable citation
before retrying. No successful export receipt is recorded on failure.

Existing owner and administrator final-export authorization remains in force.
After an interrupted close has quarantined processing copies (`purge_failed`),
manual-only Reports can be exported from the saved snapshot. Report citations
do not retain the complete verification basis needed to revalidate quarantined
sources. A bundle containing cited Reports therefore fails explicitly and asks
an administrator to restore source access before retrying. It never traverses
quarantine, recreates an active source directory, or silently drops Reports.
This limitation does not change the existing frozen investigation-ledger
recovery contract. The separate [full-text synthesis adapter](FULL_TEXT_SYNTHESIS.md)
requires current original-source validation and refuses quarantined-source
exports; it does not inherit that older investigation recovery shortcut.
Retain a verified download before deliberately closing.

## Validation and rollback

`tests/test_reports_bundle.py` covers preservation, collisions, empty Reports,
source tampering, bounded failures, a second-connection concurrent edit, and
interrupted-close handling, restart, matter isolation, and close/export leases.
Its media regression checks exact saved clip and transcript timestamps in the
bundle and rejects a changed source label. Existing
export and lifecycle suites remain applicable.

`scripts/browser-accept-reports-bundle.py` exercises synthetic browser upload,
exact support, case-note capture, Report editing/reordering, individual and
complete downloads, failure display, mobile download, and deliberate closure
with retained downloads and an unchanged external original. Supply local
Chrome and ChromeDriver paths plus an output directory through its arguments.

There is no persistent schema migration. Reverting code is data-compatible but
reintroduces the omission defect: use verified individual Report downloads
before closure until the correction is restored. Production activation is a
separate deployment decision.

Local validation on September 5: 20 focused Report regressions pass; the full
application suite passes with 706 tests and 9 environment-gated skips. The
synthetic browser acceptance passes all seven workflow checks. Compilation,
publication tree/history checks, and both Compose graphs pass. The
[review policy](QUALITY_GATES.md#review-policy) applies before merge.

## Reviewer context selection manifests

Continuity B adds `context/selections.json` to complete matter bundles and their
existing additional-work-product inventory. It includes all owners' selection
headers, revisions, ordered typed references, approval fingerprints and
attribution. Knowledge remains shared; owner-specific selections are not private
notes. The manifest describes explicit opt-in consumption; per-answer receipts record actual dispatch.
It does not claim current support availability or retain authoritative fact
copies. Shared knowledge is exported through its existing separate artifacts.
A manifest exceeding 1,000 selection owners or 8 MiB fails the complete export;
it is never silently omitted. Note-only export behavior is unchanged.

## Context supplied with focused answers

Continuity C answer/conversation Word and Markdown exports and complete-bundle
answer JSON include the full submitted context and runtime dispatch receipt.
Complete bundles additionally include `context/answer-job-*.json` for unfinished
or failed work. Exact originals and available runtime/model labels remain
separate from reviewer orientation. A receipt records supplied input, not proof
of model attention or immutable per-response model attestation. Prepared,
tokenization, generation, repair, transport failure and completion stay distinct.
Later knowledge changes do not rewrite the historical input. The inspector
separately labels current freshness and source availability.

Selection manifests now describe explicit focused-answer consumption; actual
consumption is per answer. Note-only exports remain note-only. Direct answer
copy and conversation-to-Report compilation refuse C answers because those
paths cannot preserve their receipt. Context labels never become citations.
Existing export authority, source verification and complete-bundle limits apply;
receipt limits fail clearly rather than dropping an essential section.
