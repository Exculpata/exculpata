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
