## Decision summary

**Item 114, slice 3a: protected, stored-only DAT/OPT admission**  
DRAFT r4, author: Codex astra high, for review

Goal: admit supported DAT/OPT text load files through existing protected intake, preserving original bytes.  
Outcome: reviewers can upload files or browser-selected folders, see truthful saved-only status, and receive useful rejection messages.

- **RULED (Exculpata 8:25 PM ET):** Stored-only admission and the necessary `workspace_store.py` / `workbench.py` readiness changes are accepted.
- **RULED (Exculpata 8:25 PM ET):** The minimum-two-column DAT subset is accepted with explicit columns, delimiters, encodings and matching intake/rejection wording; see A4.
- **RULED (Exculpata 8:25 PM ET):** Administrator wording stays neutral and policy-aware; exact proposed text appears in A4.
- **RULED (Exculpata 8:25 PM ET):** Place the PostgreSQL regression in `tests/test_review_bench_v2_postgres.py`, already selected by Makefile and CI.
- **RULED (Exculpata 8:25 PM ET):** Attribute the separate Codex high review to an Exculpata project decision, not `QUALITY_GATES.md`.
- **RULED (Exculpata 8:25 PM ET):** Parent: `codex/discovery-review-stored-index-114-s2`; branch: `codex/discovery-intake-load-files-114-s3a`; migration: **none**.
- **RULED (Exculpata 8:25 PM ET):** Diff size is not a blocker; all builder scratch and browser output stays inside its worktree.
- **RULED (Exculpata 9:01 PM ET):** Persist a short actionable rejection reason through the unchanged 240-character bound; display the complete explanation in browser errors and receipts. D12 specifies a tested construction ceiling of 222 characters.
- Stack: `s1 -> s2 -> s3a -> s3 -> 4a -> s4 -> 5a -> 5b`.
- PR split: **single intake PR**, separate from slice 3; admission, serving denial and truthful readiness stay together.
- Fence: **4 new, 13 modified files**, unchanged; admission package, upload owner, readiness owners, intake/admin UI, documentation, tests and import inventory.
- Headline proofs: baseline DAT rejection; both upload paths; malicious/oversized inputs; scanner dispatch; GET/HEAD/Range denial; terminal rejection/reopen/full explanation; completion/restart coverage; real PostgreSQL zero-unit projection.
- Known limits: configured document size limit; bounded grammar/encoding subset; no metadata import, previews, registered-folder expansion or ZIP intake.
- Open: no unresolved maintainer question; unruled implementation recommendations D1–D8, D10 and D12 remain reviewable in A10.

REFS: #503 (https://github.com/Exculpata/exculpata/issues/503)

## Appendix

### A1. Kickoff

- **Goal:** Extend the shared upload boundary to admit structurally valid DAT/OPT load files without treating their contents as searchable evidence.
- **User-visible outcome:** Accepted files remain saved byte-for-byte, visibly excluded from searchable coverage; rejected files receive an actionable reason.
- **Expected files:** The exact A8 fence covers admission, protected content access, readiness, selectors/messages, documentation and synthetic regressions.
- **Acceptance check:** Supported files pass whole-file validation and configured scanning, survive restart unchanged, remain unservable, and never inflate extracted/searchable counts. Structural rejection becomes terminal, preserves its short reason after reopen, removes incoming bytes and displays the complete explanation.
- **Issue link:** [#503](https://github.com/Exculpata/exculpata/issues/503). Related: [#279](https://github.com/Exculpata/exculpata/issues/279), parent [PR #500](https://github.com/Exculpata/exculpata/pull/500).

Inspected baseline: `e6644a225f75c2cedbf3e03a17bd0205e6a006e3`.
This note was prepared read-only. No implementation, behavioral test, browser run or PostgreSQL execution is claimed.
Before coding, record the kickoff in the PR body or a task file in the branch’s first commit.
Read current related issue/PR comments, assignees, claims and holds; reconcile scope before implementation.

### A2. Problem and verified current behavior

Paths below are relative to the repository root; line references describe the inspected baseline.

| Boundary | Current behavior and evidence |
| --- | --- |
| Format selection | `src/case_intelligence/pilot_uploads.py:69–141`: `DOCUMENT_MEDIA_TYPES`, `_ALLOWED`, MIME aliases and `EXTENDED_SOURCE_SUFFIXES` omit DAT/OPT. |
| Filename rejection | `PilotStore.validate_name`, line 1473: NFC normalization, filename safety checks, then case-folded suffix lookup. DAT/OPT fail before byte inspection. |
| Browser-folder paths | `validate_relative_upload_path`, line 1500: relative-component checks followed by the same leaf-name validator. |
| Declared MIME | `_store_stream_locked`, line 1574, and `finalize_resumable_upload`, line 1819: compare normalized supplied MIME with suffix-specific aliases. This is not content sniffing. |
| Signature checks | `_validate_file_signature`, line 1531: selected PDF, Office and image signatures; TXT/CSV/TSV reject UTF-16/32 BOMs. No general text-class admission check exists here. |
| Extraction fallback | `_extract`, line 2070, reaches UTF-8 text handling at line 2291: NUL, decoding and text-resource checks occur during extraction, potentially after deferred storage. |
| Malware gate | `_scan_staged`, line 1028: both fresh-upload paths scan before signature checking and final publication. Existing duplicate/recovery returns precede scanning (`pilot_uploads.py:1640–1655,1882–1924`). |
| Discovery integration | `modules/discovery_review/load_files.py:21,69`, `DELIMITERS` / `parse`, support CSV/TSV only; `reader.py:182–194`, `read_load_files`, requires uploaded, ready sources no larger than 64 MiB. |
| Preview versus serving | `reader.py:26` embeds only PDF/JPEG/PNG, but `workbench.py:13631`, `source_content`, independently serves stored originals through GET/HEAD/Range with inline disposition. |
| Readiness | `workbench.py:2120`, `_source_state`, gives ready documents a ready tone; `workspace_store.py:5159–5170,5209`, `matter_readiness`, counts successful ingestion as extraction and ready tone as searchable. |
| Coverage messaging | `workbench.py:313`, `_source_coverage`, derives excluded counts from readiness; `matter_readiness_projection`, line 6901, may recommend retrying sources when none are searchable. |
| Administrator status | `templates/workbench_admin.html:130` names images, email and spreadsheets, then assumes extended-policy availability for other types. It omits load files and needs policy-aware wording. |
| Durable rejection | `workspace_store.py:6266–6274` validates the reason against 240 characters before setting `failed`; `_safe_text`, lines 403–415, rejects excess length. `workbench.py:12109–12127` attempts incoming cleanup, passes `str(exc)` unchanged to the ledger and swallows persistence errors. A long explanation can therefore leave a nonterminal item after cleanup; `intake_receipts.py:75–78` projects that item as `uploading`. |

Upload defaults at `pilot_uploads.py:31–50`: ten direct files, 25 MiB per document, 100 MiB direct aggregate, 101 MiB raw multipart cap.
Resumable defaults include 2,000 items per session, 10,000 per selection, 5 GiB per session and 2 MiB chunks.
`maximum_file_bytes` applies configured document or recording limits; this slice retains document limits (`pilot_uploads.py:164,986`).

`PilotStore.__init__` defaults to `extended`: `_scan_required` selects extended suffixes; `all` selects every suffix; `disabled` selects none (`pilot_uploads.py:891–901,1023–1026`).
Missing/inconclusive scanning raises 503; detected malware is quarantined and raises 422; quarantine failure raises 503 (`pilot_uploads.py:1028–1058`).
Returning an already admitted occurrence is not a fresh scan.

`source_load_files.parse_dat` at line 189 streams U+0014-delimited records with optional thorn quoting.
`parse_opt` at line 214 splits each record into exactly seven comma-separated fields.
`_encoding` supports UTF-8, BOM-marked UTF-16 and sample-selected CP1252; `_records` bounds records to one MiB (`source_load_files.py:12,84–99,133–167`).
These are text load files that may arrive in legacy encodings and large productions; their suffix alone proves neither structure nor safety.

### A3. Root cause

The first rejection is `PilotStore.validate_name`: suffix-based dispatch has no DAT/OPT admission contract.
Adding suffixes alone would route canonical text files into `_extract`’s generic text fallback and leave `source_content` serving their bytes.
A successful zero-unit completion would also encounter `WorkspaceStore.matter_readiness`’s extraction/searchability assumptions.
The fix belongs at shared byte admission, protected serving and readiness ownership; changing labels alone cannot repair the aggregate.
Rejection also needs separate durable and display representations: the complete support explanation exceeds the existing ledger bound, and the current finalization handler does not distinguish them (`workspace_store.py:6266–6268`; `workbench.py:12117–12127`).

### A4. Approach and exact supported contract

**DECISION D1:** Add `case_intelligence/source_admission/` with a thin public `__init__.py` and `load_files.py` implementation; reason: upload paths and readiness owners need one shared classification contract.
Expose validation, a safe exception, format constants, supported-format wording and a public classification predicate.
Use public `parse_dat`, `parse_opt` and `LoadFileError`; callers import through the package entry point.
Do not import `workbench.py`, `workspace_store.py` or discovery internals into this package, or access another module’s underscore-prefixed attributes.
`modules/discovery_review/identifiers.py:35–57`, `IdentifierRepository`, demonstrates the narrow-owner pattern with `ModuleDatabase`; admission needs no database repository or module registration.

**DECISION D2:** Exhaust existing parsers without a findings accumulator and enforce the bounded admission subset below; reason: reuse the importer grammar without creating a competing parser.
**RULED (Exculpata 8:25 PM ET):** The minimum-two-column DAT restriction is accepted when disclosed precisely; it is an admission restriction, not an existing `parse_dat` restriction.

- DAT: at least two nonempty, distinct header cells and at least one data record; every data record has the header’s width. Header names are arbitrary, not a fixed Bates/path schema.
- DAT delimiter: U+0014; fields may be unquoted or enclosed by one matching pair of U+00FE thorn characters, following `parse_dat`. Do not add CSV-style escaping.
- OPT: no header; every record has exactly seven comma-separated fields; require at least one record containing a non-whitespace value. No quoted-comma escaping is interpreted.
- Encoding: UTF-8 with optional BOM; UTF-16 LE/BE with BOM; CP1252 fallback selected only when the first 64 KiB is not valid UTF-8. Reject UTF-32 and any later decoding diagnostic.
- Reject recognizable MZ/ELF/ZIP signatures, NUL and unsupported control characters; permit ordinary tab/CR/LF whitespace and DAT’s delimiter convention.
- Reject malformed DAT quoting, inconsistent widths, empty/header-only input and oversized records, including failures after an otherwise valid prefix.
- Validate the service-owned regular staged file under the existing mutation boundary; verify identity/size stability around validation. Never follow embedded record paths.
- Retain bounded header/current-record state, not a row or findings collection. Translate diagnostics into safe reasons/line numbers without staging paths or source values.

The following exact support text appears on intake and accompanies structural/encoding rejection messages in the UI:

> DAT support requires at least two nonempty, distinct column headers and one data record, U+0014 field separators, optional thorn (þ) quoting, and the same number of fields in every record. OPT support requires no header, exactly seven comma-separated fields per record, and at least one nonblank record; quoted commas are not supported. Supported encodings are UTF-8 with an optional BOM, UTF-16 LE or BE with a BOM, and Windows-1252 detected from the first 64 KiB; later decoding errors are rejected. Each record must fit the 1 MiB record limit.

Exact additional intake sentence:

> DAT and OPT files are saved without a preview or searchable text; metadata import is not available yet.

Exact structural-rejection display prefix, followed by the complete support text and an actionable diagnostic/location:

> This load file was not added because its contents do not match the supported DAT/OPT text format.

**RULED (Exculpata 9:01 PM ET):** Keep a short durable reason below 240 characters and pass it unchanged through the existing `fail_upload_item` bound. Render the complete prefix, support paragraph and diagnostic in browser errors and receipt display. Do not raise the cap, attempt an overlong write and recover by truncation, or derive either representation by cutting the other.

**DECISION D12:** Build the durable reason in the new `source_admission/load_files.py` safe exception using exactly `DAT/OPT format rejected: {diagnostic} Location: {location}.`; reason: a recognizable, bounded, actionable representation preserves the existing ledger and allows complete display after reopen.

- `diagnostic` comes from a closed mapping of parser/wrapper failure kinds to complete, actionable ASCII sentences of at most 160 characters, never raw exception text, paths or source values. `location` is `line N`, with a positive decimal line number of at most 20 digits, or `file` for file-level failures. The construction ceiling is **222 characters**; test every mapped diagnostic and the longest permitted location against that bound and exact store round-trip equality. This is a required test, not a claimed execution.
- Example: `DAT/OPT format rejected: Make every DAT data record match the header field count. Location: line 3.` Build the full display message separately from the fixed prefix, complete support paragraph, and the same diagnostic/location fields.
- In `pilot_uploads.py`, translate the safe exception into a load-file-specific `UploadProblem` subtype carrying `durable_reason`, with `str(exc)` containing the full display message. In `workbench.finalize_uploaded_source`, pass that subtype’s short reason to `fail_upload_item`; retain the full JSON `message`. Direct `upload_sources` retains the full text response. These are the existing error boundaries at `pilot_uploads.py:144–149` and `workbench.py:12109–12127,12230–12233`.
- Expose a public display helper through `source_admission/__init__.py` that recognizes the complete bounded reason grammar and catalog, then reconstructs the full explanation. Apply it only to failed load-file reasons in `workbench.upload_projection` for displayed `message` and `work_stage`, and in `view_intake_receipt` to the response-only `upload_message` copy. This covers browser refresh as well as the initial error: the browser refreshes terminal failures and renders `work_stage` before `message` (`static/case-intelligence.js:1361–1367,1456–1465`; `workbench.py:7837–7853`). The receipt already obtains the saved message and renders it escaped (`intake_receipts.py:350–361`; `workbench.py:11493–11514`; `templates/workbench_intake_receipt.html:50`). Leave unmatched reasons unchanged. Do not rewrite durable data or use the receipt’s 240-character `error` query parameter for the full explanation.

Use the existing size/scanner/path rejection messages for those failures; do not replace their specific reasons with a format error.
The wrapper must not change `source_load_files.py` or `source_load_file_qc.py`, call `qc_volume`, resolve paths, map Bates, join pages or construct families.
CP1252 fallback does not guarantee admission of every CP1252 file; an ASCII-compatible initial sample followed by incompatible bytes can be rejected.
Valid delimited text may contain hostile strings. Neither structural admission nor a clean scan proves arbitrary text harmless.

**DECISION D3:** Keep configured document limits, including the 25 MiB default; reason: this intake PR must not increase resource exposure.
Reject the whole oversized file with existing 413 behavior; do not truncate or borrow the discovery reader’s 64 MiB allowance.
Show the existing configured file-limit label beside the support text; larger-production support needs separate measurements.

**DECISION D4:** Store canonical MIME `text/plain`; accept direct transport MIME only when empty, `text/plain` or `application/octet-stream`; reason: browser declarations vary and staged bytes remain authoritative.
Reject direct HTML/executable/archive declarations. `workbench.create_upload_session` already derives canonical MIME from suffix; retain that behavior and validate bytes at finalization (`workbench.py:11750–11782`).
Preflight remains metadata-only: detected type/readability stay unknown and required scans remain `not_run` (`loose_file_preflight.py:38–48,52–57`; `templates/workbench_setup.html:67`).
`migrations/postgresql/0005_extended_source_media_types.sql:10–12,34–36` already permits `text/plain`; no MIME migration is needed.
Classify stored load files by uploaded origin, canonical MIME and supported case-insensitive suffix, never by message text or a particular filename.

**DECISION D5:** Complete admission as stored-only, with zero units and direct-content denial; reason: retaining bytes must not enable extraction, rendering or execution.
**RULED (Exculpata 8:25 PM ET):** This stored-only outcome is accepted.
Preserve `PilotDocument`’s opaque extensionless `stored_name`, display/relative path, byte size, SHA-256 and source/version identities (`pilot_uploads.py:1665–1680,1934–1950`).
Retain private permissions, exclusive/no-follow creation, fsync, registry persistence, direct replacement and resumable hard-link/copy recovery (`pilot_uploads.py:1712–1737,1965–1984`).
Add a narrow `_extract` completion branch: internal `ready`, empty units, no units file, zero page count and no invented pages.
Exact completion message: “Load file stored; no searchable text. Metadata import is not available yet.”
Keep `PilotStore.process` and upload progress consistent with stored-only completion (`pilot_uploads.py:2769–2827`).
`ingestion.IngestionCoordinator._process` accepts ready completion and invokes indexing; `_index_document` passes units to `review_bench_v2.upsert_postgres_document` (`ingestion.py:145–163`; `workbench.py:4371–4400`).
That function calls the embedder even for an empty batch; verify actual zero-unit projection and reopen rather than assuming success (`review_bench_v2.py:902–914`).
Deny `workbench.source_content` before opening originals, returning 404 for GET, HEAD and Range requests while queued, completed and after restart.
Add no download/preview route; authorized preservation archives retain originals under their existing contract.

**DECISION D10:** Derive stored-only readiness from existing catalog fields, including a defaulted `stored_only_count`; reason: saved inputs must not inflate extraction/search coverage.
**RULED (Exculpata 8:25 PM ET):** Changes at both readiness owners are accepted and require no new schema/state.

- `Workbench._source_state`: completed load files return the stored-only message with `attention` tone; preserve queued, failed, changed and missing states.
- `_source_catalog_projection`: preserve identity/type/origin fields; never infer capability from mutable messages (`workbench.py:2074–2097`).
- `WorkspaceStore.matter_readiness`: keep SQL aggregation; bind public format constants and use equivalent suffix/MIME/origin classification.
- Stored-only sources contribute zero extracted/searchable even after successful ingestion or an “index” stage.
- Preserve active/failed-job precedence; completed stored-only sources contribute saved=1, processing=0, attention=1, stored-only=1.
- Add `store_records.MatterReadinessRecord.stored_only_count` as a defaulted derived field, not a stored column (`store_records.py:471–491`).
- Update `_source_coverage` and `matter_readiness_projection`; retain recording, email, PDF, active-work and failure notices.
- Load-file-only guidance: “Load files are saved without searchable text. Metadata import is not available yet. Add searchable sources to use search and questions.”
- Preserve exact mixed-matter exclusions and ordinary TXT searchability; test uppercase suffixes and Python/SQL equivalence.

Retain existing source-state values and byte-comparison eligibility in `src/case_intelligence/migrations/sqlite/0026_source_byte_matches.sql:20–25`.
Add DAT/OPT to `EXTENDED_SOURCE_SUFFIXES`; preserve scanner ordering, `all`/`extended`/`disabled`, quarantine and failure semantics.
Do not claim scanning occurred under disabled policy or during metadata preflight.

**DECISION D6:** Support current workbench file/browser-folder uploads, with explicit registered-folder and legacy limits; reason: those paths have distinct intake contracts.
Update `pilot_uploads.py` mappings, aliases and supported-source message; retain its “Supported sources are” prefix consumed by preflight (`loose_file_preflight.py:117–133`).
Update both `workbench_setup.html:46–47` accept attributes, selection description and scanner text.
Update `static/case-intelligence.js:468,1512`, `securityCheckedSuffixes` and `validateSelectedFiles`’s `reviewSuffixes`; preserve size/count checks.
`loose_file_preflight.evaluate_loose_file_preflight` already delegates name/size rules and imports the shared scanner suffix set; test without another policy list (`loose_file_preflight.py:16–22,117–150,168–175`).

**RULED (Exculpata 8:25 PM ET):** Keep administrator wording neutral.
Exact proposed unavailable-scanner text for both setup and administrator surfaces:

> Security scanning needs setup. Files that require a malware scan cannot be added until scanning is available.

Exact accompanying policy text under `extended`: “The current policy requires scanning for images, email, spreadsheets, and DAT/OPT load files.”
Under `all`: “The current policy requires scanning for every uploaded file.”
Under `disabled`: “Security scanning is disabled. Files are not scanned for malware before they enter this matter.”
When ready, setup retains “Private security checks are active.” followed by the applicable policy sentence.
Use neutral status styling for the revised administrator notice; do not promise that unspecified documents bypass scanning.

`source_locations.py:26–30,274–277`, `_MEDIA_TYPES` / `_enumerate`, remains PDF/DOCX/TXT for registered folders.
`templates/workspace.html:48` remains PDF/TXT-oriented; `review_bench.py:334` constructs its legacy `PilotStore` without a scanner, so extended-policy DAT/OPT admission stays blocked there.
ZIP remains absent from `_ALLOWED`; no general ZIP expansion exists in the inspected upload flows.
`workbench_workspace.html:98`’s starting-source suggestion and `docs/EXACT_SEARCH_RESULTS.md:26–34`’s extracted-format description need no new capability claim.
Document these limits in `docs/DISCOVERY_REVIEW.md`; discovery adapter/field JSON changes belong to slice 3.

Use `UploadProblem`: `workbench.upload_sources` returns its message/status and rolls back new files; `finalize_uploaded_source` returns JSON `message`, records the separate bounded reason specified in D12 and attempts incoming cleanup.
Verify browser selection, preflight and durable receipt outcomes all retain rejection reasons, with full display wording reconstructed after reopen.

No new table/column means the four lifecycle obligations do not apply: migration plus byte-identical SQLite mirror; hard-coded registration; purge DELETE; archive-schema row.
Verified example: packaged `0046_discovery_review_identifier.sql` matches `migrations/sqlite/0046_discovery_review_identifier.sql`; registration is at `workspace_store.py:272`, deletion at 2203, schema entry at `matter_archive_schema.py:102`.
Handoff corrections: `complete_matter_purge` is in `workspace_store.py`; discovery `purge.py:9–16` owns derived-cache cleanup. Field JSON is under `src/case_intelligence/config/`.
Add `core.sources` rows for both new production Python files to `tests/snapshots/import_layers.json`; test files are outside that inventory.
Preserve module isolation, no reach-across imports and no prohibited underscore attribute access (`tests/test_discovery_review_boundaries.py:12–30`).
Introduce no JavaScript `+`, `+=` or `${}` interpolation; `scripts/rename_javascript_policy.py:49–65` also rejects other construction forms.
No route registrations/public store signatures change; both route snapshots and the store-signature snapshot should remain unchanged (`tests/test_api_snapshots.py:99–116`).

### A5. Vision/AGENTS check

- **No modes:** Extend ordinary intake; no user-selectable mode or new scanner-policy switch.
- **No single-use feature:** One content-class boundary serves both upload paths and shared stored-source accounting.
- **No dropped/capped computed numbers:** Compute no Bates/pages/families; reject whole oversized inputs and retain exact saved/searchable/excluded counts.
- **No filename allowlisting:** Every safe name receives the same format/content checks; no production-specific filename exception.
- **No weakened safety/privacy/egress:** Preserve local processing, live membership, CSRF, capacity, scanning, immutable-source checks and protected storage.
- **Neutral language and synthetic data:** Use sources, matters and productions; fixtures contain no real identities, records or private affiliations.
- **Evidence integrity:** Preserve bytes/digests and disclose absent extraction/import through completion, restart and preservation.
- **Contributor practice:** Fail-first regressions, repository/DB interfaces, historical failure evidence and real HTTP/browser artifacts remain builder obligations.

### A6. Blast radius and threat model

| Area | Coverage |
| --- | --- |
| Callers/checks | Shared validation in both finalization paths; deferred extraction cannot bypass it; preflight stays metadata-only. |
| UI/docs | Both selectors and JS lists, setup/admin policy wording, failure receipts and explicit supported-subset documentation. |
| Data/readiness | Existing registry/catalog fields; SQL aggregation, TXT controls, job precedence, mixed-matter exclusions and restart tests. |
| Archives | `matter_archive.py:342–350`, `_capture_sources`, includes originals independently of suffix; test exact bytes and changed-original rejection. |
| Purge | `Workbench.execute_matter_purge` removes the source tree through existing quarantine/deletion ownership (`workbench.py:1758–1777,1818–1826,1854`); test retained files disappear without changing lifecycle code. |
| Performance | Streaming validation with bounded records and unchanged byte limits; measure near-limit valid files and late failures; no Python catalog materialization. |
| Security | Content route denies bytes; authorization, leases, CSRF, reservations, scanner implementation and integrity checks remain intact. |

An uploader could rename executable/binary content, forge MIME, submit script-like text, exhaust resources with large records, or attempt traversal through names and embedded paths.
Mitigations are whole-file grammar/encoding validation, explicit resource limits, unchanged filename/storage guards, configured malware scanning and stored-only handling.
Never evaluate formulas/scripts or resolve embedded paths. Hostile strings can still be valid delimited text; denial of serving/extraction is essential.
Scanner stand-ins prove dispatch and failure handling, not antivirus detection quality.

### A7. Proving tests

**DECISION D7:** Keep boundary tests in `tests/test_load_file_admission.py`, with targeted existing-file regressions; reason: concentrate security evidence without changing test-lane configuration.
All new test names below are proposed, not executed.
First write `test_dat_upload_preserves_original_bytes`: a synthetic two-column DAT, clean counting scanner, exact bytes/hash assertion; demonstrate today’s extension rejection on `e6644a22`.
Before readiness changes, seed a ready DAT/OPT catalog row and succeeded ingest job through public store methods; demonstrate incorrect extracted/searchable counts.
Write the administrator wording assertion before changing its template. Record the historical failing commit in the PR.

**RULED (Exculpata 9:01 PM ET):** Add the long-rejection regression before changing failure handling. Since baseline DAT/OPT admission is blocked, first reproduce the ledger defect on `e6644a22` by injecting an `UploadProblem` containing the complete explanation, longer than 240 characters, at the finalization boundary of an otherwise valid synthetic upload. Assert terminal failure through the real HTTP route and receipt; the baseline must fail that assertion. Then exercise real malformed DAT/OPT through the implemented validator: item state `failed`, single-item session `partial`, receipt availability `failed` and processing count zero, exact short reason retained after close/reopen, incoming bytes removed and no admitted original. Assert the complete prefix, support paragraph and diagnostic in the direct error, finalization JSON, refreshed browser upload display and reopened receipt page. Do not satisfy this test by stubbing `fail_upload_item` or receipt projection.

In the following table, `admission::` means `tests/test_load_file_admission.py::`.

| Proposed test | Should pass | Must still be caught |
| --- | --- | --- |
| `admission::test_dat_upload_preserves_original_bytes` | Supported DAT unchanged | Baseline rejection; missing validator/scanner dispatch |
| `admission::test_opt_upload_preserves_original_bytes` | Seven-field OPT, uppercase suffix | Empty/wrong-width records |
| `admission::test_load_file_admission_matches_both_upload_paths` | Direct/resumable, immediate/deferred | Either path bypassing validation |
| `admission::test_load_file_encodings_are_checked_to_eof` | UTF-8/BOM, CP1252, UTF-16 LE/BE BOM | UTF-32, mixed/damaged tail, late fallback failure |
| `admission::test_load_file_content_class_rejects_disguises` | Supported text records | Renamed MZ/ELF/ZIP, binary/NUL, non-table HTML/plain text, malformed DAT quoting |
| `admission::test_load_file_transport_mime_is_not_content_proof` | Supported direct aliases | Disallowed direct MIME; invalid bytes behind resumable canonical MIME |
| `admission::test_load_file_limits_reject_whole_input` | Exact configured byte limit, many small records | One byte over, oversized record, invalid tail, admitted residue |
| `admission::test_load_file_names_keep_existing_path_boundary` | Safe nested browser-relative names | Traversal, absolute/drive paths, backslashes, reserved/control/bidi names |
| `admission::test_load_file_scan_policy_and_quarantine` | Clean scan under extended/all | Missing/unavailable/error scanner, infection, quarantine failure; scan claims under disabled |
| `admission::test_load_file_http_selection_and_rejection` | File/folder selection and explanatory receipts | Quota/unsafe-descriptor denial lost; unsupported subset hidden |
| `admission::test_load_file_rejection_reason_bound_and_display` | Every diagnostic/location combination at most 222 characters, unchanged store round trip and complete display reconstruction | Raw exception/path leakage, truncation, overlong reason, unrelated messages expanded |
| `admission::test_long_load_file_rejection_is_terminal_after_reopen` | Failed ledger/receipt, durable short reason, removed incoming bytes and complete browser/receipt explanation | Swallowed overlength write leaving `uploading`; full wording lost on refresh/reopen |
| `admission::test_load_file_is_stored_without_preview` | Completed source metadata with zero units | GET/HEAD/Range bytes, Document/Text preview, text-extractor invocation |
| `admission::test_load_file_restart_and_existing_lifecycle` | Reopen, digest preservation, duplicate recovery, authorized deletion | Changed bytes, cross-matter access, retained matter-owned files |
| `admission::test_load_file_http_coverage_survives_completion_and_restart` | Truthful labels/counts/notices after real background completion/reopen | Hidden exclusions, false retry promise, active/failure/integrity states concealed |
| `tests/test_review_bench_v2_postgres.py::test_load_file_empty_projection_postgres` | Actual PostgreSQL zero-unit completion/reopen and TXT positive control | Projection failure, fabricated searchable chunks or inflated coverage |

**RULED (Exculpata 8:25 PM ET):** The PostgreSQL test belongs in the final row’s file, not the new admission file.
Verified: `Makefile:75–78` and `.github/workflows/quality-gates.yml:364–367` explicitly select that file; their deselection names a different learned-model test.
Its `postgres_schema` fixture isolates the database; `tests/conftest.py:27–45` skips without a DSN and supplies the isolated schema.
Require execution in the dedicated PostgreSQL lane and the report checker’s zero-skip result (`scripts/check-postgres-test-report.py:15–26`).

Add `tests/test_processing_awareness.py::test_stored_load_file_readiness_does_not_claim_text`:
DAT+OPT require total/saved=2, extracted/searchable=0, processing=0, attention/stored-only=2, `can_query=False`.
TXT+DAT require total/saved=2, extracted/searchable=1, processing=0, attention/stored-only=1, partial querying enabled.
TXT alone stays fully searchable; parameterize uppercase suffixes, pending/failed jobs, changed sources and classification equivalence.

Extend `tests/test_intake_scan_status_http.py::test_intake_status_matches_policy_and_scanner_readiness` for DAT/OPT preflight and exact setup text.
Add `tests/test_intake_scan_status_http.py::test_admin_scan_status_names_load_files`: authorized admin rendering across extended/all/disabled and scanner availability, without invoking `scan`.
Add `tests/test_matter_archive.py::test_archive_preserves_admitted_load_files`: originals survive preservation byte-for-byte; changed bytes still block certification.
Retain existing negative controls in `test_upload_pilot.py:268`, `test_store_rejects_paths_mime_malformed_empty_oversize_and_symlinks`; `test_loose_file_preflight.py:846`, `test_selection_preflight_is_matter_scoped_and_never_claims_a_completed_scan`; and `test_intake_receipt_http.py:178`, `test_receipt_writes_require_real_session_csrf_and_current_membership`.
Retain `test_processing_awareness.py:54`, `test_readiness_uses_durable_jobs_not_optimistic_catalog_state`, and line 105, `test_attention_blocks_only_when_no_source_is_searchable`.
Use synthetic `tmp_path` fixtures inside the builder worktree; use repository interfaces and `tests.fixture_write.fixture_write` for any necessary raw shared-workspace writes.

### A8. Exact fence

**DECISION D8:** Keep one implementation PR within these 17 files; reason: admission, serving denial and truthful accounting form one acceptance boundary.

New files, exactly four:

- `specs/item114-s3a-intake-allowlist.md`
- `src/case_intelligence/source_admission/__init__.py`
- `src/case_intelligence/source_admission/load_files.py`
- `tests/test_load_file_admission.py`

Modified files, exactly thirteen:

- `src/case_intelligence/pilot_uploads.py`
- `src/case_intelligence/workbench.py`
- `src/case_intelligence/workspace_store.py` — readiness query/projection only
- `src/case_intelligence/store_records.py` — defaulted derived count only
- `src/case_intelligence/static/case-intelligence.js`
- `src/case_intelligence/templates/workbench_setup.html`
- `src/case_intelligence/templates/workbench_admin.html`
- `docs/DISCOVERY_REVIEW.md`
- `tests/test_processing_awareness.py`
- `tests/test_intake_scan_status_http.py`
- `tests/test_matter_archive.py`
- `tests/test_review_bench_v2_postgres.py`
- `tests/snapshots/import_layers.json`

The r4 rejection fix adds **no file** to this fence: reason construction/display helpers belong in the already proposed admission package, exception transport in `pilot_uploads.py`, persistence selection and display projection in `workbench.py`, and regressions in the already proposed admission test file. `fail_upload_item`, `_safe_text`, `intake_receipts.py` and `workbench_intake_receipt.html` remain unchanged; the store’s readiness-only fence still holds.

Must NOT touch: `source_load_files.py`, `source_load_file_qc.py`, `source_locations.py`, `ingestion.py`, `review_bench.py`, `review_bench_v2.py`, `pilot_source_access.py`, `templates/workspace.html`, `templates/workbench_workspace.html`, any discovery-review package file or its field JSON.
Also excluded: migrations, `matter_archive_schema.py`, archive/purge/lifecycle implementations, malware-scanner implementation/configuration, auth/deployment/egress settings, Makefile, CI workflows, rename receipts, API snapshots and import-graph/source-size baseline increases.
All paths outside the positive fence remain excluded; do not commit `.design-inputs/`, review working files or scratch/browser artifacts.

The combined note, implementation and tests will likely exceed 500 changed lines; implementation/tests may also exceed that preference.
**RULED (Exculpata 8:25 PM ET):** Size is not a blocker. Prefer one intake PR; if packaging becomes unwieldy, separate only the reviewed design document, not interdependent safeguards.
Verified source ceilings: `pilot_uploads.py` 3,044; `workbench.py` 16,921; `workspace_store.py` 10,838 (`tests/snapshots/source_line_counts.json:3–6`).
Recheck parent head; use narrow replacement/extraction within this fence and never raise those ceilings.

### A9. Stacking

**DECISION D9 — RULED (Exculpata 8:25 PM ET):** Branch `codex/discovery-intake-load-files-114-s3a` on `codex/discovery-review-stored-index-114-s2`; reason: isolate sensitive admission review before parser integration.
Migration: **none**.
Fixed stack: `s1 -> s2 -> s3a -> s3 -> 4a -> s4 -> 5a -> 5b`; migration numbers elsewhere in the stack remain provisional.
Assume only handoff-listed slice-1 additions: per-matter codes, append-only events, Coding field/group, digit keys and archive inclusion.
Slice 2’s stored index is an intended parent contract, not inspected implementation; neither parent implementation exists at this baseline.
Reverify parent revision/holds, source callbacks, ready-source handling, readiness, source-content access, import inventory and size ceilings at build time.
The stored index must retain saved load-file items without claiming metadata import or searchable text.
Slice 3 connects retained bytes to `source_load_files.parse_dat` / `parse_opt` and discovery `load_files.py`; LFP/DII and rendering remain slices 4 and 5.

### A10. Decisions log and review

- **DECISION D1:** Recommend the two-file admission package and public shared contract; avoids duplicated boundary logic.
- **DECISION D2:** Recommend exhaustive parser reuse and the disclosed grammar/encoding subset; **RULED** minimum-two-column DAT acceptance is conditional on exact disclosure.
- **DECISION D3:** Recommend unchanged configured limits; avoids expanding resource exposure.
- **DECISION D4:** Recommend canonical `text/plain` and three direct transport aliases; bytes determine admission.
- **DECISION D5:** Recommend zero-unit completion and 404 serving denial; **RULED** stored-only admission is accepted.
- **DECISION D6:** Recommend workbench file/browser-folder scope and explicit other-path limits; **RULED** administrator wording stays neutral.
- **DECISION D7:** Recommend the focused synthetic test allocation; **RULED** PostgreSQL regression moves into the already selected database file.
- **DECISION D8:** Recommend the exact fence and single intake PR; **RULED** diff size is not a blocker.
- **DECISION D9:** **RULED** branch, parent and no migration; preserves the supplied stack.
- **DECISION D10:** Recommend derived stored-only counts and truthful exclusions; **RULED** readiness-owner changes are accepted.
- **DECISION D11:** This PR gets its own **Codex high review before merge**, separate from slice 3’s parser review; reason: Exculpata’s project decision for this sensitive intake boundary.
- **DECISION D12:** Recommend the exact bounded reason format, typed transport and shared display reconstruction in A4; preserves actionable durable failure within the existing fence. **RULED (Exculpata 9:01 PM ET):** Keep the 240-character store bound unchanged, display the complete explanation and prove terminal failure/reopen/cleanup.

**RULED (Exculpata 8:25 PM ET):** D11 is a project decision by Exculpata, not a model requirement from `QUALITY_GATES.md`.
The handoff says intake “needs own review” without naming a model.
Repository merge policy still requires six required CI checks green, resolved review conversations and maintainer-performed merge (`docs/QUALITY_GATES.md:3–6`).
No review pass or merge authorization is implied. No unanswered maintainer question remains.

### A11. Build command hints

Run only in the writable builder checkout. **RULED (Exculpata 8:25 PM ET):** all scratch, temporary and browser output stays inside that worktree.
Set a fresh worktree-local temporary root before tools that use `tempfile` or `mktemp`:

```sh
mkdir -p .scratch
scratch_run="$(mktemp -d "$PWD/.scratch/item114-s3a-XXXXXXXX")"
mkdir -p "$scratch_run/tmp"
export TMPDIR="$scratch_run/tmp"
git status --short
git diff --check
git diff --name-only codex/discovery-review-stored-index-114-s2...HEAD
.venv/bin/python scripts/publication-check.py
.venv/bin/python -m ruff check src services scripts tests
.venv/bin/python scripts/rename_contract_check.py
.venv/bin/python scripts/rename_contract_artifacts.py --check
```

Manually compare both committed and working-tree paths with A8; the inspected Makefile has no standalone fence command.
Import layers and route/store snapshots are pytest contracts, so run these structural checks before behavioral pytest:

```sh
.venv/bin/python -m pytest -q tests/test_import_boundaries.py tests/test_module_registry.py tests/test_discovery_review_boundaries.py tests/test_source_size_ratchet.py tests/test_api_snapshots.py
.venv/bin/python -m pytest -q tests/test_load_file_admission.py tests/test_processing_awareness.py tests/test_upload_pilot.py tests/test_upload_occurrences.py tests/test_loose_file_preflight.py tests/test_intake_scan_status_http.py tests/test_intake_receipt_http.py tests/test_malware_scan.py tests/test_source_load_files.py tests/test_source_load_file_qc.py tests/test_discovery_review_list.py tests/test_matter_archive.py
```

With the documented disposable synthetic PostgreSQL 17/pgvector database available:

```sh
export CASE_REVIEW_POSTGRES_TEST_DSN='postgresql://exculpata:synthetic-ci-only@127.0.0.1:5432/exculpata_test'
export CASE_INTELLIGENCE_STORAGE_RESERVE_GIB=0
export CASE_INTELLIGENCE_TEST_MODE=1
.venv/bin/python -m pytest -q tests/test_review_bench_v2_postgres.py::test_load_file_empty_projection_postgres
make test-postgres
make check-fast
.venv/bin/python -m pytest -q tests/test_browser_acceptance_runner.py
.venv/bin/python scripts/run-browser-acceptance.py --output "$scratch_run/browser-acceptance"
```

The browser output directory must not already exist (`scripts/run-browser-acceptance.py:383–387`); additionally exercise synthetic DAT/OPT file/folder selection, rejection, stored status and admin wording. For rejection, verify the complete explanation remains visible after the terminal-failure refresh and on the reopened receipt.
No snapshot update is expected. If routes change, revisit scope before `python -m tests.test_api_snapshots --update routes`; that regenerates both route configurations (`tests/test_api_snapshots.py:119–130`).
Public store signatures must remain unchanged. Skipped PostgreSQL tests or missing prerequisites are not passes.
Do not assume the handoff’s reported sandbox failures are harmless; record actual builder results.
Repeat publication checks before publishing, preserve the installed pre-push guard, inspect public PR text separately, and never force-push or use `--no-verify`.

### A12. Revision log

- **r1 P2, false searchable coverage:** Reverified `_source_state`, `matter_readiness` and `_source_coverage`; preserved D10, exact counts, completion/restart regressions and readiness-owner fence entries in A4/A7/A8.
- **r1 P3, missing administrator surface:** Reverified `workbench_admin.html:130`; preserved inventory/fence coverage and policy-aware rendering regression in A4/A7/A8.
- **r2 P2, PostgreSQL test outside its lane:** Confirmed explicit Makefile/CI selection and no-DSN skipping; moved the proposed regression to `test_review_bench_v2_postgres.py`, updating A7/A8/A11.
- **r2 P3, unattributed model requirement:** Confirmed handoff and quality policy do not specify the model; D11 attributes Codex high review to Exculpata in A10.
- **Slice rulings 1–3:** Accepted stored-only/readiness scope, disclosed the DAT subset with exact UI/rejection text, and supplied neutral admin wording in A4.
- **Slice rulings 4–6:** Corrected database-test placement, review attribution and ruled branch/parent/no-migration status in A7–A11.
- **Common structure/refs/output rulings:** Added the compact decision summary and A1–A12 appendix; updated REFS and the kickoff issue link to #503, removed answered questions, and confined builder output to worktree-local paths.
- **r3 P2 → r4, overlong durable rejection:** Verified the 240-character rejection and swallowed persistence failure in A2/A3; A4/D12 separates the bounded reason from complete display wording, A7 adds fail-first terminal/reopen/cleanup/display proofs, and A8 retains the unchanged 4-new/13-modified fence under the **RULED (Exculpata 9:01 PM ET)** contract.