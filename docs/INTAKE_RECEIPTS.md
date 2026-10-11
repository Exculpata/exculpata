# Selected-file receipts

Current contract for the browser's confirmed file and folder selection. This
workspace remains temporary; receipts are deleted with the matter.

## Staff workflow

The Add sources status distinguishes disabled scanning, an enabled ready scanner,
and scanning that needs setup. Disabled scanning explicitly says that files are
not scanned for malware, even if a scanner is available. A ready scanner only
gets the active-checks message when scanning is enabled. When setup is needed,
files requiring a scan remain blocked. This status describes the current policy;
it does not certify that a selected file has already been scanned.

Choose files or a folder in **Sources**, review the complete selection, and
confirm the ready files. Confirmation first saves a receipt of every selected
item, including unsupported, empty, repeated-path, over-limit and other skipped
items. If nothing can upload, **Save selection receipt** records the selection
without transferring bytes. The preview alone still creates no durable inventory,
upload session or capacity reservation.

**Selected-file receipts** in Sources lists the team's selections; **Older
receipts** reaches earlier pages. Open a receipt to see selected, included,
skipped and unrecorded counts, bytes received, and current availability. Receiving
bytes does not mean parsing, indexing or transcription succeeded. Playable media
without audio remains **Playback only**; held recordings can **Need review**.
A malformed received file remains received and separately shows a failure.

**Open source** opens the exact received source version using the normal viewer,
including its existing integrity and matter checks. A removed or changed version
is unavailable rather than silently pointing at a replacement. Selection reasons
are explicitly labelled as browser-reported observations from confirmation. They
are not proof of historical server capacity or an attested preflight decision.
The reported state and reason remain separate from the server's filename check,
so a capacity-excluded file retains the reported capacity reason even when its
filename and size are valid.
The server uses bounded explanations for selection-level exclusions; it does not
store arbitrary browser explanation text as verified output. Empty folders are absent because
the browser supplies files, not a directory inventory.

**Download receipt**, **Text version**, and **Structured version** provide CSV,
Markdown and JSON. The complete matter work-product bundle includes all three
formats for every receipt, including selections consisting entirely of skipped
files. Downloads omit original source bytes and internal identities/locations.
They are work product, not an archive that can reopen the matter.

## Retry and incomplete selections

After interruption, reselect the same files to resume the existing selection and
upload. A lost response can be retried without duplicate receipts, sessions or
byte transfer. Same-page retry also works when browser storage is denied. Without
browser storage, automatic identification of the selection cannot survive closing
or reloading the page; its durable receipt remains discoverable in Sources.

Collection names are checked before saving a browser checkpoint. A rejected name
can be corrected with the same files still selected, including when browser
storage is unavailable. Older invalid name checkpoints are ignored. Unsupported Unicode characters receive
a normal validation response. Choosing a new selection hides the earlier receipt
link until the new selection has its own saved receipt.

Explicitly cancelling an upload retains its receipt and clears the browser
checkpoint, so a later deliberate selection can start a new attempt.

Metadata is saved in bounded idempotent batches. Until every row is recorded, the
receipt states exactly how many selected files are still unrecorded; uploading
cannot begin. Incomplete exports retain those counts, including a CSV summary
when no file rows were recorded. File names for unrecorded rows cannot be recovered
without reselecting the files.

Older upload checkpoints without a receipt can be adopted only when their exact
manifest and earlier collection batches match the included selection. Previously
received bytes and offsets are retained. A collection already bound to another
receipt cannot be reassigned. Cancelled/missing older checkpoints keep the existing
fresh-retry recovery response. Changed or unauthorized bindings fail without
partial writes or deleting earlier bytes. An explicit session or collection
checkpoint that conflicts with the receipt binding remains a resume mismatch;
receipt idempotency does not silently override it.

The upload route still owns capacity admission, signatures, malware checks and
source processing. A receipt does not authorize a skipped file for upload.
Source-byte storage is reserved when an upload session is created. Receipt
metadata has the separate admission limits below. Metadata retries retain
original row identities and descriptor digests; the uploaded source version is
captured when finalization commits. During adoption, an existing immutable uploaded
source can supply its version from the matter catalog; absent support remains
unavailable.

## API and storage contract

All routes require current matter access. Metadata mutations require CSRF and
the principal who created the receipt; team members can read it. Only the matter
owner may deliberately cancel an empty upload attempt and discard its receipt. Existing audited
administrator review also opens status, pages and downloads without allowing
receipt mutations or granting an ordinary nonmember access. Export uses the
normal matter response lease and authorized final-export boundary.

| Route | Purpose |
| --- | --- |
| `POST /matters/{slug}/intake-receipts` | Create or return the actor's exact confirmed selection key. |
| `POST /matters/{slug}/intake-receipts/{receipt}/items` | Append or verify a batch of selection rows. |
| `POST /matters/{slug}/intake-receipts/{receipt}/seal` | Verify complete contiguous inventory and included-path uniqueness before transfer. |
| `GET /matters/{slug}/intake-receipts/{receipt}` | Return current selection, transfer and availability counts. |
| `GET /matters/{slug}/intake/{receipt}` | Show 100 selection rows per page with exact source links. |
| `POST /matters/{slug}/intake/{receipt}/discard` | Owner-confirmed discard before data is received; cancels bound empty upload attempts. |
| `GET /matters/{slug}/intake/{receipt}/export?format=csv` | Download CSV, Markdown or JSON. |
| `GET /matters/{slug}/setup?receipt_page=2` | Discover older selections, ten receipts per page. |

Selections contain at most 10,000 rows. Metadata batches contain at most 2,000
rows and 6 MiB of UTF-8 JSON; the browser splits batches by both count and encoded
size. Invalid types, counts, ordinals or nested descriptors fail before writes.
Paths use the same validated canonical relative path as upload admission; unsafe
names are minimized. Client metadata is not a byte-derived finding. The selection
is a browser-reported inventory; the server does not attest which files the user
selected or the historical selection-level exclusions. Projection includes
`reviewed_basis: browser_report`, and all rendered/exported review reasons carry
the same explicit origin. Receipt metadata never authorizes source transfer.

Receipt admission also has cumulative limits:

| Scope | Receipts | Reserved selected rows | Charged metadata |
| --- | ---: | ---: | ---: |
| One matter | 1,000 | 100,000 | 32 MiB |
| One creator across matters | 2,000 | 200,000 | 64 MiB |
| Whole workspace | 10,000 | 1,000,000 | 128 MiB |

Creation reserves the complete selected-row count before item batches arrive.
The stored metadata charge includes UTF-8 field sizes and a conservative fixed
allowance per receipt and row; it is not a physical database-file measurement.
Count/row/byte admission and insertion share a SQLite write transaction across
connections. Exact retries add no charge, and a rejected batch inserts no rows.
The earlier rows and unrecorded count remain available. Normal source-upload
quotas continue to govern source bytes.

At a receipt limit, the product explains how to recover. The matter owner can
open a receipt with no received data, download it if wanted, and confirm
**Discard receipt**. Both incomplete and completed all-skipped selections can be
discarded. If empty uploads were already started, the confirmation explicitly
cancels those attempts before removing the receipt. This also works for selections
saved by a member whose access was later revoked. Metadata reservations are
released and the existing content-free audit records the action.

Receipts with partial or complete received data remain with the matter. Cleanup
checks every item in the associated upload sessions, source/version bindings and
saved partial files, including bytes whose earlier offset commit failed. A shared
source mutation lock serializes cleanup with chunk writes and offset commits;
an upload request rechecks its state after its body arrives and it acquires that
lock. Cancellation and receipt deletion share an immediate SQLite transaction.
Unexpected sessions shared with another receipt fail closed. No received sources
or saved partial bytes are deleted by receipt cleanup.

Use another matter for further deliberate intake when receipts with received data
occupy its capacity, or contact the administrator for an account/workspace limit.
The normal owner close/export process remains available.

Mirrored SQLite migration `0025_intake_receipts.sql` adds receipt, item and transfer
tables to the existing control database. It creates no new storage boundary or
worker. Receipt writes count as matter activity. Incomplete metadata alone is not
active processing and cannot strand matter closure. Normal owner closure removes
all receipt rows in the existing deletion transaction; upload and processing work
retain their existing active-work checks.

Receipt page rows/counts and exports use SQLite read snapshots. Complete bundles
materialize at most 1,000 receipts and 100,000 selection rows, with the existing
100 MiB uncompressed export limit also enforced. Oversized bundles fail explicitly
and direct staff to individual receipts; older receipts are never silently omitted.
CSV cells receive the existing formula protection. The final bundle still checks
all other work-product sections and total output size.

## Upgrade, backup and rollback

Back up and verify restoration of the complete application-owned boundary before
an upgrade. The normal backup already includes the SQLite control database and
managed partial uploads. The synthetic normal backup/restore regression verifies
receipt rows, skipped reasons, metadata charges, transfer bindings and seven
saved partial bytes.
The test uses real SQLite databases and partial files with a mocked service/restic
runner; it does not claim a new encrypted-repository or PostgreSQL integration
drill. No source originals are added to the backup boundary by this feature.

A stopped synthetic previous-version reader can open the existing Sources page
and saved upload offsets without damaging new receipt tables. Returning to the
current reader resumes the exact bytes with the same receipt/session. This proves
read compatibility, not feature equivalence: an older application has no receipt
UI or receipt exports and must not be used for ordinary receipt-aware review or
closure. After receipts are in use, prefer a forward correction; a rollback needs
the complete verified pre-upgrade backup and an explicit disposition for newer
work. Do not drop the new tables or restore only the control database.

## Synthetic acceptance

The regression suite covers 10,000-item recording, malformed requests, CSRF,
revoked/foreign actors, concurrent retries, exact manifests, canonical paths,
source-version changes, held media, coherent snapshots, CSV protection, pagination,
complete bundles, incomplete selection, activity and deletion. The dedicated
Chrome acceptance exercises actual nested folder input, denied browser storage,
lost successful responses, interrupted/reloaded byte transfer, exact source
viewing, all-skipped selections and normal receipt/final-bundle downloads.

```console
python -m pytest -q tests/test_intake_receipts.py tests/test_intake_receipt_http.py tests/test_backup_consistency.py
python scripts/browser-accept-intake-receipts.py --chrome-binary /path/to/chrome --chromedriver /path/to/chromedriver --output /tmp/generated-receipt-acceptance
python scripts/verify-intake-receipt-rollback.py --previous-source /path/to/previous-checkout
```

Browser artifacts contain only generated fixtures. They are additional acceptance
evidence, not required public attachments or proof of confidential-data readiness.

Validation on September 8, 2026: `make check` passed 920 application tests with
nine optional skips, all 194 transcription tests, compilation, both Compose
graphs and publication checks. Eleven dedicated Chrome workflows passed, including
actual capacity exclusions, name correction with denied browser storage and
owner recovery from sealed skips and bound empty uploads at the real 1,000-receipt limit,
and replacement of the previous receipt
link after a new selection. All 37 existing loose-file preflight workflows passed
on this correction. Normal synthetic backup/restore preserves the receipt metadata charge;
the previous-reader check preserves receipt state and exact seven-byte-offset
forward resume.

During combined validation, a media regression inspected the last generator call
while a background overview could run later. Its assertion now selects the
requested written-only question and checks every matching call's evidence kind;
answer/citation coverage is unchanged. The frozen acceptance pack records the
changed test and file digests without changing its cases or fixtures. The full
gate was rerun after this correction.

## Deliberate browser selection

The upload form starts with an empty required collection name. Enter a name for
this group before confirming its selected-file receipt. File and folder pickers
are equally prominent buttons. The no-JavaScript direct-upload form also retains
the submitted name; older API callers that omit it retain their compatibility
default.

**Cancel selection** clears pending files and folder inputs, aborts selection
review, and ignores any late review response. It creates no receipt or upload
session. The same files can be selected again. Previously saved receipts and
upload recovery checkpoints remain available. During confirmation and transfer,
file/folder inputs, picker buttons, collection naming, retry, and selection
cancellation are disabled until the browser finishes handling the response; **Cancel upload** retains its existing
semantics for a transfer already underway. Unrelated ingestion refresh waits
while the user has an unconfirmed selection or a nonblank edited collection name,
including a name entered before picking files. Once a receipt saves the name, it
no longer counts as an unsaved draft. Clearing the draft allows refresh again.

Synthetic browser acceptance covers blank/whitespace names, both pickers,
cancellation during review and after folder review, same-file reselection, and
late response rejection alongside existing lost-response and receipt recovery.
HTTP regression covers named direct uploads and whitespace rejection before
source storage. No schema, model, or deployment change is required.

The browser regression holds a terminal upload response after the server finishes
processing. Controls must remain disabled during that gap and become available
when the response is released; the next selection then creates its own receipt.
Server-side job completion alone is not a browser-ready signal.

All browser upload journeys explicitly name their synthetic collections. The
extended acceptance checks cover assertions, entities, entity discovery, matter
renaming, notebook/Report/review-decision conflicts, folder navigation, media
preflight, email coverage, and loose-file preflight in addition to the regular
intake and Report-bundle CI journeys. The refresh regression verifies both
name-before-selection preservation and renewed refresh after clearing the draft.
