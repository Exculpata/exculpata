# Production summary engine

`case_intelligence.production_summary.build_summary` returns source-linked facts;
`summary.to_markdown()` exports them. Supply the selected production's complete
`ProductionSource` inventory (including failed sources), its name and matter slug.
Each source wraps the existing `PilotDocument`, its store action token and optional
version-matched `SourceLocator` Bates metadata. Entity inputs are `(entity, mentions)`
pairs from `EntityService.detail`; they retain status and validated passage support.

This is an in-process engine, not an authorized endpoint. The caller must authorize
the matter and hold its source guard while collecting inputs and building the
summary. Never pass only ready documents. It reads derived units through the existing
streaming reader, does not mutate evidence, and does not call a model or network.

Counts cover supplied document identities; pages mean recorded PDF pages, not text
lines, email sections or spreadsheet rows. Dates are valid ISO calendar dates stated
in extracted text, not document creation dates. Only supplied version-matched
locators from document metadata or production load data define Bates labels.
Explicit `Bates: LABEL` or `Bates: START to END` text lines are cited references;
they never label the citing document or close gaps. Duplicate or overlapping
labels across documents are flagged with those documents cited. Numeric suffixes are
compared within each exact prefix; nonnumeric and cross-prefix ranges are reported
as unordered. Gaps are internal intervals only, not a conclusion about completeness.
Explicit `Reference: filename` lines match selected filenames or relative paths
case-insensitively; other reference language needs human review. Missing entity
support and text/OCR coverage notices remain visible. These checks cannot establish
absence from unextracted text. Limits refuse oversized inputs instead of truncating.

UI integration, selection receipts for files without document identities, reading
PDF page-label metadata, and PDF export remain follow-up integration work.
