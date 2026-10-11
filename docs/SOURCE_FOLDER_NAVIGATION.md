# Source folders and existing organization

Sources now provides read-only folder navigation over the current matter's
cataloged sources. Open an immediate child folder, go up one level, or return to
all folders. The matching source list includes the selected folder's descendants.
Folder counts reflect the current collection, source-set, filename/type and
review/status filters; the matter-wide status totals remain the whole matter.

## Exact folder boundaries

Selecting `Production/North` includes `Production/North/report.txt` and
`Production/North/Interviews/detail.txt`. It excludes `Production/Northwest`,
`Other/North` and a filename containing `North`. Case and punctuation are literal;
percent and underscore are not wildcard characters. Input is normalized to NFC
and validated using the existing relative-path contract. Invalid paths receive
an ordinary recovery message. Unknown or removed folders show an empty list and
navigation to the parent or all folders.

Folders describe cataloged sources. Empty directories and skipped selection rows
do not appear as processed sources; [selected-file receipts](INTAKE_RECEIPTS.md)
remain the complete accounting of what the browser selected.

## Filters and source groups

Folder links and the Apply form preserve the active source filters. Child lists
are paginated at 50 folders; source lists retain their existing maximum of 100
rows. Out-of-range pages clamp to the last available page. Counts and results use
bounded database projections instead of materializing all sources in Python.

Upload collections remain import batches. Existing source sets already support
multiple groups per source and bulk creation/addition. Browsing a folder does not
change membership or set the sources assigned to a question or review task.
Use the existing source-set actions to save a focused review population.

Source links retain these filters in [continuous source inspection](CONTINUOUS_SOURCE_BROWSING.md), with a bounded list beside the viewer.

Source links still open the exact existing source. Same basenames in different
folders and separate upload occurrences retain their identities. Navigation does
not rename, relocate or write external originals. Matter authorization, audited
administrator reads, source support, export and owner closure retain their
existing boundaries.

## Operations and recovery

No new schema migration, dependency, model call, storage boundary or background
worker is introduced. Reverting the folder-navigation change removes these views
without changing source organization or source support. Normal backup and restore
continue to cover the existing temporary application-owned state. Receipts have
their own [upgrade and recovery contract](INTAKE_RECEIPTS.md).

## Synthetic acceptance

The initial regression reproduced the missing exact subtree filter. Contract
tests cover nested/sibling paths, literal punctuation and Unicode, bounded
pagination at 10,000 cataloged sources, combined collection/source-set/status/
review filters, unknown/removed folders, malformed requests and foreign-matter
canaries. Browser acceptance uses generated nested uploads and tests navigation,
reload, browser back, existing bulk source sets, exact support, final export,
normal owner closure and unchanged external originals. The browser harness
recognizes ChromeDriver's equivalent detached-node response during navigation.

```console
python -m pytest -q tests/test_source_folder_navigation.py tests/test_source_library.py
python scripts/browser-accept-source-folders.py --chrome-binary /path/to/chrome --chromedriver /path/to/chromedriver --output /tmp/generated-folder-acceptance
```

Validation on September 8, 2026: `make check` passed 926 application tests
with nine optional skips, all 194 transcription tests, compilation, both Compose
graphs and publication inspection. Seven real Chrome workflows passed, including
desktop/narrow layouts, exact source support, saved-note export and owner closure.
Before merge, follow the [review policy](QUALITY_GATES.md#review-policy).
