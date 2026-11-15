# Item 114, slice 3: DAT/OPT metadata, page Bates and families

DRAFT r3 (REFS update 9:01 PM ET), author: Codex astra high, for review

## Decision summary

Goal: Read admitted DAT metadata and OPT records without inventing identifiers, losing provenance or hiding records.
Outcome: Reviewers see matched metadata, parent-first families, literal OPT page labels and every unmatched, rejected or conflicting record.

- **RULED (Exculpata 8:25 PM ET):** Unmatched DAT rows remain visible without creating sources; TEXT ingestion and citations are deferred. No further issue-author agreement is required.
- **RULED (Exculpata 8:25 PM ET):** Retain provenance-bound pairing, conservative association/family rules and the r2 memory limits: D8, D11, D12, D18 and D19.
- **RULED (Exculpata 8:25 PM ET):** OPT navigation must reach every stored record through bounded database reads on both full-page and fragment endpoints.
- **DECISION D20:** Use a version-bound `record` cursor, never the capped `unit` parameter, for DAT/OPT records.
- **DECISION D21:** Remove the same arbitrary million-position ceilings from inherited `page`/`unit` validation; audit all new record and diagnostic pagination for equivalent ceilings.
- **DECISION D15:** Single PR, with parser/storage and presentation changes in reviewable commits; incomplete visibility must not become a separately delivered importer.
- Stack: s1 codes → s2 stored index → s3a intake → **s3 DAT/OPT** → s4 LFP/DII → s5 rendering.
- **DECISION D16:** Parent `codex/discovery-intake-load-files-114-s3a`; proposed branch `codex/discovery-dat-opt-114-s3`.
- Migration: proposed `0049_discovery_dat_opt.sql`, subject to allocation against the landed parents.
- Fence: 22 modified and 9 new files; discovery-review package, shared parser/QC, field JSON, migration mirrors, schema/purge registration, tests and docs.
- Headline proofs: lossless diagnostics beyond 1,000 locations; order-independent conflicts; literal tail record beyond ordinal 1,000,000 through both endpoints; streaming import through index publication; preservation and purge.
- Known limits: no phantom sources, TEXT ingestion, citation changes, inferred PDF correspondence, TIFF/Office rendering or arbitrary nested family ancestry.
- Open: no remaining maintainer question from the supplied rulings. Resolve actual parent index ownership and migration allocation before coding.

REFS: #503 (https://github.com/Exculpata/exculpata/issues/503)

## Appendix

### A1. Kickoff

- **Goal:** Extend Production documents to consume admitted DAT/OPT metadata, preserve its source identity and expose uncertainties.
- **User-visible outcome:** Unique matches fill existing items; valid families display parent first; Document shows literal OPT records; unmatched and failed records remain inspectable.
- **Expected files:** Parsing, production repositories/services, index integration, fields, routes, presentation, lifecycle registration, preservation coverage, focused tests and documentation; exact fence in A8.
- **Acceptance:** Synthetic inputs survive parsing, matching, restart, indexing, full-page/fragment navigation, preservation and deletion with unchanged originals, exact counts, complete locators and bounded memory.
- **Issue link:** [#503](https://github.com/Exculpata/exculpata/issues/503) (item 114, created by the maintainer); requested directly by the maintainer; related [#279](https://github.com/Exculpata/exculpata/issues/279), parent [PR #500](https://github.com/Exculpata/exculpata/pull/500).
- **RULED (Exculpata 8:25 PM ET), D1/D10:** This is the narrower review increment. Unmatched rows create no sources; TEXT and citations remain deferred. It does not complete #279.
- The refreshed #279 is open, has no assignees and has no comments. Its broader source-creation, mapping/confirmation and text/citation scope remains separate.
- Before building, refresh issue/PR comments, claims, assignments and holds; record this kickoff in the pre-coding PR body or the branch’s first task-file commit.

### A2. Problem: verified current behavior

References describe inspected checkout `e6644a225f75c2cedbf3e03a17bd0205e6a006e3`. Parent slices are absent; their implementation is not asserted.

| Location | Current behavior |
| --- | --- |
| `src/case_intelligence/modules/discovery_review/load_files.py:31`, `LoadFile`; `:69`, `parse` | `LoadFile` contains source ID/name, headers, tuple rows, path/Bates headers and problem text. CSV/TSV only; UTF-8 with optional BOM. Values are clipped to 4,000 characters and parsing stops at two million rows (`:99–105`). |
| `src/case_intelligence/source_load_files.py:189`, `parse_dat`; `:214`, `parse_opt` | DAT yields positional `DatRow(values, headers, file, line)`; OPT yields `OptPage(values, file, line)` with seven unchanged strings. Neither returns the review shape. |
| Same parser, `:37`, `_Findings.add` | Keeps 1,000 locations per code, then a dropped-count summary. Rejected records are not yielded as normal records, so later exact locations cannot be recovered from iterator output. |
| `src/case_intelligence/modules/discovery_review/reader.py:178`, `read_load_files`; `:201`, `build` | Filters suffixes to CSV/TSV, skips files above 64 MiB, calls `read_bytes()` and builds the review list in memory. |
| `load_files.py:130`, `Matcher.find`; `:146`, `attach`; `reader.py:232` | Matching uses paths, compatible unique filenames or Bates-named stems. First matching row wins; existing produced Bates/control metadata is not searched. |
| `src/case_intelligence/config/discovery_review_fields.json:61` | “Family begins/ends”, `BEGATTACH`/`ENDATTACH` aliases and default item fields already exist. Grouping is missing. |
| `src/case_intelligence/source_load_file_qc.py:33`, `qc_volume` | Materializes DAT/OPT tuples at lines 50–51. Family handling at lines 125–130 checks endpoint presence and groups identical pairs in input order. |
| `src/case_intelligence/modules/discovery_review/presentation.py:134`, `item_panel` | Document supplies the existing PDF/image content URL. The item template has no OPT model or PDF page-position callback. |
| `src/case_intelligence/modules/discovery_review/routes.py:56,60,83` | Full-page `page`, full-page `unit` and fragment `unit` each have `le=1_000_000`. OPT has no corresponding parser record-count limit. |

`reader.EMBEDDED_MEDIA_TYPES` permits PDF, JPEG and PNG. TIFF rendering remains later work.
The review’s million-record counterexample is compatible with the inspected 25 MiB default in `pilot_uploads.MAX_FILE_BYTES`; file admission does not make that navigation ceiling safe.
Handoff corrections: SQL purge deletion belongs to `WorkspaceStore.complete_matter_purge`, not feature `purge.py`; packaged SQLite migrations have root `migrations/sqlite/` mirrors.
The handoff’s publication warning is historical: #500’s current body reports a clean outgoing scan. It proves nothing about this future change.

### A3. Root cause

The first review exclusions occur in `load_files.parse` and `reader.read_load_files`.
Beyond them, whole-file reads, tuple storage and first-row-wins projection prevent bounded, deterministic DAT integration.
`source_load_files._Findings.add` loses later diagnostic locations before a durable importer could observe them.
No current review owner stores version-bound OPT records or validates family relationships.
Reusing `routes.py`’s text `unit` for OPT would introduce a second loss boundary: successfully stored records beyond one million would be unreachable.

### A4. Approach

#### Packages and supported seams

**DECISION D2:** Add `discovery_review/production/` with a thin public entry point; this isolates the needed storage/association seam without a workspace refactor.
`__init__.py` exposes import, projection, collision queries and bounded reads; `service.py` coordinates work, `repository.py` owns SQL and `matching.py` resolves candidates.
Follow `identifiers.IdentifierRepository`: borrow `ModuleDatabase.connection` and `.lock`; create no second connection or application authority.
Call `ModuleContext.ensure_capacity` before acquiring database/module locks, as its documented lock order requires.
Feature callers use the package entry point; shared parser/QC remains `core.sources` and cannot import the feature.
No imports of `workbench.py` or `workspace_store.py`, reach-across feature imports or underscore-prefixed attribute access.
Register all four new Python modules, including `production/__init__.py`, as `modules.discovery_review` in `tests/snapshots/import_layers.json`.
Make `test_package_contract_and_isolation` recursive; its current top-level `glob("*.py")` misses nested packages.

#### Lossless parser events and DAT adaptation

**DECISION D17:** Add an optional synchronous public `on_event` callback to the existing parsers; durable ingestion needs every rejected locator.
Events carry kind, logical-record ordinal, starting physical line, recoverable ordered cells/header cells and diagnostic codes.
Emit header, accepted, rejected and file-failure events before legacy diagnostic capping. Multiple findings on one record do not inflate rejected-record counts.
Unrecoverable cells remain absent; an unreadable tail gets an explicit failure, never fabricated records or complete status.
Callback failure aborts the attempt. Preserve iterator shapes, `headers_out`, strict behavior without a diagnostic consumer and capped `findings=` compatibility.
Implement events in `source_load_files.py`; the feature must not duplicate tokenization or access `_records`/`_Findings`.

**DECISION D3:** Keep `LoadFile`’s named fields while allowing iterable rows and explicit counters; tuple-only rows force materialization.
`load_files.parse` gains admitted-path DAT input and callback plumbing while retaining byte-input CSV/TSV calls.
Delegate to `parse_dat`; adapt accepted positional cells to `Row` values and retain physical-line provenance.
One service-owned consumption pass persists events and resolves yielded rows; no second parse or double counting.
Preserve ordered original headers/cells. Duplicate-header display ordinals cannot authorize an ambiguous semantic mapping.
Recognize path, Begin Bates or control-number headers independently of `definition.default_sort`; unmappable rows remain inspectable.
Update `attach`/report callers that assume `len(rows)`. Exhaustion plus successful persistence is required for complete coverage.

**DECISION D4:** Preserve the existing dialect/encoding contract and report unsupported input; guessing conversion can alter evidence metadata.
DAT uses U+0014 delimiters and thorn quoting. Preserve whitespace, padding, embedded delimiters and accepted CR/LF values.
Keep `®` literal, as `test_registered_mark_never_converted` requires; #279’s older newline-marker wording differs.
Preserve UTF-8, UTF-8 BOM, cp1252 and BOM-marked UTF-16 LE/BE support, plus UTF-32/BOM-less UTF-16 rejection and mixed-encoding diagnostics.
OPT remains seven comma-separated strings without general CSV quoting. Keep the parser’s 1 MiB record bound and parent admission limits.
Do not inherit review DAT clipping, two-million-row truncation or whole-file reads; do not invoke materializing `qc_volume` during import.

**DECISION D5:** Bound batches by both 256 records and 4 MiB of decoded payload; row count alone cannot bound wide records.
Count UTF-8 bytes for cells, headers and diagnostics; flush before overflow. Only the current bounded parser event may exist outside the batch.
Persist an event exceeding the byte target alone immediately, with instrumentation proving no second event accompanies it.
Capacity/resource failure leaves an explicit incomplete attempt; batch size never limits total accepted records.

#### Field definitions

Existing JSON aliases already cover Bates, control number, volume, supplied page count and family endpoints; retain them without speculative vendor aliases.
Keep supplied `load_file_pages` distinct from extracted `pages`, and retain “Family begins/ends” keys and labels.
**DECISION D6:** Add validated positional `opt_columns`; OPT has no header row, and its page metadata must stay outside item columns.
Define labels and unique positions 0–6 for Bates, volume, path, document break, both unused supplied positions and page count.
Extend `fields.parse_definition` to require the complete positional contract separately from `Definition.fields`.
Retain rejection of conflicting semantic aliases, unknown groups and computed fields with load-file aliases.
Long or ambiguous DAT headers remain available in raw-record inspection even when ineligible for the existing bounded column chooser.

#### Preserved records, schema and lifecycle

**DECISION D7:** Add three preserved matter-scoped tables; supplied metadata and rejected-record locators are evidence metadata, not merely an index.

| Table | Required contents |
| --- | --- |
| `workbench_discovery_production` | Production/root binding; DAT/OPT source IDs, versions and hashes; parser/definition revision; attempt/generation state; exact coverage counters and file failures. |
| `workbench_discovery_dat_record` | Matter/attempt ownership, event kind, record ordinal, starting line, recoverable ordered headers/cells, parse status and cited diagnostics. |
| `workbench_discovery_opt_page` | Same ownership/locator contract; all seven raw values, including supplied page count, and every rejected-record locator/diagnostic. |

Composite constraints enforce same-matter ownership. Bates is not unique in preservation storage; originals and competing records must survive.
Keep raw records immutable within source versions; associations/families are derived, generation-bound projections.
Use admitted IDs and safe relative provenance, never parser-generated absolute host paths. Supplied suspicious paths remain inert strings.
**DECISION D22:** Store ordered cells/headers as JSON arrays of strings, not dictionaries keyed by supplied headers; `matter_archive_projection.portable` filters dictionary keys such as `path` and `headers`.
Use schema-declared diagnostic fields and portable relative locators; test preservation of arbitrary header text without changing archive filtering.
Retain incomplete attempts distinctly; old records never silently rebind to changed source bytes.

For each new table, complete all four schema steps:
1. Packaged migration under `src/case_intelligence/migrations/sqlite/` and byte-identical root mirror under `migrations/sqlite/`.
2. Filename registration in `WorkspaceStore.__init__`’s hard-coded migration list.
3. Explicit child-before-parent `DELETE` in `WorkspaceStore.complete_matter_purge`.
4. Exact ordered columns in `matter_archive_schema.SCHEMA`, included in preservation rather than `EXCLUDED_TABLES`.

Classify index additions too; slice 2’s derived index remains excluded and rebuildable.
`validate_schema` rejects unclassified tables/columns; verify `exported_columns` and actual archive payloads.
`case_lifecycle_guards.install_content_guards` discovers direct `matter_id` tables; test frozen writers after migration and restart.
No new file cache or purge authority is needed; feature `purge_hook` already clears derived state and guards its reserved render-cache path.
Require a synthetic backup/clean database restore drill and actual archive/verifier exercise; preservation export is not an application restore workflow.

#### Association, cardinality and identifiers

**RULED (Exculpata 8:25 PM ET), D8:** Pair only within unambiguous admitted production-root and intake provenance.
Use receipt/catalog identity and relative paths, not managed-directory adjacency or OPT volume text alone.
DAT may work alone; later unambiguous OPT supplements it. Missing proof or multiple plausible pairs yields `ambiguous_production`.

Resolve DAT candidates by scoped exact path, compatible unique filename, existing produced Bates/control number and Bates-named-file fallback.
Check all supplied keys: contradictory candidates produce `conflicting_match`; nonunique candidates produce `ambiguous_match`.
A path cannot override a contradictory identifier. Team IDs never serve as match keys; comparison normalization never changes supplied display values.

**RULED (Exculpata 8:25 PM ET), D18:** Publish at most one equivalent DAT row class per item version; ordering cannot choose a winner.
Equivalent duplicates require identical projected field presence and exact values, including raw extra fields; all locators remain reachable.
Non-equivalent rows produce `conflicting_dat_rows`; withdraw all their DAT field, family and page associations, including earlier published ones.
Do not merge complementary DAT rows or use latest-wins. Retain `duplicate_begin` diagnostics while distinguishing equivalent repetition.
Compare every competing CSV/TSV row, including legacy extras, using semantic fields and exact raw-header fields.
Absent fields make no assertion; unequal present shared fields produce `conflicting_load_file_metadata` and withhold the whole DAT association.
Compatible values coexist with provenance; retain existing CSV/TSV presentation with a visible DAT conflict when incompatible.
Standalone CSV/TSV clipping and first-row-wins behavior are not repaired here; incomplete legacy candidates cannot certify DAT compatibility.
Stage conflicts in database state and publish atomically; reversed rows, upload order and restart must give identical results.
Re-evaluate affected dependencies only; tentative imported values cannot establish their own matching evidence.

**DECISION D9:** Check imported identifiers and Team ID assignment transactionally; preserving both inputs requires preventing either race order from overwriting the other.
Emit `team_identifier_conflict`, preserve raw metadata and Team IDs, and withhold conflicting DAT/OPT associations.
Reserve supplied literal Bates/control identifiers and OPT labels; never enumerate interior Bates from ranges.
`routes.assign_production_identifiers` currently prechecks displayed endpoints outside `IdentifierRepository.assign`; expose a narrow collision-query seam used inside the assignment/import transaction.
Preserve all-or-none identifier assignment and existing lifecycle fences.

**RULED (Exculpata 8:25 PM ET), D10:** Unmatched DAT rows remain pageable records on the DAT source’s Fields panel, with exact matched/unmatched/rejected/conflicted totals.
They create no source items. Main-list rows remain admitted items; later admitted files trigger affected matching again.

#### OPT groups and Document presentation

**RULED (Exculpata 8:25 PM ET), D11:** Associate an OPT group only with one accepted DAT class through an unambiguous production-scoped Begin Bates match.
Literal `Y` begins a group; blank break fields continue it. Invalid markers, pre-break records, duplicate group keys and ambiguous page keys block association.
A malformed record breaks continuity until the next trustworthy start. Supplied and observed page counts remain separate; mismatches are findings, never repairs.
Resolve image references only to admitted sources within that production; no traversal, URL fetch, implicit import or executable path links.
Document shows selected record’s literal Bates, volume, supplied path, OPT source/version/starting line, record ordinal and supplied page count.
An ordinal is labelled “record”, never Bates. Uniquely admitted JPEG/PNG may render through authorized content delivery.
TIFF, absent images and unsupported formats show metadata with an unavailable-image explanation.
Offer Document for associated records even without an embeddable native; expose unassociated and rejected OPT records on the OPT source’s Document panel.
A native PDF may remain separately available; its iframe position is not synchronized with or certified by OPT ordinals.
No OPT means no per-page Bates label. Do not infer labels from ranges, page counts or consecutive-looking strings.
OPT page labels stay out of Text, Fields and list columns; DAT document-level identifiers retain their existing purpose.

#### Uncapped record navigation

**RULED (Exculpata 8:25 PM ET):** Both review endpoints must reach every stored OPT record using bounded database reads.
**DECISION D20:** Add the same `record` query contract to `production_documents` and `production_document_item`; a generation-bound locator prevents stale or cross-source rebinding.
`record` defaults to empty; otherwise grammar is `^(dat|opt):[0-9a-f]{32}:[1-9][0-9]{0,18}$`, maximum 56 characters.
Components are record kind, immutable attempt ID and source-record ordinal. Validate ordinal ≤9,223,372,036,854,775,807 before binding SQL.
New schema ordinals use that same positive INTEGER domain; this bound addresses every representable stored record, not an arbitrary subset.
`record_dir` defaults to `at` and accepts only `at|next|previous`; direction without a record is invalid.
Malformed/overflow input or incompatible kind/view returns 422. DAT requires Fields; OPT requires Document.
Full-page explicit record navigation requires a valid `doc`; do not fall back to the first listed item when it is missing, foreign or filtered out.
Resolve selected item, attempt, source version and record under the authorized matter; foreign/missing/mismatched associations return 404.
A recognized stale generation returns 409 with reload guidance after ownership checks; never reveal another matter’s attempt state.
Retain authorization, membership recheck, response leases and `Cache-Control: no-store` on both endpoints.
Use indexed predicates on `(matter_id, attempt_id, ordinal)`, plus scoped association keys for matched-item navigation.
OPT selection and predecessor/successor each use an indexed seek with `LIMIT 1`; no `OFFSET`, preceding-record scan or all-record fetch.
DAT record windows use the same cursor domain with at most 100 records and bounded reverse seeks. Diagnostics and duplicate locators use equivalent keyset reads.
Initial views select the first permitted record; links carry actual returned ordinals and attempt identity, including the last record.
All stored OPT records remain reachable through their source panel even when unassociated or rejected; associated navigation cannot cross document groups.
Templates emit ordinary full-page links and existing `data-review-fragment` links. No JS changes or new method/path routes are required.

**DECISION D21:** Correct the same ceiling class in inherited `page`/`unit` validation and test it; leaving another million-position gate would repeat the defect.
Use positive integer validation up to the same representable ordinal domain, rejecting oversized numeric input before expensive work.
Keep `unit` text-only; OPT never consumes it. Retain slice 2’s bounded database list paging, without introducing large-offset scans.
Audit results: 100-row windows, batch bounds and cache eviction limit work, not reachability; no DAT/OPT total-row, family-member, provenance or diagnostic-example cap may hide stored records.
Legacy CSV/TSV clipping/two-million-row behavior and email-header clipping remain known limitations outside DAT/OPT. Existing identifier batch limits reject an operation rather than hide records.
The chooser’s header/column bounds must not limit raw DAT inspection. Record-size/admission failures remain explicit coverage failures, not successful truncated imports.

#### Families and the stored index

**RULED (Exculpata 8:25 PM ET), D12:** Support conservative two-level families declared by identical attachment endpoint pairs; those fields do not prove arbitrary ancestry.
Require both endpoints, a unique declaring parent whose Begin Bates equals Family begins, and a unique declaring terminal member whose End Bates equals Family ends.
Require valid member ranges within those endpoints, using public `source_load_file_qc.parse_plain_label` for supported comparisons.
Parent leads; children sort naturally by Begin Bates with stable source/record ties. Input order does not establish parenthood.
Never include undeclaring rows merely because their numbers fall inside a range.
Missing parent/terminal produces `orphan_attachment`; incomplete, reversed, unsupported or incomparable bounds produce `invalid_family_range`.
Nonidentical overlaps, competing parents and nesting produce `family_overlap`/`family_conflict`; cyclic candidate links produce `family_cycle`.
Validate iteratively over actual records using database-backed work state; no integer-span expansion, recursive untrusted traversal or whole-family Python collection.
Invalid/conflicted records remain visible and ungrouped with original fields and cited explanations such as “Family parent was not found”.
Add public record-oriented QC helpers; retain existing duplicate/range/count/path findings without walking managed storage through `qc_volume`.
The indexer consumes accepted DAT projections and stores production association, family root/parent/member-order keys and indexed matching/filter values.
DAT/OPT, candidate files, definitions and Team IDs invalidate affected associations/components; coherent publication rejects stale source/version/revision work.
**DECISION D13:** Sort groups by the parent’s requested sort value and keep children parent-first within bounded 100-item pages; unlimited family expansion would defeat slice 2.
Filtered child matches get marked parent context inside a cell/header, excluded from match totals and identifier targets.
Cross-page families get bounded continuation context; retain existing keyboard row adjacency.
Show “Indexed N of M” plus separate exact DAT/OPT coverage/conflict totals; stream database cursors instead of building production-sized maps.

### A5. Vision/AGENTS check

- **No modes:** Extend existing Production documents, Document and Fields surfaces.
- **No single-use features:** Reusable production/page-record contracts support later LFP/DII and rendering slices.
- **No dropped/capped computed numbers:** Exact counts and all stored records/locators remain reachable; limits govern work units or explicit failed coverage.
- **No filename allowlisting:** Recognize formats and admitted provenance, with no privileged producer filenames or scanner exceptions.
- **No weakened egress/privacy/safety:** Local-only processing preserves malware admission, source digests, capacity, authorization, response leases and lifecycle fences.
- **Neutral language/synthetic data:** Use production, document, parent, attachment and team identifier; generated fixtures contain no real people or case material.
- **Evidence/judgment:** Originals and human identifiers remain separate from derived associations; uncertain families stay uncertain.
- **Working practice:** Write cheap failing tests first, record the actual failing parent commit and verify HTTP/browser artifacts. No new judgment rule or AGENTS change is needed.

### A6. Blast radius

| Area | Coverage |
| --- | --- |
| Callers/checks | Preserve parser/CSV compatibility; recursive boundaries, four import-layer rows, DB-access/source-size ratchets and unchanged rename/publication checks. |
| Data | Versioned attempts, immutable raw records, complete rejected locators, atomic conflict publication; interrupt/restart/change/removal tests. |
| Docs | Review/QC guides explain unmatched rows, conflicts, cursor navigation, dialect limits and lack of PDF correspondence. |
| Archives/purge | Exact schema coverage, arbitrary supplied header preservation, real archive verification, changed-original rejection, three explicit deletions, frozen/late-writer tests and clean restore. |
| Performance | Full pipeline memory profiles, wide batches, huge families, million-record cursor test and parent list-navigation benchmark. |
| Security | Bound numeric parsing and SQL reads; reject unsafe paths, cross-matter cursors, stale associations and HTML execution; retain lease/access checks. |

### A7. Proving tests

Names below are proposed implementation contracts, not executed tests. **F** means write the cheap failure before implementation against the actual stacked parent.
Use public repositories for reads/setup; bounded raw fixture inserts use `tests.fixture_write.fixture_write(workspace)` and explicit named handles.

| File and test | Should pass / must still be caught |
| --- | --- |
| `tests/test_source_load_files.py::test_record_events_preserve_all_rejected_locators` **F** | Every start among >1,000 malformed multiline records plus adjacent accepted records / capped callbacks, invented cells or physical-line/record confusion. |
| Same: `test_event_callback_compatibility_and_failure` | Legacy strict/findings behavior and complete OPT rejection events / callback failure marked complete or duplicate events. |
| `tests/test_discovery_review_dat_opt.py::test_dat_loadfile_shape_preserves_cells_and_lines` **F** | Family columns and long multiline values unchanged / clipping, default-sort dependence or ambiguous semantic headers projected. |
| Same: `test_dat_adapter_encoding_variants` | cp1252, UTF-8/BOM, UTF-16 LE/BE, CRLF and literal registered marks / unsupported encoding, mixed damage, inner-thorn ambiguity or oversized records silently accepted. |
| Same: `test_scoped_matching_by_path_bates_and_control` **F** | Unique admitted candidates / contradictory keys, cross-production repetition, Team ID matching or unsafe paths. |
| Same: `test_dat_projection_cardinality_is_order_independent` **F** | One equivalent class and every duplicate locator / conflicting rows select a winner in either order or after restart. |
| Same: `test_dat_csv_conflicts_are_order_independent` **F** | Compatible shared/DAT-only fields / conflicting legacy extras or incomplete candidates certify compatibility. |
| Same: `test_unmatched_dat_rows_remain_visible_without_sources` **F** | Pageable raw records and later matching / phantom sources or missing rejected/unmatched rows. |
| Same: `test_opt_page_records_are_exact_and_document_only` **F** | Nonconsecutive literal labels with provenance / guessed labels, inferred PDF mapping or labels in Text/Fields/list. |
| Same: `test_no_opt_has_no_page_bates`; `test_opt_bad_breaks_and_unmatched_pages_are_reported` | Existing native viewing and inspectable OPT-only records / fabricated labels, invalid breaks or silent orphan loss. |
| Same: `test_opt_navigation_reaches_tail_on_both_endpoints` **F** | Literal record after ordinal 1,000,000, including previous/next / old `unit` cap, clamping, skipped tail or wrong provenance. |
| Same: `test_record_cursor_validation_and_matter_isolation` **F** | Valid owned cursors / malformed, overflow, wrong source/group, foreign matter, revoked access and stale generation. |
| Same: `test_other_position_queries_have_no_million_ceiling` **F** | Valid `page`/text `unit` above one million / retained arbitrary cap or acceptance of invalid/out-of-domain values. |
| `tests/test_source_load_file_qc.py::test_family_parent_order_and_invalid_relationships` **F** | Shuffled parent/children become stable / orphans, broken bounds, overlaps, competing parents, nesting and cycles. |
| Same: `test_family_ranges_never_expand_numeric_spans` | Large endpoints evaluated over actual rows / numeric expansion, recursion or hidden invalid members. |
| `tests/test_discovery_review_dat_opt.py::test_family_index_paging_filter_and_invalidation` **F** | Sort/filter/page context and coherent rebuild / double counts, context identifier assignment or stale families/pages. |
| Same: `test_team_identifier_import_race_preserves_both_inputs` **F** | Distinct values coexist / collisions in either race order, partial assignment or overwritten identifiers. |
| `tests/test_discovery_review_dat_opt_storage.py::test_all_rejected_locators_survive_restart` **F** | All 2,501 malformed multiline starts and exact totals survive / indistinguishable tails after 1,000 examples. |
| Same: `test_streamed_import_memory_and_complete_counts`; `test_batch_limits_include_wide_records` **F** | D5/D19 bounds through publication / materialization, count-only batches or omitted tail records. |
| Same: `test_memory_contract_detects_materializing_controls` | Streaming control passes / fixed materializing and count-only negative controls escape detection. |
| Same: `test_restart_version_change_and_failed_attempt_visibility`; `test_production_schema_mirror_and_restore` **F** | Idempotent recovery, identical mirrors, clean restore / stale publication, duplicate active attempts or missing fences. |
| Same: `test_purge_and_frozen_writers_cover_production_records` **F** | Authorized purge/retry removes all rows / late writes and residual metadata. |
| `tests/test_matter_archive.py::test_dat_opt_metadata_is_preserved_and_scoped` **F** | Raw cells, arbitrary headers, seven OPT values and every rejected locator preserved / filtering loss, cross-matter export or changed originals. |
| `tests/test_discovery_review_list.py::test_malformed_definition_is_rejected_whole` | Complete positional definition / duplicate/missing OPT positions, alias conflicts and invalid computed-field sources. |
| `tests/test_discovery_review_boundaries.py::test_package_contract_and_isolation` | Recursive four-file registration / nested prohibited imports or private attribute access. |

**DECISION D20, regression execution:** Normal CI creates 1,000,002 synthetic OPT records directly in bounded database batches, not through a 19 MB parse.
Use a completed-generation fixture with synthetic provenance, not a claim of parser or original-digest validation; public repository reads verify setup.
Give ordinal 1,000,002 the literal supplied label `TAIL®-X0007`, a distinct volume/path and exact synthetic source/version/line.
Through both endpoints, navigate 1,000,000 → 1,000,001 → 1,000,002 and back, assert exact displayed label/provenance and final absence of Next.
Exercise associated and unassociated source-panel paths; inspect bounded reads/query plans to reject full scans, offsets and all-record fetches.
All cursor validation and million-record navigation checks run in normal CI; larger stress repetitions are optional and cannot substitute for this regression.

**RULED (Exculpata 8:25 PM ET), D19:** Retain numeric full-pipeline memory acceptance, not parser-only iteration.
**SCOPE CHANGE 2026-11-14 (approval_ref 2026-11-14T20:14, see A13):** the three 5,000/20,000-record profiles (clean, malformed, family) and their negative controls leave this slice and move to their own performance design note. The wide 20/80-record profile, its count-only control and every other D19 rule stay in this slice.
Generate inputs incrementally and initialize source populations before measurement; warm code using a separate tiny matter.
Measure import opening, parsing, persistence, matching, QC, family validation, index publication and first-page read, with no populated import cache.
Use fresh subprocesses, `tracemalloc` and sampled process RSS; run clean, malformed-multiline and family-heavy 5,000/20,000-record profiles with approximately 512-byte cells.
Include one family containing most rows and 20/80-record profiles with 256 KiB cells, including multibyte text, within unchanged admission limits.
Require positive peaks, larger Python peak ≤1.5× smaller, each Python peak ≤64 MiB, incremental RSS ≤128 MiB and retained Python growth after collection ≤4 MiB.
Instrument batches and matching/QC/publication queues; prove dual limits, flush-before-overflow and immediate oversized singleton persistence.
Assert exact accepted/rejected/unmatched/conflict totals, tail values and all 2,501 rejected starts with bounded reads.
Fixed test-local controls retaining rows/families/diagnostics or batching wide rows by count alone must fail; do not relax thresholds to accommodate them.
These are acceptance limits, not measured results. Keep expected-data construction outside measurement without excluding importer stages.
Retain existing parser exact-value, path, registered-mark and `test_parse_memory_is_flat` tests, plus QC range/family/no-gap and diagnostic-cap compatibility tests.
Capture synthetic browser evidence for tail navigation, no OPT, unavailable TIFF, family continuation, conflicts and script-disabled links; a PDF HTTP response does not prove visual correspondence.

### A8. Fence

**DECISION D14:** Use this exact provisional fence, resolving parent index ownership and migration allocation before coding; absent parent code cannot justify guessed paths.
Paths below are repository-relative. No wildcard grants are intended.

**Modified, 22 files:**

```text
src/case_intelligence/modules/discovery_review/load_files.py
src/case_intelligence/modules/discovery_review/reader.py
src/case_intelligence/modules/discovery_review/fields.py
src/case_intelligence/modules/discovery_review/presentation.py
src/case_intelligence/modules/discovery_review/routes.py
src/case_intelligence/modules/discovery_review/identifiers.py
src/case_intelligence/config/discovery_review_fields.json
src/case_intelligence/source_load_files.py
src/case_intelligence/source_load_file_qc.py
src/case_intelligence/workspace_store.py
src/case_intelligence/matter_archive_schema.py
src/case_intelligence/templates/workbench_production_documents.html
src/case_intelligence/templates/workbench_production_document_item.html
src/case_intelligence/static/discovery-review.css
tests/snapshots/import_layers.json
tests/test_discovery_review_list.py
tests/test_discovery_review_boundaries.py
tests/test_source_load_files.py
tests/test_source_load_file_qc.py
tests/test_matter_archive.py
docs/DISCOVERY_REVIEW.md
docs/LOAD_FILES.md
```

**New, 9 files:**

```text
src/case_intelligence/modules/discovery_review/production/__init__.py
src/case_intelligence/modules/discovery_review/production/service.py
src/case_intelligence/modules/discovery_review/production/repository.py
src/case_intelligence/modules/discovery_review/production/matching.py
src/case_intelligence/migrations/sqlite/0049_discovery_dat_opt.sql
migrations/sqlite/0049_discovery_dat_opt.sql
tests/test_discovery_review_dat_opt.py
tests/test_discovery_review_dat_opt_storage.py
specs/item114-s3-dat-opt.md
```

If slice 2 moved indexing, replace `reader.py` with its actual owning path in the accepted kickoff before editing; explicitly list any necessary additional caller.
Limit `workspace_store.py` to migration registration and three purge deletions; preserve its source-size ratchet. Parser changes are D17 compatibility-preserving events.
Must **not** touch `pilot_uploads.py`, `workbench.py`, module contracts/registry, feature `purge.py`, `static/discovery-review.js` or `matter_archive_projection.py`.
Also exclude intake/signature/malware limits, authentication/authorization, network/egress, lifecycle authority, render/cache code, LFP/DII, OCR, Office conversion, citations and gap engines.
Exclude existing migrations, checker exceptions/implementations, CI workflows, import-edge exceptions, DB-access/source-size baselines, frozen fixtures and corpus expectations.
Route/store snapshots are outside this query-only change. A method/path change requires an explicit fence amendment and reviewed regeneration.
**DECISION D15:** Single PR despite an expected diff above 500 lines; schema, archive/purge, conflicts and visible record access form one acceptance boundary.
Use ordered parser/storage/QC and index/presentation commits with their tests; do not present the importer as complete before its UI lands. The removed 500-line rule is not a waiver of focused review.

### A9. Stacking

**DECISION D16:** Build on `codex/discovery-intake-load-files-114-s3a` as `codex/discovery-dat-opt-114-s3`; these remain proposed branch names.
Assume only s1’s stated code list/events/Coding fields/digit keys/archive inclusion, s2’s stored-index contract and s3a’s DAT/OPT admission.
D17 parser events are work in this slice, not an assumed parent capability.
At build time re-verify migration numbering, index ownership, source revisions/callbacks, admitted states, production-root provenance, bounded list queries and identifier transactions.
Preserve Coding fields, saved views and parent archives; rerun parent performance, navigation, admission-rejection and lifecycle tests.
If provenance is insufficient, retain visible unassociated records; do not invent root bindings or weaken D8.
Historical sandbox failures do not establish that a new failure is harmless.

### A10. Decisions log

The recommendations below collect every design decision; ruled entries are settled.

- **RULED D1/D10:** Narrow review scope; unmatched records visible without sources; TEXT/citations deferred.
- **DECISION D2:** Thin production package using borrowed database authority, because ownership must remain explicit.
- **DECISION D3:** Iterable review rows and explicit counters, because tuples force materialization.
- **DECISION D4:** Existing dialect/encoding limits with visible failures, because guessed conversion changes supplied values.
- **DECISION D5:** Dual 256-record/4-MiB batches with singleton handling, because wide records defeat count-only limits.
- **DECISION D6:** Separate validated positional OPT definition, because OPT has no headers and page labels are Document-only.
- **DECISION D7:** Three preserved metadata tables, because raw records and rejection locators are durable evidence metadata.
- **RULED D8:** Provenance-bound pairing; no adjacency or Bates-only production inference.
- **DECISION D9:** Transactional collision checks preserving both inputs, because import/assignment races cannot choose ownership.
- **RULED D11/D12:** Conservative OPT association and two-level declared families; uncertain relationships remain inspectable.
- **DECISION D13:** Parent-based group sorting and bounded continuation context, because large families cannot expand pages.
- **DECISION D14:** Exact provisional fence reconciled against landed parents, because their implementation is absent here.
- **DECISION D15/D16:** One PR on the stated parent/branch, because the visible and durable acceptance contracts belong together.
- **DECISION D17:** Lossless synchronous parser events, because existing findings omit later exact locations.
- **RULED D18/D19:** Order-independent cardinality/conflicts and full-pipeline numeric memory limits. **Amended 2026-11-14 (A13):** the 5,000/20,000-record profiles move out of s3.
- **DECISION D20:** Version-bound record cursor and mandatory million-record CI proof, because `unit` cannot safely serve OPT.
- **DECISION D21:** Remove equivalent position ceilings and audit every new pagination path, because changing only OPT leaves the same defect class.
- **DECISION D22:** Ordered JSON cell/header arrays, because supplied header strings must not become archive policy keys.
- All **RULED** entries above mean **RULED (Exculpata 8:25 PM ET)**. No remaining maintainer question is required by the supplied rulings.

### A11. Build command hints

This design revision wrote no files and ran no application tests.
**RULED (Exculpata 8:25 PM ET):** Builders place every scratch, subprocess and browser artifact inside their own worktree.

```bash
mkdir -p .scratch/tmp
export TMPDIR="$PWD/.scratch/tmp"
.venv/bin/python scripts/publication-check.py
.venv/bin/python -m ruff check --no-cache src services scripts tests
.venv/bin/python scripts/rename_contract_check.py
git diff --check
git diff --name-status "$S3_BASE"...HEAD
git diff --name-status
git diff --cached --name-status
git ls-files --others --exclude-standard
```

Set `S3_BASE` to the inspected stacked parent; compare all outputs with A8. `Makefile` has no standalone fence command.
The JS policy rejects `+`, `+=`, `${}` interpolation, `.join()`, `.concat()` and computed member calls; retain it unchanged.
Import layers and route snapshots are pytest checks here; run them before the broader behavior suite:

```bash
.venv/bin/python -m pytest -q tests/test_import_boundaries.py tests/test_discovery_review_boundaries.py tests/test_db_access_ratchet.py tests/test_source_size_ratchet.py
.venv/bin/python -m pytest -q tests/test_case_lifecycle_fences.py tests/test_api_snapshots.py
.venv/bin/python -m pytest -q tests/test_fixture_write_policy.py tests/test_fixture_inventory.py tests/test_fixture_inventory_coverage.py tests/test_fixture_reflective_deny.py
.venv/bin/python -m pytest -q tests/test_source_load_files.py tests/test_source_load_file_qc.py tests/test_discovery_review_list.py tests/test_discovery_review_mount.py tests/test_discovery_review_dat_opt.py tests/test_discovery_review_dat_opt_storage.py tests/test_matter_archive.py
```

`tests/test_api_snapshots.py` records route methods/paths, not query validation; this proposal needs no regeneration.
If routes change after a fence amendment, run `.venv/bin/python -m tests.test_api_snapshots --update routes` and inspect both configurations.
Run `make check-fast`, separate applicable browser validation, the synthetic backup/restore drill and `make publication-candidate-check` before pushing.
Keep browser output under `.scratch/`; preserve the installed external publication guard and separately inspect public PR text.
Missing prerequisites, skipped suites and failures are not passes. Schema/export work is within expected hosted-review scope; six green required checks, resolved conversations and maintainer merge remain the gate.

### A12. Revision log

- **R1-P1, lost rejected locators:** Reverified `_Findings.add` and parser yield behavior; retained D17, durable rejected events, >1,000-location restart/archive tests in A4/A7.
- **R1-P2a, competing DAT rows:** Reverified `attach`/`reader.build`; retained D18 equivalence, whole-association withdrawal, CSV-extra comparison and reversed-order/restart proofs in A4/A7.
- **R1-P2b, inadequate memory proof:** Reverified parser-only `test_parse_memory_is_flat` and materializing `qc_volume`; retained every D19 profile, numeric bound and negative control in A7.
- **R2-P2, unreachable OPT tail:** Reverified both `unit` caps; replaced the proposed reuse with D20’s scoped cursor, bounded seeks, both-endpoint validation and normal-CI 1,000,002-record regression in A4/A7.
- **Ruling 1:** D1/D10 are settled in the summary, A1 and A4; removed issue-author reconciliation.
- **Ruling 2:** D8/D11/D12/D18/D19 are marked ruled and retained in A4/A7/A10.
- **Ruling 3:** D15 chooses one PR with reasons; branch, fence and tests appear in the summary and A7–A9.
- **Rulings 4–5:** D20 specifies the exact navigation contract; D21 records the cap audit and related validation repair in A4/A7.
- **Structure/scratch/REFS rulings:** One-screen decision summary, substantive appendix, worktree-local artifacts and the REFS line replace the former layout. **REFS update (Exculpata 9:01 PM ET):** the item 114 issue now exists as #503; the TBD line is replaced by `REFS: #503 (https://github.com/Exculpata/exculpata/issues/503)`.
- **Additional verification:** D22 makes raw-header storage compatible with inspected archive projection without weakening that guard. No review finding was rejected.

### A13. Scope change, 2026-11-14

Date: 2026-11-14 (ET, after 8:14 PM). approval_ref: 2026-11-14T20:14 (APPROVALS.md; approved under the standing grant of 2026-11-12T22:23, non-hard-gate). This is a written scope change, not a retry and not an exception.

**Removed from this slice (6 test cases, 2 test functions):**
- `test_streamed_import_memory_and_complete_counts[clean-5000-20000-512]`, `[malformed-5000-20000-512]`, `[family-5000-20000-512]`.
- `test_memory_contract_detects_materializing_controls[clean-5000-20000-512-rows]`, `[malformed-5000-20000-512-diagnostics]`, `[family-5000-20000-512-families]`.

**Kept in this slice:** `[wide-20-80-262144]` of the streamed-import test, `[wide-20-80-262144-count_only]` of the materializing-controls test, `test_batch_limits_include_wide_records`, `test_all_rejected_locators_survive_restart`, the million-record OPT navigation regression (D20), every other test in the A7 table, and every numeric bound in D19 (1.5x ratio, 64 MiB Python peak, 128 MiB incremental RSS, 4 MiB retained growth). No bound, assertion, subprocess deadline or pytest timeout is changed. Nothing is skipped or marked as an expected failure.

**Reason (measured, 114 s3 diagnose-and-fix item, 2026-11-14, single-process runs without a pytest timer):** import plus first index publication costs about 90 ms per record (250 records 23.2 s, 500 records 44.2 s, 1,000 records 86.9 s, 2,000 records 181.9 s; linear, no quadratic path). One 5,000-record pipeline therefore takes about 450 s and one 20,000-record pipeline about 1,800 s, against the repository's 360 s per-test limit. The streamed-import test needs both sizes in one test, the materializing-controls test needs four pipelines. A single 5,000-record pipeline already exceeds the limit, so the 5,000 profiles cannot pass either; they leave together with the 20,000 profiles. The budget implied by the test is about 7.2 ms per record; publication alone measures about 14 ms per item (about 28 ms with tracemalloc). Closing that gap needs a rework of the production service and of stored-index publication, which is outside this slice's fence.

**What this does not claim:** this slice no longer proves bounded memory at 5,000 and 20,000 records through the real pipeline. That proof is owed. It is carried by the performance design note `specs/load-file-publication-perf-design.md` (own item, own exact-SHA merge card), which restores the three profiles and their controls unchanged once publication meets the budget. Until that item merges, the memory bound for large record counts is not proved on main.

**Lifecycle failure (separate):** the intermittent `assert 409 == 200` in `tests/test_discovery_review_index_lifecycle.py::test_single_heavily_coded_item_stays_pending_with_complete_values_on_resume` is not part of this scope change. It has its own design note, `specs/lifecycle-409-design.md`. This slice must pass the test without a code or test change made to avoid the 409.
