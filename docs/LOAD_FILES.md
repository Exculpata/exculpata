# DAT and OPT load files

The LF1 load-file module is a pure, read-only library. It does not connect to
intake, storage, user interfaces, workflows, or exports. Call `parse_dat` and
`parse_opt` to stream records, or `qc_volume` to obtain a dry-run report.

DAT parsing recognizes the Concordance field delimiter (ASCII 20), thorn quote
(ASCII 254), and embedded-newline marker (ASCII 174). It detects CP1252, UTF-8
(with an optional BOM), and UTF-16 byte order. Headers and Bates labels remain
exactly as produced. Broken or mixed encodings produce a cited error rather than
a guessed conversion.

OPT parsing expects seven columns per page and retains each image key unchanged.
Volume-relative text, native, and image paths reject absolute paths, parent
traversal, and symlink escapes.

The dry-run report counts documents and pages and reports DAT-only or OPT-only
keys, missing and orphan files, field-count errors, encoding errors, Bates gaps
and possible overlaps, orphan attachments, invalid ISO dates, and hash
mismatches. Findings contain a source file and line. `propose_field_map` provides
conservative header suggestions; `FieldProfile.to_data` is a storage-neutral
producer profile format.

Later work will separately cover LF2 intake, LF3 user interface, and LF4 export.

`QCReport.joined_pages` maps each OPT document-break key to its ordered page
keys, including duplicates; `families` groups DAT members by their exact
BEGATTACH/ENDATTACH pair. Missing family boundaries are findings. OPT document
page counts and each page's membership in the DAT range are checked.
`FieldProfile.from_data` validates producer names, supported target fields and
one header per target. Pass its `fields` as `field_map` to QC or DAT parsing.
This also protects custom-mapped Bates columns from newline-marker conversion.

Strict parser calls raise `LoadFileError`; passing a `findings` list enables the
same row recovery used by QC. Bad widths and undecodable records are skipped
with their physical line numbers; empty-field records remain visible. UTF-32 is
rejected with a finding. Universal newline decoding handles CR, LF and CRLF
without text-cookie arithmetic. Detection reads only 64 KiB and uses an
incremental UTF-8 decoder at the prefix boundary. Records are capped at 1 MiB
of decoded characters, including multi-line quoted fields; an oversized record
is discarded through its current physical line before parsing resumes.

Parsing retains one bounded record. QC retains compact Bates indexes, joined
page keys, family members, referenced paths and findings, so memory grows with
those results rather than metadata or text payloads. Duplicate checks use one
pass. A timed generated 200,000-row test measures peak traced memory.

Range comparisons accept exact ASCII prefixes followed by 1 to 18 ASCII digits.
Non-plain labels, reversed ranges and differing prefixes are reported. Gaps
use the maximum preceding end and distinguish possible from certain gaps when
padding or labels are ambiguous. Parity tests compare these facts with
`bates_gaps`; the layer boundary keeps the implementation here pending a shared
lower-layer helper. Labels in records are never normalized or renumbered.

Generate SYNTH001 (50 documents, 180 pages, three natives and one family), its
encoding variants and a copy of the checked-in SYNTH002 QC volume in temporary
storage using `tests/fixtures/load_files/SYNTH001/generate.py`. The script's
`generate(destination)` function takes a fresh destination directory. The
synthetic image paths contain text stand-ins; LF1 validates paths, not images.
SYNTH002 includes an overlay DAT for parsing tests only, not overlay import.
