# Load-file quality checks

`source_load_file_qc.qc_volume(volume_root, dat_path=..., opt_path=..., profile=...)`
returns unchanged DAT documents and OPT pages, source-cited ranges, joined pages,
families, and findings. Supply an LF1a `FieldProfile`; no intake or storage occurs.
Load files must resolve inside the explicit root. Referenced paths use either
separator and must stay inside that root. Orphan scanning does not follow directory symlinks.
Bates labels and dates are never normalized. QC emits no gap facts; callers in an
application layer can pass the ranges and page counts to the existing gap engine.
Tests assert a 50-document, 180-page volume and 5,000/20,000-row scaling: both runs
under 15 seconds, larger time < 8 * smaller time + 0.5 seconds, and memory ratio < 6.
Findings retain at most 1,000 entries per code plus one summary of the dropped count.
Dates accept YYYY-MM-DD, YYYYMMDD, and month/day/year, optionally followed by
a space or T and HH:MM or HH:MM:SS. Blank or whitespace-only cells are missing dates.
Invalid dates are cited without changing the value.

## Streaming production import

The discovery importer consumes the public DAT and OPT parsers directly; it
does not call the volume-walking QC routine. Embedded paths remain inert strings
and do not authorize opening, locating or importing another file.

The optional synchronous `on_event` callback receives a frozen `RecordEvent`
for the header, each accepted or rejected logical record, and file-level
failures. Events retain ordered cells, starting physical lines and uncapped
logical ordinals before the legacy diagnostic examples are capped. An
unrecoverable value stays absent. Callback failure aborts the attempt. Existing
iterator shapes, `headers_out`, strict calls and bounded `findings` behavior
remain supported. Internal validated cursors resume at completed record
boundaries; a changed source cannot reuse the old checkpoint.

The production repository preserves complete events in matter-scoped attempts,
using ordered JSON arrays for supplied headers and values. Batches are bounded
by both 256 records and 4 MiB of decoded UTF-8 payload. A single larger event is
persisted alone; batch limits never truncate the total record population.
Capacity, read or persistence failures retain explicit incomplete coverage.

Record-oriented family checks compare only actual declared records. They do
not enumerate numerical label ranges or walk source directories. Literal
registered marks remain literal. Supplied page counts, extracted page counts,
page-record ordinals and produced page labels have separate meanings.

Archive schema coverage includes the three production tables and the exact raw
arrays. Backup and database restore are distinct from preservation export.
Both require complete schema and source integrity validation.
