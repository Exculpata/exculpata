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
