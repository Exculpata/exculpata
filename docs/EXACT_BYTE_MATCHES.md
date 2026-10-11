# Identical received bytes

Status: implemented and locally validated; the [review policy](QUALITY_GATES.md#review-policy)
applies before merge. This is source organization, not a
change to intake admission, source identity, evidence verification or retention.

## Staff workflow

Sources lists **N files with identical bytes** for received sources sharing the
same recorded byte digest and size. The count includes that source. Open the
link to compare those sources across the matter's collections and folders. This
deliberate comparison starts with all matching occurrences; subsequent folder,
collection, source-group, type, search and review filters narrow it normally.
Choose **Has copies** under **Identical bytes** to see sources that have another received
match anywhere in the matter. Counts describe the whole matter, including matches
outside the current filtered view.

Use the existing selection and group actions to retain a review set. Those
actions now return to the current comparison and listed Sources filters after
both success and recoverable errors. Source opening, review, saved support and
export retain each occurrence's exact source/version reference. Removing one
occurrence preserves the others and updates their counts. No files are merged,
renamed or automatically removed.

A removed, changed, unknown or foreign reference opens an empty view with
**This comparison is unavailable** and a **Leave comparison** link. It never
silently shows all sources. Malformed tokens are rejected. A reference with no
remaining matching peers can still show itself. Matching bytes alone establish
neither common origin nor authenticity; source contexts remain distinct.

## Recorded bytes and bounded queries

Only the existing source registry's server-captured digest and size can populate
the lookup. Selected-file claims, unreceived uploads, signature-rejected bytes
and malware-quarantined bytes cannot establish matches. Registered sources need
a captured digest first. Known changed, missing or unknown source states are
excluded. Playback-only and failed processing can still have received bytes;
byte equality does not claim searchable text or completed transcription.

Comparison concerns the recorded received versions, not a continuous rescan of
external originals. Opening sources retains the normal exact-version/byte
checks. Browsing neither rehashes files nor loads the complete source inventory.
No digest, internal path or new opaque identifier appears in staff output.

Additive SQLite migration `0026_source_byte_matches.sql` creates one derived
lookup keyed by matter/source and carrying the current version, digest and size.
An index on matter/digest/size supports matching. A view joins it to the current
catalog version, size and admitted state. Upsert/replacement synchronizes catalog
and lookup in one transaction; catalog deletion cascades to the lookup. Ordinary
source-store opening rebuilds it from the authoritative registry. There is no
second source inventory, registry JSON field or new catalog column.

Database queries determine matching groups and folder counts. The source page
materializes no more than 100 rows. Current valid byte rows are materialized once
for the matter and counted once per digest/size group, then the counts are joined
to the display page. Indexed source identity plus a materialized current-ID set
avoid rescanning the whole byte-row intermediate for each displayed source.
The 10,000-source synthetic regression exercises one large identical-byte group,
late ordinary/matching/comparison pages, scoped folders, folder pages and rebuild
after an older catalog writer. Each late-page path must stay below five million
SQLite VM steps. The source registry and source bytes remain outside browse reads.

## Recovery and compatibility

Back up and verify restoration of the whole application-owned boundary before
upgrade. The normal synthetic backup/restore test uses real control/registry
SQLite databases and received files with the existing mocked service/restic runner.
It verifies current comparisons after restore, removes only the disposable
derived lookup, and proves normal source opening rebuilds it from the restored
source identities, versions, digests, sizes and exact bytes. This is not a new
encrypted-repository or PostgreSQL recovery drill.

The stopped previous reader can open Sources and export selection while retaining
both identical-byte source occurrences. The catalog and registry row shapes are
unchanged. Returning to the current reader rebuilds the lookup through normal
reconciliation. A stale lookup cannot match a catalog version/size/state change;
never run concurrent old and new application writers against the same boundary.
Older readers lack the comparison UI. Prefer forward correction after use;
ordinary source/occurrence rollback limits still apply. Do not restore only the
control database or overwrite newer staff work.

## Synthetic acceptance

```console
python -m pytest -q tests/test_exact_byte_matches.py tests/test_backup_consistency.py
python scripts/browser-accept-upload-occurrences.py --verify-byte-matches --chrome-binary /path/to/chrome --chromedriver /path/to/chromedriver --output /tmp/generated-byte-match-acceptance
python scripts/verify-byte-match-rollback.py --previous-source /path/to/previous-checkout
make check
```

The focused contracts cover actual repeated uploads with equal/different bytes,
source tokens and passages, hidden digests, removed/foreign/malformed references,
changed states/versions, unreceived/quarantined intake, folder/filter continuity,
group-action recovery and 10,000-source bounded paging. Browser acceptance also
covers desktop/narrow layouts, reload, saved source groups, exact source support,
normal export and owner closure with unchanged external fixtures.

Validation on September 8, 2026: `make check` passes 954 application tests with
nine optional skips, all 194 transcription tests, compilation, both Compose
graphs and publication inspection. Nine Chrome workflows pass. The stopped
previous Sources/export reader and forward-opening check pass. Normal synthetic
backup/restore and lookup reconstruction pass in the application suite.

The initial large-group query counted rows before pagination. Materializing the
display page alone still recounted the same large group for each displayed row;
hosted security review reproduced delayed unrelated workspace reads. The final
query aggregates groups once and uses indexed identity joins. A matched generated
10,000-source HTTP workload fell from 3.43 seconds to 0.38 seconds; an unrelated
read waiting on the shared workspace lock fell from 3.20 seconds to 0.16 seconds.
SQLite work fell from about 39.8 million to 4.2 million steps for the full HTTP
request. These are local synthetic observations, not deployment latency promises.
The tighter five-million-step contract covers all three late-page paths.
Browser acceptance exposed the lost comparison after grouping and
now verifies the fixed return path. Narrow-layout screenshots wait for the
existing navigation transition to finish; the existing Sources table still uses
horizontal scrolling. No new confidential-data or live-deployment claim is made.
