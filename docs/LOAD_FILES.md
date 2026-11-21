# Import a discovery production

In the matter's **Sources** page, use **Choose a folder** under **Add sources**
and select one complete production folder. Include the load files and the
native, produced text and image files you received. A path written in a load
file can identify a file in the selection. It cannot transfer that file's bytes.

For example, two deliveries can both contain a page labelled `ABC0001`:

```text
Production_A/
  LOAD/records.dat
  LOAD/pages.opt
  NATIVES/report.pdf
  TEXT/report.txt
  IMAGES/ABC0001.png

Production_B/
  LOAD/records.dii
  LOAD/pages.lfp
  IMAGES/ABC0001.png
```

1. Select `Production_A`, name the collection and review the selection. Confirm
   it and open the selected-file receipt to check which files transferred or
   were skipped. In this example the DAT declares Begin Bates `ABC0001`, native
   path `NATIVES/report.pdf` and text path `TEXT/report.txt`. The OPT declares
   the document break, page label and path `IMAGES/ABC0001.png`.
2. Select `Production_B` separately and confirm it with its own receipt. In
   this example the DII declares document Begin Bates `ABC0001`, and the LFP
   declares a page with that label and the path to the second production's
   image. Keeping the deliveries separate lets each record match within its own
   receipt and top-level folder even when filenames and Bates labels recur.
3. Open **Review → Production documents**. Check the accepted, rejected,
   matched, unmatched and conflicted counts for each format. If indexing is
   pending, use the refresh link. Open a matched source's **Fields** to inspect
   document metadata or **Document** to inspect supplied page labels and an
   available page image.
4. To investigate an unmatched record, select the load file itself. Use
   **Fields** for DAT or DII document records and **Document** for OPT or LFP
   page records. The load-file name, version, line and record ordinal identify
   the supplied values. An unassociated record and a skipped file have
   different outcomes. Use the selected-file receipt to check whether the
   referenced original reached the matter.

The importer accepts the documented DAT, OPT, LFP and DII dialects. DAT and
DII supply document metadata. OPT and LFP supply explicit page records. A
uniquely associated JPEG or PNG can appear beside its page record. TIFF
originals can be admitted, but inline TIFF page previews are unavailable.
DII supplies no per-page Bates. The original load files are stored without
searchable text. Supported source types, grammar and preview behavior are
described in [Discovery review](DISCOVERY_REVIEW.md).

## Offline load-file quality checks

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
