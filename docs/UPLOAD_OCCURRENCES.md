# Deliberate upload occurrences

Current contract for repeated paths in confirmed resumable intake. This workspace
remains temporary; received copies and their derived state are deleted on close.

## Staff outcome

Confirming another file or folder selection creates a separate received source
for each new upload item, even if an earlier collection contains the same relative
path or identical bytes. Both collections retain their own source, receipt link
and exact source version. Retrying the same interrupted upload retains its source
identity and saved bytes instead of adding another source.

Previously, selecting `Records/report.txt` in two separate collections reused one
source for equal bytes, leaving a collection empty. Different bytes at the same
path were rejected. The generated regression covers both cases.

Each occurrence keeps its selected relative path. Staff can browse its collection,
add it to a source group, open its exact source support and save supported work
product. Removing one source through the existing supported removal route leaves
the other occurrence intact. A retained receipt marks a removed source unavailable.
Removal no longer leaves an already finished upload looking permanently active:
remaining sources stay searchable when there is no queued or running work.

This does not expand source-removal permissions. The existing small direct-upload
compatibility route retains its legacy retry/name-conflict behavior; this change
covers the browser's confirmed resumable intake. Equal bytes are not evidence of
shared origin or authenticity. No automatic source deletion or duplicate merging
is introduced.

## Identity and recovery

The existing durable upload item identifies the occurrence. Its existing source
record stores `resumable:<upload_item_id>` in the internal `name_key` field.
Neither the staff filename nor the validated relative path changes. No new schema,
source-registry field, storage boundary, worker, dependency or model is added.
Source versions, source sets and collection membership retain their current
contracts.

Finalization finds the same item key before creating a source and checks its path,
size, media type and byte digest. A source persisted before the control-database
commit is recovered after restart with the same identity and version. The normal
hard-link path and bounded copy fallback both preserve this key. The fallback
verifies regular, non-symlink files, exact size and digest, stable source metadata
and a flushed destination; it does not use path-based deduplication.

A legacy interrupted hard-link finalization can be adopted only when its canonical
path, digest and recorded inode match and its stored source file still shares the
incoming file's inode. Byte equality alone cannot adopt an unrelated source.
A pre-change interrupted copy without either an occurrence key or a shared inode
cannot be identified unambiguously by hash alone. Operators should resolve such
legacy interrupted work before upgrade or preserve its whole backup for recovery.

Upload admission, file checks, authorization, processing and exact source/version
resolution continue through the existing routes. A missing catalog source with a
retained completed upload reports attention when no queued/running ingestion or
media job remains; actual active work still blocks search and closure.

## Status during concurrent uploads

Full status and resume reconciliation inspect staging under the same matter's
source lock used for chunk admission. Filesystem I/O does not hold the global
workspace lock, so slow staging storage does not block database work in unrelated
matters. Only an item needing recovery takes the workspace lock to refresh its
current authorized ledger state and apply the update atomically. A finalization
or cancellation committed during inspection is preserved, including when the
inspection reports missing or unsafe staging. Normal scans read the item list
once rather than querying the whole session for each file.

A competing offset/state commit is reread through the existing matter and actor
authorization checks; unchanged-ledger errors still propagate. Compare-and-set
chunk admission remains unchanged.

Bytes saved before an interrupted ledger commit still advance the recorded
offset. Missing bytes or unsafe staging files still fail an active item and
require a new upload collection. Status recovery does not create a source,
change receipt bindings, or derive retry identity from a path or digest.

The deterministic HTTP regressions in `tests/test_upload_status_race.py` cover
competing chunk, finalization and cancellation requests, stale compare-and-set
recovery, unchanged-ledger failures, missing/oversized staging, uncommitted
bytes, exact source/receipt support and foreign-matter denial. Threaded tests
pause filesystem inspection and require unrelated matter work and terminal
ledger commits to finish before inspection resumes. Run them with:

```console
python -m pytest -q tests/test_upload_status_race.py
```

## Backup and rollback

Before upgrading, capture and verify the complete application-owned backup.
The normal synthetic backup/restore regression uses real SQLite and source files
with a mocked service/restic runner. It restores two finalized sources interrupted
before their control commits, then retries to the same two source identities,
versions, keys and bytes. It does not claim a new encrypted-repository or
PostgreSQL recovery drill.

A stopped previous source-registry reader can read both occurrences, bytes and
identities because the JSON record shape is unchanged. Returning to the current
reader retries the original upload items without adding sources. This proves
read compatibility, not safe operation of an older writer: older finalizers still
use path-based deduplication. Prefer a forward correction after occurrence intake
is used. Rollback requires the complete verified pre-upgrade backup and deliberate
handling of newer work; do not restore only one database or registry.

## Synthetic acceptance

The focused regressions cover equal/different bytes at equal paths, distinct
collection and receipt bindings, same-item retries, interrupted control commits,
restart, hard-link and copy fallback, interrupted copy/restart recovery,
legacy hard-link adoption, missing sources,
and readiness with each relevant job state.

The Chrome acceptance confirms three actual folder selections at the same path,
including two with equal bytes and one with different bytes. It opens each exact
source from its receipt, groups an occurrence, searches and saves supported work,
removes the others while preserving the retained source/group/note/search,
checks a foreign-matter canary, downloads all receipts in the final bundle and
closes normally with unchanged external originals. The removal step uses the
existing loopback development route; it does not claim a new shared staff removal
permission.

```console
python -m pytest -q tests/test_upload_occurrences.py tests/test_backup_consistency.py
python scripts/browser-accept-upload-occurrences.py --chrome-binary /path/to/chrome --chromedriver /path/to/chromedriver --output /tmp/generated-occurrence-acceptance
python scripts/verify-upload-occurrence-rollback.py --previous-source /path/to/previous-checkout
```

Validation on September 8, 2026: `make check` passed 942 application tests
with nine optional skips, all 194 transcription tests, compilation, both Compose
graphs and publication inspection. Six real Chrome workflows and the stopped
previous-reader/forward-retry check passed on the combined prerequisite revision.
The normal synthetic backup/restore coverage is included in the application suite.
Before merge, follow the [review policy](QUALITY_GATES.md#review-policy).

The browser navigation harness treats the specific ChromeDriver detached-node
inspector error as stale, matching the folder-navigation harness; other browser
errors still fail acceptance. The first combined rerun hit that driver error
after the close action; the completed rerun verified closure and unchanged originals.
