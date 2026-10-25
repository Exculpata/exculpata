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
labels across documents in computed series are flagged with those documents
cited, once per shared range. Numeric suffixes are compared after NFKC normalization and surrounding
whitespace removal; prefix case remains significant. Each prefix has a cited
`bates_series` status: computed or not computed, with reasons. Reversed ranges
and prefixes ending in a digit block only their series; cross-prefix ranges block
both series. Nonnumeric labels without trailing digits block no numeric series.
Control, format or combining characters and non-Latin or mixed-script prefixes
cannot be scoped safely: an ALL status names their cause and every series is
marked not computed. Unresolved labels retain notices and exact duplicate checks.
Series differing only by case, punctuation or whitespace receive a notice but
remain separate. Gap labels use the widest zero-padded suffix in their series.
Page-only locators do not consume the Bates limit. Every emitted Bates fact,
including gaps, counts toward the finding limit.

Coverage facts cite documents without Bates labels, including failed, needs-OCR
and page-only sources; these documents do not change series status or gap facts.
Documents with multiple recorded pages and a start label
without an end receive a separate coverage count and a series caveat. Page count
never invents an end label. A gap bracketed by a multi-page start-only document
states that covered documents may hold unrecorded labels. Gaps are internal
intervals between recorded labels only, not a conclusion about completeness.
The export header directs reviewers to series status and coverage notices and
states that an empty gap list does not establish completeness.
Explicit `Reference: filename` lines match selected filenames or relative paths
case-insensitively; other reference language needs human review. Missing entity
support and text/OCR coverage notices remain visible. These checks cannot establish
absence from unextracted text. Limits refuse oversized inputs instead of truncating.

An interrupted or malformed derived-unit stream makes that document's facts
incomplete. Its dates and text references are excluded from summary coverage,
and it contributes neither recorded page totals nor an undated-document count.
Locator metadata remains available for Bates gaps and overlaps independently of
text readability. Remaining aggregate figures are marked incomplete; no closed
page gap is emitted for the unreadable document. Source inventory and the
incomplete-read notice still link to it. Date recognition excludes explicit
Bates/reference lines and date tokens embedded in paths or hyphenated identifiers.
Markdown treats source text as literal text, including strike markers, heading
markers and bare web URLs; source links remain navigable. Control, format and
combining characters are displayed as Unicode code points instead of invisible
formatting.

UI integration, selection receipts for files without document identities, reading
PDF page-label metadata, and PDF export remain follow-up integration work.
