# S4 implementation on current main

## Kickoff

- Goal and user-visible outcome: mechanically apply Astra slice 04-s4 onto current main so Production documents shows literal LFP page records and supported DII document metadata, with zero DII page records and no invented Bates. Reuse the existing stored-index owner, real target/version, and bounded publication hooks.
- Expected files: `specs/item114-s4-lfp-dii.md`; the six modules in `src/case_intelligence/source_image_load_files/`; `tests/test_source_image_load_files.py`; `tests/test_discovery_review_image_load_files.py`; `src/case_intelligence/source_load_files.py`; `src/case_intelligence/modules/discovery_review/load_files.py`; `src/case_intelligence/modules/discovery_review/reader.py`; `tests/snapshots/import_layers.json`; `docs/DISCOVERY_REVIEW.md`; only the two stale registration assertions and their extensions in `tests/test_discovery_review_dat_opt.py`. The kickoff is retained in the spec in the sole first commit; this local PR body remains excluded from Git.
- Acceptance check: generated narrow-subset parsers and three strict memory profiles pass; actual uploaded-review lifecycle and DAT/OPT regressions pass; main's publication ownership remains intact; import/boundary, size, network and private-term checks pass without baseline or scanner changes. Full CI parity is delegated to the launcher by the task instruction; no result will be claimed before execution.
- Issue: https://github.com/Exculpata/exculpata/issues/503 (item 114), related https://github.com/Exculpata/exculpata/issues/279 and merged https://github.com/Exculpata/exculpata/pull/526.

Recorded before applying implementation. Live issue #503 has no comments or assignees and the open LFP/DII PR search returned none. The supplied task narrows its slice stack to s4 interpretation after merged s4a. This kickoff matches that scope. The separately held publication-work branch is excluded.

## Build reconciliation

The design below is retained as the supplied historical proposal. For this build,
main is `6a39e60fb78b54065bdcd6623e9917b4f012f813`, including s4a (#526).
The generated grammar was approved at 2026-11-14T12:08; approval is no longer open.
The shared admission grammar and test generator already exist and are reused.
The existing stored-index owner supplies the real publication target, version and
bounded hooks. No stored-index, admission, router, schema or rendering changes
are part of this slice. The additional modification to
`tests/test_discovery_review_dat_opt.py` updates and extends only its two stale
LFP/DII registration tests. The kickoff above was recorded in the local PR body
before implementation and is retained here in the first commit.

## Decision summary

**Item 114, slice 4: LFP and DII image-index parsing**
DRAFT r3, author: Codex astra high, for review

Goal: extend the existing load-file pipeline to supported LFP and DII inputs without inventing evidence.
Outcome: reviewers see explicit LFP page labels, supported DII document metadata, existing DAT families, and cited QC failures.

- **RULED (Exculpata 8:25 PM ET):** Fixtures are generated from test code only; no real production or vendor-tool sample is used.
- **RULED (Exculpata 8:25 PM ET):** DII supplies document-level Bates only: document begin and optional declared page count. No per-page DII Bates without verified public syntax.
- **RULED (Exculpata 8:25 PM ET):** Keep refusing `<<DOC>>` syntax.
- **RULED (Exculpata 8:25 PM ET):** Separate 4a intake PR, with its own review and the same shared, content-based admission boundary as 3a.
- **RULED (Exculpata 8:25 PM ET):** Both parsers compare 5,000 against 20,000 records using `tracemalloc`; peak growth must be below 1.5×, with exact counts.
- **RULED (Exculpata 8:25 PM ET):** All builder scratch and browser output stays inside its worktree.
- **RULED (Exculpata 9:01 PM ET):** 4a extends both upload JavaScript suffix lists and both workbench file/folder `accept` attributes for LFP/DII; retain unsupported-type, size, count, and scanner checks.
- Stack: s1 → s2 → s3a → s3 → **4a intake** → **s4 parsers** → 5a → 5b.
- 4a parent: `codex/discovery-dat-opt-114-s3`.
- Proposed branches: `codex/discovery-intake-lfp-dii-114-s4a`, then `codex/discovery-lfp-dii-114-s4`.
- Migration: **none** for either PR; other slices’ migration numbers remain provisional.
- PR split: **two PRs**, admission followed by interpretation.
- Fence: 4a **3 new / 7 modified** files, covering admission, browser selection, and its regression harness; s4 **9 new / 5 modified**, chiefly `source_image_load_files/` and review integration.
- Headline proofs: file and folder selection through upload confirmation; both upload paths; hostile-input rejection; exact OPT/LFP page parity; DII document parity; inert paths; quantitative streaming memory; original-hash and lifecycle preservation.
- Known limits: `<<DOC>>` has no verified public specification in the repo or handoff; DII per-page Bates and universal three-format page-record equality are not established; rendering remains slice 5.
- Open: approve generated LFP/DII grammar examples and their field mappings; reconcile the landed parent’s public contracts and exact paths.

REFS: #503 (https://github.com/Exculpata/exculpata/issues/503)

## Appendix

### A1. Kickoff

- **Goal:** Add trustworthy LFP page interpretation and bounded DII document interpretation through the existing review pipeline.
- **User-visible outcome:** Supported metadata appears with its source, missing page labels remain absent, and malformed or ambiguous input remains visible.
- **Expected files:** The two exact fences in A8 cover admission, browser selection, shared parser support, review adapters, generated fixtures, tests, and documentation.
- **Acceptance check:** Generated inputs survive file/folder selection, admission, interpretation, matching, display, preservation, and deletion without altered originals, invented Bates, hidden failures, or production-sized parser collections.
- **Issue link:** [#503](https://github.com/Exculpata/exculpata/issues/503). Related: [#279](https://github.com/Exculpata/exculpata/issues/279), parent [PR #500](https://github.com/Exculpata/exculpata/pull/500).

Inspected baseline: `e6644a225f75c2cedbf3e03a17bd0205e6a006e3`.
Read the r2 base note and prior review; its sole open finding was P2, the missing browser-intake fence and regression. The earlier memory-threshold finding was resolved.
This revision involved reading only: no files changed, tests executed, or browser results claimed.
The fetched #503 records the fixed slice stack and synthetic-fixture requirement.
The fetched #279 remains open, without assignees or comments; it places LFP and DII in later, separate work.
The inspected #500 body excludes these capabilities; its fetched discussion contained no comments.
Before coding, refresh comments, claims, assignments, related PRs, and holds.
Record each PR’s kickoff before implementation, in its PR body or a task file included in its first commit.

### A2. Problem and verified current behavior

All code paths below are relative to the repository root; line numbers refer to the inspected baseline.

| Evidence | Current behavior |
| --- | --- |
| `src/case_intelligence/pilot_uploads.py:69`, `DOCUMENT_MEDIA_TYPES`; `:1473`, `PilotStore.validate_name` | DAT, OPT, LFP, and DII suffixes are absent from admission. |
| `src/case_intelligence/static/case-intelligence.js:468`, `securityCheckedSuffixes`; `:1512`, `reviewSuffixes` | Both lists omit LFP/DII. `requiresSecurityCheck` uses the first at `:472`; `validateSelectedFiles` rejects unsupported suffixes at `:1513` and retains empty/size checks at `:1514–1516`. |
| `src/case_intelligence/templates/workbench_setup.html:46`, `#source-files`; `:47`, `#source-folder` | Both upload inputs have explicit `accept` lists omitting LFP/DII. |
| `src/case_intelligence/templates/workspace.html:48`, `#pilot-files` | The other upload `accept` list is the legacy PDF/TXT pilot selector; s3a explicitly excludes that surface, and 4a retains that boundary. |
| `src/case_intelligence/modules/discovery_review/load_files.py:69`, `parse` | Selects CSV/TSV by suffix and decodes UTF-8 with optional BOM. |
| Same file, `parse`, lines 99–103 | Stops at a row limit and clips cell values to 4,000 characters. These are not acceptable new parser semantics. |
| `src/case_intelligence/modules/discovery_review/reader.py:178`, `read_load_files` | Filters uploaded, ready sources by suffix and size, then reads the whole stored source with digest verification. |
| `src/case_intelligence/source_load_files.py:11,214`, `OptPage`, `parse_opt` | Streams seven unchanged positional strings plus file and line; this is not a verified slice-3 semantic page type. |
| `src/case_intelligence/source_load_file_qc.py:33`, `qc_volume` | Materializes DAT/OPT records, checks referenced files, and derives attachment groups from mapped DAT fields. |
| `src/case_intelligence/modules/discovery_review/reader.py:26`, `EMBEDDED_MEDIA_TYPES` | Document embedding supports PDF, JPEG, and PNG. Parsing LFP/DII does not add rendering. |

Handoff qualifications:

- Slices 1–3 are absent from this checkout; their landed interfaces and test results require build-time verification.
- The actual field JSON is `src/case_intelligence/config/discovery_review_fields.json`, loaded at `modules/discovery_review/fields.py:183`.
- SQL deletion belongs to `WorkspaceStore.complete_matter_purge`, `workspace_store.py:2152`.
- `modules/discovery_review/purge.py:9`, `purge_hook`, clears the review cache and deletes the reserved render-cache directory.
- The handoff’s unfinished-publication statement is historical: #500’s inspected body now reports a clean outgoing scan, not validation of this future work.
- The newer DII ruling narrows the handoff’s unconditional “same page records as parse_opt” statement.

### A3. Root cause

The baseline server rejection occurs in `PilotStore.validate_name`: `_ALLOWED` has no LFP/DII entries.
Browser intake also has independent suffix and selector lists, identified in A2.
After admission, `reader.read_load_files` and `load_files.parse` still lack content-based image-index interpretation.
`parse_opt` cannot interpret another format’s boundaries by treating its fields as seven OPT columns.

Do not reuse `qc_volume` as the review parser.
It builds complete tuples at lines 50–51, resolves referenced paths at lines 62–76,
reads native files for hashing at lines 91–99, and walks the volume at lines 149–159.
Those operations have a different authority and memory contract from parsing an admitted load file.

### A4. Approach

#### Format knowledge and approved grammar

**DECISION D1:** Implement only explicitly documented, maintainer-approved synthetic dialects; reason: a format name does not establish field meaning.

LFP confidence is high for command records beginning with `IM`, predominantly comma-separated parameters,
and semicolon-separated location components. An IM record identifies an image/page; the format also has other commands.
The documentation describes boundary, position, location, type, and optional rotation.
See [Ipro’s LFP overview](https://my.ipro.com/help/ECapture/Content/eCapture/LFP%20Files/IPRO_Tech_LFP_Files_Overview.htm).

Confidence is lower for universal boundary and offset interpretation.
Boundary letters can be project-defined, and offsets depend on the image representation.
Approve the supported boundary letters, location forms, escaping, optional fields, and offset interpretation before coding.
Do not treat every nonempty boundary as a document start or assume ordinary CSV quoting.
See [Ipro’s IM reference](https://my.ipro.com/help/ECapture/Content/eCapture/LFP%20Files/Import_a_New_Page_%28IM%29.htm).

DII confidence is high for a tagged, multiline structure, but low for a universal grammar.
ReadySuite documents `@FIELD`/`@C FIELD`, `@T`, and `@PARENTID`; that describes its implementation, not a complete interoperability specification.
Its page-number behavior is not authority to infer Bates here.
See [ReadySuite’s supported formats](https://help.kldiscovery.com/readysuite/overview/supportedformats).

**RULED (Exculpata 8:25 PM ET):** Refuse `<<DOC>>` syntax; neither the repo nor handoff supplies a verified public specification.
Approve DII record starts/ends, document-begin mapping, optional count syntax, repeated tags,
multiline values, image-reference syntax, and recovery boundaries using generated examples.
Approval establishes this supported subset; it does not establish compatibility with all DII producers.

**RULED (Exculpata 8:25 PM ET):** A small generator in tests constructs bytes from code.
Use invented document keys, paths, counts, and malformed variants; no copied production or sample exported by a vendor tool.
Store grammar expectations in the specification and generated test assertions.

#### Separate 4a admission

**DECISION D2 — RULED (Exculpata 8:25 PM ET):** Use a separate 4a PR; reason: sensitive intake deserves its own review.

4a extends the parent’s shared admission classification to `.lfp` and `.dii`.
Suffixes identify eligible upload types; accepted content must satisfy the documented textual admission subset.
Do not use a particular filename, suffix alone, or an incidental `IM` substring as proof.
Keep DAT/OPT’s existing parent validation unchanged.
For the newly admitted formats, exhaust bounded text validation; do not accept binary tails after a valid prefix.
Admission alone does not certify document associations, Bates meaning, or supported rendering.

The adjacent 3a design proposes `source_admission/__init__.py` and `source_admission/load_files.py`.
Those paths are planning evidence, not landed code; re-verify their public interface before using the A8 fence.
**DECISION D13:** Extend that shared admission owner and export its content classification for s4 reuse; reason: avoid competing recognition rules or a dependency on the later parser PR.
4a must remain independently buildable; its tests must not import s4-only code.

Preserve ordinary and resumable MIME, size, safe-name, staged-file, digest, and malware checks.
Baseline `store_stream` delegates to `_store_stream_locked`; both it and `finalize_resumable_upload`
check MIME agreement and call `_scan_staged` before signature validation
(`pilot_uploads.py:1563,1592,1654,1848,1923`).
`_scan_required` and `_scan_staged` at lines 1023–1059 preserve policy-dependent scanning and blocking failures.
Extend required extended-policy scanning to both new suffixes; scanner absence, infection, and indeterminate results still block.

**RULED (Exculpata 9:01 PM ET):** Extend `securityCheckedSuffixes` at `static/case-intelligence.js:468`
and `reviewSuffixes` at `:1512` with `lfp` and `dii`, and add `.lfp,.dii` to both
`workbench_setup.html:46,47` `accept` attributes. The s3a design extends these same lists for DAT/OPT;
it does not supply a shared dynamic inventory. Retain its DAT/OPT entries and existing size/count checks.
The separate `mediaSuffixes` list at `case-intelligence.js:1511` stays unchanged: these inputs use the document-size limit.
The legacy selector at `templates/workspace.html:48` remains excluded, consistent with s3a.
No new `+`, `+=`, or `${}` interpolation constructions are permitted.

Verify parent stored-only readiness, protected serving, extraction dispatch, and supported-type messages.
If additional parent-owned behavior remains suffix-specific outside A8, revise the fence before coding;
the known browser lists above are already included and are not deferred to that reconciliation.

#### Package and shared records

**DECISION D3:** Use `case_intelligence.source_image_load_files` with a thin public entry point; reason: core OPT and review can share it without core-to-feature imports.

`__init__.py` exports public records and parsing functions.
`records.py` owns the parent page contract and any necessary document-result/diagnostic contract.
`stream.py` contains only reusable bounded decoding and record support.
`detect.py` adapts the common content classification; `lfp.py` and `dii.py` own format interpretation.
Format implementations depend on shared contracts, never on each other.
Extract only the record/stream seam needed here; keep DAT mapping and `FieldProfile` in `source_load_files.py`.
Preserve existing public imports and parent behavior; do not import another module’s `_records` or `_Findings`.

`modules/discovery_review/identifiers.py:35`, `IdentifierRepository`, demonstrates narrow ownership through a public boundary.
A pure parser needs neither its own database connection nor a new repository.
Every new production Python file, including package `__init__.py`, gets a `core.sources` entry in `tests/snapshots/import_layers.json`.
Test helpers are outside that production-module inventory.
No imports of `workbench.py` or `workspace_store.py`, cross-feature imports, or underscore-prefixed attribute access.
Keep the existing import checker and forbidden-edge baseline unchanged
(`tests/test_import_boundaries.py:20,108,131`; `tests/test_discovery_review_boundaries.py:12`).

**DECISION D4:** Reuse the landed OPT page type exactly and distinguish document results from page results; reason: unavailable page evidence must not become synthetic pages.
LFP emits that page type for supported explicit image records.
Preserve labels, leading zeroes, punctuation, order, source version, record locator, and produced optional values.
Never increment a beginning Bates, expand a range, derive labels from filenames, or repeat a document identifier across pages.

**RULED (Exculpata 8:25 PM ET):** DII’s supported output is document-level Bates only.
Emit document begin and an optional supplied page count when the approved grammar establishes them.
Count explicit page entries separately when present; do not expand a count into page records or attach labels to those pages.
DII document metadata uses the parent’s existing document-association path; do not disguise it as a page record.
If the parent cannot represent this distinction, resolve its public contract before implementation.
Full three-format page-record equality is therefore unavailable in this scope.
Test full OPT/LFP page equality and three-format agreement on genuinely shared document facts, explicitly asserting DII’s absent page output.
A future verified DII page syntax would require a reviewed scope revision.

#### Detection, failures, and publication

**DECISION D5:** Recognize bounded content, then validate interpretation completely; reason: a plausible prefix cannot validate a production.
Return recognized, unsupported, malformed, and ambiguous outcomes.
Replay the bounded prefix or reopen the same verified source version; never lose bytes consumed by detection.
Insufficient recognition evidence yields an explicit outcome, not a suffix-based guess.
After recognition, malformed later content cannot trigger fallback to another grammar.

**DECISION D6:** Withhold unsafe associations while retaining cited failures; reason: recovery must not certify damaged evidence.
LFP may resume at an independently validated record; DII requires an approved unambiguous document boundary.
Reject conflicting singleton tags, unknown structural commands, missing required fields, nested boundaries, and truncated blocks.
Reset document, directory, count, and relationship state after a damaged DII block.
Use the parent’s staged publication and revision checks; if absent, validate and replay stable input without whole-file buffering.
Stale source versions and incomplete generations cannot appear current.

#### Encodings, bounded memory, paths, families, and QC

**DECISION D7:** Preserve the existing decoder’s supported encodings and explicit failure semantics; reason: identical bytes should not acquire format-dependent labels.
`source_load_files._encoding:84` recognizes UTF-8, UTF-8 BOM, BOM-marked UTF-16, and sample-selected CP1252.
It rejects UTF-32 and unmarked NUL-bearing input; `_records:133` reports damaged decoding and oversized records.
CP1252 selection is heuristic; never describe it as producer attestation.
Keep the 1 MiB record bound; additionally bound DII physical lines, values, and retained document metadata.
Do not apply a whole-document size cap merely because a document contains many individually valid page entries.
Consume those entries incrementally and retain exact counters, not a page list.
Use parent storage for collection-wide conflict checks; avoid parser-wide sets growing with production size.
Oversized values produce cited failures, never sliced successful values.
Preserve parent per-record diagnostic streaming; bounded display examples must not erase available rejected-record locators.

**DECISION D8:** Embedded locations remain inert data; reason: source text cannot authorize filesystem or network access.
The parser reads only its authorized load-file input.
Never open, resolve, `stat`, glob, or filesystem-join image paths, directory tags, volumes, or filenames.
Keep lexical comparison keys separate from produced strings; do not call `resolve_volume_path` or `qc_volume`.
Traversal, absolute/drive/UNC/URI paths, NULs, and symlink targets remain blocked from filesystem use.
The parent’s authorized association boundary decides whether a reference matches an admitted source.

**DECISION D9:** Reuse slice 3’s DAT family authority; reason: the handoff defines families through BEGATTACH/ENDATTACH.
LFP boundary letters and DII parent tokens cannot create a second family system.
Reuse the same document matching and family validation; expose unsupported or conflicting relationship assertions as QC.
Missing attachment metadata creates no inferred family.

**DECISION D10:** Use plain, source-cited QC and exact coverage counts; reason: reviewers must distinguish missing evidence from successful interpretation.
Examples: “Unsupported image boundary,” “Document record is incomplete,” and “No page Bates supplied.”
Keep document count, explicit page-entry count, declared page count, emitted pages, rejected records, and unmatched associations distinct.
Unknown totals stay unknown; document-only DII output never implies page-label coverage.
Do not put raw payloads or host paths into logs or public artifacts; preserve local source/version and line/block locators.

#### Schema, routes, and JavaScript

**DECISION D14:** Add no table, renderer, cache owner, endpoint, or browser control; reason: extend the parent’s import contracts.
Migration number, schema changes, archive-map changes, purge implementation, and route regeneration are not applicable.
4a modifies the existing browser intake lists and selectors under the 9:01 PM ruling; s4 requires no JavaScript edits.
If storage becomes necessary, revise scope and complete all four schema steps together:
(1) packaged SQL plus byte-identical `migrations/sqlite/` mirror;
(2) migration filename in `WorkspaceStore.__init__`’s hard-coded list;
(3) explicit DELETE in `WorkspaceStore.complete_matter_purge`;
(4) exact schema columns and archive disposition in `matter_archive_schema.py`.
The inspected 0045/0046 migration pairs are byte-identical and have registration, deletion, and archive coverage
(`workspace_store.py:271,2202`; `matter_archive_schema.py:101,132`).
`scripts/rename_javascript_policy.py:35`, `inspect_tree`, rejects `+`, `+=`, and `${}` interpolation, among other constructions.
Apply that restriction to new browser-test JavaScript as well as application edits; do not copy prohibited constructions from older harness examples.
If routes change after a fence amendment, run `python -m tests.test_api_snapshots --update routes` and review both route configurations (`tests/test_api_snapshots.py:33,121`).

### A5. Vision/AGENTS check

- **No modes:** Automatic recognition extends the existing Production documents workflow.
- **No single-use features:** Shared parser and admission contracts support multiple formats and existing consumers.
- **No dropped or capped computed numbers:** Counters remain exact; resource failures are explicit; Bates is never computed.
- **No filename allowlisting:** Common content validation governs eligible types; no individual-name exception.
- **No weakened egress/privacy/safety:** No outbound calls, referenced-path reads, scan bypass, permission changes, or serving-policy relaxation.
- **Neutral language:** Use matter, production, source, document, page, and reviewer; exclude private affiliations and deployment details.
- **Synthetic only:** Generate bytes and hostile cases from code, with no real or vendor-exported samples.
- **Evidence integrity:** Preserve original hashes, source/version locators, and visible uncertainty; DII document Bates stays distinct from page Bates.
- **Contributor practice:** Record kickoff and cheap failing tests first; use repositories, not raw cursor reads; verify uploaded artifacts.
- **Merge authority:** Six required CI checks and resolved conversations remain gates under `docs/QUALITY_GATES.md:5`; maintainer merge only, no force-push or hook bypass.

### A6. Blast radius

| Area | Coverage |
| --- | --- |
| Callers | Preserve DAT/OPT public imports and decoder behavior; run existing parser/QC suites unchanged. |
| Checks | Import registration, feature isolation, fixture policy, rename contract, lint, and source-size ratchet remain active. |
| Data | Preserve literal values and provenance; reject duplicate/conflicting joins and stale source versions. |
| Docs | Describe the approved grammar, DII document-only output, admission limits, and parsing/rendering distinction. |
| Archives | Preserve admitted originals through parent source coverage; retain parent treatment of derived index state. |
| Purge | Introduce no storage owner; test removal of parent associations through ordinary authorized deletion. |
| Performance | Quantify parser memory; re-run parent indexing/navigation checks without invoking volume QC from list requests. |
| Security | Exercise file/folder selection, both upload paths, scanner failures, inert references, cross-matter denial, protected serving, and HTML-safe QC. |

`tests/test_source_load_file_qc.py:202`, `test_qc_memory_grows_linearly`, permits growth below 6× for 4× input.
It is not evidence of flat parser memory.
Recheck the parent’s million-item first/next/previous-page target on a stated environment; do not claim it was measured here.

### A7. Proving tests

**DECISION D11:** Use the following generated contract and actual-artifact tests; reason: parser success alone cannot establish intake or review behavior.
All new test names below are proposals.
Write valid browser selection, valid admission, valid LFP, valid DII, and parity failures before implementation; record the actual parent SHA and failing output.
Generate fixtures before memory measurement, warm the parser, collect garbage, and consume outputs incrementally.

| Proposed test file and test | Should pass | Must still be caught |
| --- | --- | --- |
| `tests/test_lfp_dii_admission.py::test_lfp_dii_selection_contract` | Both rendered inputs accept `.lfp` and `.dii`; actual JavaScript validation accepts file and folder descriptors, including uppercase suffixes; scanner classification includes both. | Either missing selector/list entry, unsupported suffix, empty/oversize input, excessive count, duplicate paths. |
| Same file, `test_admission_both_upload_paths` | Supported generated bytes pass ordinary and resumable intake under multiple safe names. | Suffix-only acceptance, wrong MIME, binary/executable content, malformed tails. |
| Same file, `test_admission_preserves_safety_gates` | Clean required scan and safe bounded input. | Infection, unavailable/indeterminate required scan, unsafe paths, oversize, changed staged bytes. |
| Same file, `test_admitted_indexes_remain_stored_only` | Original hash survives restart; protected serving and truthful readiness match 3a. | GET/HEAD/Range exposure, invented text units, inflated searchable coverage. |
| `scripts/browser-accept-loose-file-preflight.py`, LFP/DII file/folder regression case | Each format is selected through each input, reaches preflight, and passes confirmation into the real upload path under a clean synthetic scanner. | Selector/handler bypass, post-preflight suffix rejection, unsupported/oversize eligibility, scanner unavailability or failed scans being treated as acceptance. |
| `tests/test_source_image_load_files.py::test_lfp_explicit_pages` | Approved IM fields preserve labels, order, locations, and optional values. | Wrong arity, malformed escaping/location components, unsupported boundary/offset/rotation. |
| Same file, `test_dii_document_metadata_only` | Approved tagged blocks retain document begin and optional supplied count. | Missing/conflicting tags, orphan entries, nested/truncated blocks, `<<DOC>>`, emitted page Bates. |
| Same file, `test_opt_lfp_page_parity` | Equal semantic page records for one generated document set, including nonconsecutive labels. | Changed label, order, boundary, location, or optional value. |
| Same file, `test_opt_lfp_dii_document_parity` | Shared document facts agree; DII page output is explicitly empty. | Fabricated page equality, range expansion, filename-derived labels, repeated document Bates. |
| Same file, `test_provenance_is_format_specific` | Each output cites its own source/version/record. | Equality comparisons erase or invent provenance. |
| Same file, `test_detection_uses_content` | Identical bytes classify identically under eligible names. | Incidental marker text, ambiguous prefixes, recognition-limit guessing, fallback after corruption. |
| Same file, `test_supported_encoding_parity` | Supported encodings preserve text across sample/chunk boundaries. | UTF-32, unmarked UTF-16, mixed/invalid encoding, lossy label replacement. |
| Same file, `test_malformed_record_recovery` | Later independent records remain diagnosable. | State leakage, unknown structural commands, false complete status. |
| Same file, `test_record_limits_and_findings_counts` | Bounded records and exact rejected totals with locators. | Giant lines/metadata, hidden overflow, clipped successful values, lost parent diagnostic events. |
| Same file, `test_referenced_paths_are_inert` | Locations remain data; only the supplied input is read. | Traversal, absolute/drive/UNC/URI/NUL references and any attempted referenced-path I/O. |
| `tests/test_discovery_review_image_load_files.py::test_uploaded_formats_share_review_behavior` | Uploaded JPEG/PNG productions show explicit LFP page Bates, DII document metadata, and identical DAT families. | DII page-label invention, Text-view injection, ambiguous matches, orphans, cross-matter access. |
| Same file, `test_invalid_input_never_publishes_associations` | Valid source generations publish coherently. | Duplicate keys, late malformed blocks, changed bytes, conflicting metadata, stale generations. |
| Same file, `test_load_file_lifecycle_preserves_originals` | Archive hashes match originals; authorized deletion removes derived associations. | Changed-original acceptance, cross-matter inclusion, residual state. |

**RULED (Exculpata 9:01 PM ET):** File and folder selection must have fail-first LFP/DII regression evidence, with unsupported-type, size, and scanner controls retained.

**DECISION D15:** Pair source-executed JavaScript/HTTP tests in the admission suite with a case in the existing Selenium harness; reason: `accept` assertions alone miss JavaScript rejection after preflight.

Use `tests/test_rename_javascript_consumers.py:15,23`’s Node/declaration-extraction pattern to execute the actual `validateSelectedFiles`, `securityCheckedSuffixes`, and `requiresSecurityCheck` declarations with bounded synthetic file descriptors. Exercise both plain filenames and `webkitRelativePath` values; do not reproduce the suffix lists in a substitute validator. Render setup through HTTP and inspect each input’s `accept` tokens, following `tests/test_loose_file_preflight.py:919`’s rendered-input pattern. Missing Node is not a passing execution.

In `scripts/browser-accept-loose-file-preflight.py`, use the existing Selenium `File`/`DataTransfer`, `webkitRelativePath`, and dispatched `change` pattern at `:149–209` for **each** of `#source-files` and `#source-folder`. Supply valid bytes from the shared generator; assert both `accept` attributes separately because programmatic file assignment does not enforce picker filtering. Use isolated synthetic matters and scanner outcomes without altering existing cases. Follow selection through preflight and confirmation to a saved original: the real change handlers are at `case-intelligence.js:1987–1988`, while `startResumableUpload` invokes validation at `:1786`. A successful preview alone is insufficient.

For each selector, retain a valid DAT/OPT control consistent with s3a’s `test_load_file_http_selection_and_rejection`, reject an unsupported `.unsupported` file and an over-limit file, and retain count limits. Under required scanning, unavailable status must prevent eligibility; infection or indeterminate scan results must prevent admission. Preflight must not claim a completed scan. Record failure on `e6644a22` and on the actual pre-4a parent before implementation, then success on 4a; a server-only implementation must still fail the JavaScript regression.

**RULED (Exculpata 8:25 PM ET), P3 fix:** Add these three tests in `tests/test_source_image_load_files.py`:

- `test_lfp_memory_is_flat`: 5,000 versus 20,000 fixed-width IM records; assert exactly N emitted pages and correct values.
- `test_dii_memory_is_flat`: 5,000 versus 20,000 fixed-width document blocks; assert exactly N document results, exact declared/observed counts, and zero page-Bates records.
- `test_dii_large_document_memory_is_flat`: one document containing 5,000 versus 20,000 explicit page-entry records under the approved grammar; assert one document result, exactly N consumed page entries, the exact supplied count when present, and zero fabricated page records.

For **each** test: measure parsing with `tracemalloc`, require `small_peak > 0` and `large_peak < 1.5 * small_peak`.
Do not retain output lists or include fixture generation in the measured interval.
The large-document fixture must contain N entries, not merely a changed count token.
Precedent: `tests/test_source_load_files.py:335`, `test_parse_memory_is_flat`.
These tests must catch accumulated document/page lists and silently capped totals.

Retain `test_opt_rows_kept_exactly`, `test_encodings_identical`, `test_volume_path_rejects_escapes`, and `test_parse_memory_is_flat` in `tests/test_source_load_files.py:211,67,197,335`.
Retain `test_generated_volume_exact`, `test_family_and_orphan_attachment`, and `test_every_row_counted_or_cited` in `tests/test_source_load_file_qc.py:56,165,45`.
Use public stores/repositories; any necessary raw shared-workspace fixture write uses `tests.fixture_write.fixture_write`.
Exercise the real scan decision with synthetic scanner outcomes, not a mocked admission verdict.

### A8. Exact fences

**DECISION D12:** Use these fences, reconciled against the actual parent before coding; reason: absent parent implementations cannot justify silent scope expansion.

**4a, new — 3 files:**

- `specs/item114-s4a-intake-lfp-dii.md`
- `tests/test_lfp_dii_admission.py`
- `tests/fixtures/load_files/generate_image_indexes.py`

**4a, modified — 7 files:**

- `src/case_intelligence/pilot_uploads.py`
- `src/case_intelligence/source_admission/__init__.py`
- `src/case_intelligence/source_admission/load_files.py`
- `src/case_intelligence/static/case-intelligence.js`
- `src/case_intelligence/templates/workbench_setup.html`
- `scripts/browser-accept-loose-file-preflight.py`
- `docs/DISCOVERY_REVIEW.md`

**RULED (Exculpata 9:01 PM ET):** The JavaScript and setup-template entries cover the two suffix lists and both upload `accept` attributes identified in A2/A4.
The browser-harness entry implements **DECISION D15**’s selection regression only.
The admission package paths are conditional on the landed 3a layout.
The generator is ordinary test code, builds bytes directly, and is reused by s4 tests.
Keep `pilot_uploads.py` changes narrow and within its existing source-size ceiling.

**s4, new — 9 files:**

- `specs/item114-s4-lfp-dii.md`
- `src/case_intelligence/source_image_load_files/__init__.py`
- `src/case_intelligence/source_image_load_files/records.py`
- `src/case_intelligence/source_image_load_files/stream.py`
- `src/case_intelligence/source_image_load_files/detect.py`
- `src/case_intelligence/source_image_load_files/lfp.py`
- `src/case_intelligence/source_image_load_files/dii.py`
- `tests/test_source_image_load_files.py`
- `tests/test_discovery_review_image_load_files.py`

**s4, modified — 5 files:**

- `src/case_intelligence/source_load_files.py`
- `src/case_intelligence/modules/discovery_review/load_files.py`
- `src/case_intelligence/modules/discovery_review/reader.py`
- `tests/snapshots/import_layers.json`
- `docs/DISCOVERY_REVIEW.md`

**Must NOT touch in either PR:** `workbench.py`, `workspace_store.py`, `matter_archive_schema.py`,
`source_load_file_qc.py`, both migration trees, review `purge.py`, routes, presentation, field JSON,
templates/static assets other than 4a’s two named files, authentication/network policy, checker implementations, import-graph exceptions, or workflows.
Additionally, s4 must not touch `pilot_uploads.py`, the admission package, templates/static assets, or the browser harness; 4a must not add review interpretation.
All unlisted paths are excluded, including committed scratch/browser output and `.design-inputs/`.
If parent ownership differs, amend the exact fence and kickoff first.

4a should remain small; s4 will likely exceed 500 changed lines with extraction and tests.
There is no 500-line gate in the inspected rules.
Keep behavior-preserving shared support, LFP integration, and DII integration reviewable within s4, with final parity retained.
The fixed PR stack and branch names remain unchanged; any further PR split requires a separate scope revision.

### A9. Stacking

Fixed order: s1 → s2 → s3a → s3 → 4a → s4 → 5a → 5b.
4a builds on `codex/discovery-dat-opt-114-s3`; s4 builds on `codex/discovery-intake-lfp-dii-114-s4a`.
The s4 branch remains `codex/discovery-lfp-dii-114-s4`.
Assume only the handoff’s codes/events, stored index, DAT/OPT admission, OPT page records, Document-only page Bates, and DAT families.
Do not assume particular tables, diagnostics, ready-state handling, or parser signatures.
Re-verify public record/stream ownership, document-only result support, staged publication, source invalidation,
4a admission and browser selection, family matching, archive/purge disposition, and parent test filenames.
Reuse existing public seams and shrink the fence where possible; do not rewrite reviewed storage to fit proposed names.

### A10. Decisions log

- **D1:** Recommend explicit generated dialect approval; uncertain field layouts remain open.
- **D2 — RULED (Exculpata 8:25 PM ET):** Separate 4a intake review and the stated branch sequence.
- **D3:** Recommend a thin shared core parser package; extract only reusable record/stream support.
- **D4:** Recommend exact parent page-type reuse, separate document output, and honest parity boundaries under the DII ruling.
- **D5:** Recommend bounded content recognition followed by full interpretation validation.
- **D6:** Recommend withholding unsafe associations and preserving recovery/provenance through parent publication.
- **D7:** Recommend compatible bounded decoding, exact counts, and retained diagnostic events.
- **D8:** Recommend inert embedded paths and parent-owned authorized association.
- **D9:** Recommend DAT-only family authority.
- **D10:** Recommend cited plain QC with distinct exact coverage measures.
- **D11:** Recommend the named tests; generated-only fixtures and quantitative memory thresholds are **RULED (Exculpata 8:25 PM ET)**.
- **D12:** Recommend the exact fences; the browser intake files and retained selection safeguards are **RULED (Exculpata 9:01 PM ET)**.
- **D13:** Recommend one public admission classifier reused by s4, without a dependency from 4a onto s4.
- **D14:** Recommend no new schema, route, renderer, or storage owner; 4a extends existing browser intake controls.
- **DECISION D15:** Execute actual JavaScript declarations, rendered selector assertions, and the existing Selenium selection/confirmation path; reason: each catches a distinct part of the ruled browser-intake regression.

Remaining maintainer question: approve the generated grammar examples and mappings, particularly LFP boundaries/offsets and DII block/count syntax.
Fixture provenance, separate 4a, DII page-Bates limits, `<<DOC>>` refusal, memory thresholds, and required browser-intake coverage are settled.

### A11. Build command hints

Run only in a writable builder worktree. **RULED:** keep every scratch/runtime/browser output inside that worktree.
Use Python 3.12 and repository development dependencies; substitute the inspected direct-parent SHA below.

```bash
mkdir -p "$PWD/.scratch/tmp"
export TMPDIR="$PWD/.scratch/tmp"
S4_BASE='replace-with-inspected-direct-parent-sha'
git rev-parse HEAD
git status --short
git diff --check
git diff --name-status --no-renames "$S4_BASE"...HEAD
git diff --name-status --no-renames "$S4_BASE"
git ls-files --others --exclude-standard
make publication-check
make publication-candidate-check
make secret-check
.venv/bin/python -m ruff check src services scripts tests
.venv/bin/python scripts/rename_contract_check.py
.venv/bin/python scripts/rename_contract_artifacts.py --check
```

Compare committed, working-tree, and untracked paths with the appropriate fence, including deletions.
The inspected Makefile has no standalone generic fence command.
Import-layer and route checks are pytest tests; run them before broader application pytest:

```bash
.venv/bin/python -m pytest -q tests/test_import_boundaries.py tests/test_discovery_review_boundaries.py tests/test_api_snapshots.py
.venv/bin/python -m pytest -q tests/test_lfp_dii_admission.py tests/test_loose_file_preflight.py tests/test_intake_scan_status_http.py tests/test_rename_javascript_consumers.py
.venv/bin/python -m pytest -q tests/test_source_load_files.py tests/test_source_load_file_qc.py tests/test_source_image_load_files.py tests/test_discovery_review_image_load_files.py
.venv/bin/python -m pytest -q tests/test_discovery_review_list.py tests/test_discovery_review_mount.py tests/test_matter_archive.py
.venv/bin/python -m pytest -q tests/test_fixture_write_policy.py tests/test_fixture_inventory.py tests/test_fixture_inventory_coverage.py tests/test_fixture_reflective_deny.py tests/test_db_access_ratchet.py tests/test_source_size_ratchet.py
make check-fast
```

Run only existing tests at each stack stage; add landed parent suites after inspecting their names.
No snapshot update is expected; approved route changes require `.venv/bin/python -m tests.test_api_snapshots --update routes`.
`make check-fast` requires its Compose, scanner, pinned Gitleaks, and disposable PostgreSQL prerequisites (`Makefile:52,64,72,92`).
Missing prerequisites and historical sandbox failures are not passes.
Exercise actual generated uploads in the review UI and record Bates scope, families, QC, and unchanged hashes.

For 4a, run the extended selection harness with installed Chrome/ChromeDriver paths and a fresh worktree-local output directory; its CLI is defined at `scripts/browser-accept-loose-file-preflight.py:268`:

```bash
.venv/bin/python scripts/browser-accept-loose-file-preflight.py \
  --chrome-binary "$S4_CHROME_BINARY" \
  --chromedriver "$S4_CHROMEDRIVER" \
  --output "$PWD/.scratch/browser-s4a-r3-01"
```

Set `S4_CHROME_BINARY` and `S4_CHROMEDRIVER` to the verified executable paths first.
Keep the `TMPDIR` export above for browser profiles, runtime, generated inputs, and other temporary output; never use `/tmp`.

For the existing general browser harness, use a fresh in-worktree output path:
`.venv/bin/python scripts/run-browser-acceptance.py --output "$PWD/.scratch/browser-s4-r3-01"`.
Its general journeys do not replace the slice-specific uploaded-artifact checks.

### A12. Revision log

- **r1 P3:** Reverified `test_parse_memory_is_flat`; A7 now specifies sizes, `tracemalloc`, strict 1.5× threshold, and exact counts for both parsers and one large DII document.
- **Ruling 1:** A4/A7/A8 specify a code-based byte generator and prohibit real or vendor-exported samples.
- **Ruling 2:** Summary and A4/A7 distinguish DII document metadata from pages, refuse `<<DOC>>`, and remove the unsupported universal page-parity promise.
- **Ruling 3:** Summary and A4/A8/A9 give separate 4a scope, branch, parent, fence, and tests.
- **Ruling 4:** A7 replaces qualitative memory promises with three quantitative tests.
- **Structure, references, scratch rulings:** Summary/appendix layout, exact REFS statement, and worktree-local build/browser paths applied.
- **Retained passed findings:** Baseline parsing/admission, decoder bounds, import/JS checks, migration mirrors, SQL purge location, and archive coverage were re-read; no prior execution claim is adopted.
- **r3, prior-review P2 — RULED (Exculpata 9:01 PM ET):** A2/A4 identify both JavaScript lists, both workbench selectors, and the excluded legacy selector; A7/D15 specify fail-first file/folder selection through confirmation with retained negative controls; A8 expands 4a to 3 new / 7 modified files; summary/A11 match. REFS and kickoff now link #503, and stacking explicitly ends 5a → 5b.
