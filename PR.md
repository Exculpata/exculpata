# Atomic project publication windows

## Kickoff

- Goal: reuse header aliases by definition digest and publish consecutive simple records in atomic windows of at most 12, reducing time spent preparing the discovery list while preserving every displayed value, coding value, stored item and key byte. Only A1 and the project portion of A2 are authorized.
- Expected files: reader.py, stored_index/rebuild.py, stored_index/repository.py, production/service.py (one added method after document_projection), production/repository.py, codes.py if required for the bounded document-first coding read; new tests/test_production_document_projections.py and tests/test_discovery_review_index_publish_windows.py; this review record.
- Acceptance check: traced 1,000-record project publication at most 0.1 commits and 3 statements per record, including control statements; exact single-record projection/coding/storage parity; atomic cancellation/reopen; reads and primitives at most 24 rows and steps at most 256 touches; unchanged existing tests, memory limits and local parity. Stop if Gate A fails after one correction.
- Issue: https://github.com/Exculpata/exculpata/issues/503. The issue has no assignees or comments. The supplied narrowed task is the scope authority. Related merged PR #525 supplies the baseline. The concurrent record-window work is outside this change.

This kickoff was recorded before implementation, after phase 0, and is in the branch's first commit. It matches the requested scope and user-visible outcome.

## Kickoff: review fix r1

- Goal: restore in-transaction next-record selection after single-record publication so email and load-file lists need no more project steps or commits than the original base. Strengthen full-row resume evidence and report all traced profiles.
- Expected files: stored_index/rebuild.py, the two new publication test files as needed, and PR.md. Optional style cleanup stays within the authorized fence.
- Acceptance check: reproduce the single-path regression on starting commit `4a5a741ce0bb342b408503242bd9c9671753ba3a`; measure original `dafce68` steps and commits for email and load-file populations; assert no regression for either; retain clean DAT Gate A, full-row resume equality, hard caps and unchanged existing tests.
- Issue: https://github.com/Exculpata/exculpata/issues/503. Rechecked the issue: no assignees or comments. The supplied review fix is the scope authority; no competing claim or hold was found. This matches the requested user-visible outcome and acceptance criteria. Recorded before fix coding; the original kickoff remains in the branch's first commit.

## Limits

The 1.583 ms publication ceiling is not a goal of this item and remains unmet. The broader performance item stays stopped. No order windows, contract changes, cursor/key version changes, field-build changes or existing-test restatements are included.

## Phase 0 and parent measurements

Base: `dafce68a89d06e4d2538e56faceddd64e32b308a`, matching origin/main at launch; clean source tree. Existing synthetic DAT/OPT pipeline generator, clean profile, 1,000 records, width 512, warm-up, tracemalloc enabled. Real HTTP publication, SQLite execute/executemany counted as one statement per invocation; implicit commits counted separately. Phase means the phase at entry to the step, so the final project step includes its transition into order. Counters are context-local to the step thread; an initial diagnostic with a global context was discarded because it included background SQL.

The counter run overlapped unrelated validation. Its elapsed time and instrumented peak are not timing or memory acceptance evidence. Separate fresh-process memory checks without the SQL observer are reported below.

| Metric | Base | Parent implementation |
| --- | ---: | ---: |
| Project steps | 1,001 | 87 |
| Project statements, including control | 47,048 | 2,935 |
| Project statements/record, including control | 47.048 | **2.935** |
| Project commits/record | 1.001 | **0.087** |
| Project control statements/step, excluding commit | 13 | 13 |
| Order control statements/step, excluding commit | 18 | 18 |

### Statement shapes

Counts below are measured, not inferred. Variable-length bound-parameter lists are grouped by SQL shape (`?…`); all other SQL text is retained. Per-record figures use the 1,000-record denominator. Statements count execute/executemany invocations; implicit commits are listed separately and excluded from statement totals, consistently with the existing harness. Control counts include the two code-token queries and production stamp on every signature check.

| Phase | Control SQL shape | Before count | Before/step | After count | After/step |
| --- | --- | ---: | ---: | ---: | ---: |
| project | `SELECT event_id FROM workbench_discovery_code_event WHERE matter_id=? ORDER BY event_id DESC LIMIT 1` | 3003 | 3.000 | 261 | 3.000 |
| project | `SELECT COUNT(*),COALESCE(SUM(active),0) FROM workbench_discovery_code WHERE matter_id=?` | 3003 | 3.000 | 261 | 3.000 |
| project | `SELECT COUNT(*),MAX(updated_at),SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END) FROM workbench_discovery_production WHERE matter_id=?` | 3003 | 3.000 | 261 | 3.000 |
| project | `SELECT epoch,revision,generation,definition_digest,key_version,total,indexed,pending,errors,phase,summaries,checkpoint FROM workbench_discovery_index_state WHERE matter_id=?` | 2002 | 2.000 | 174 | 2.000 |
| project | `BEGIN IMMEDIATE` | 1001 | 1.000 | 87 | 1.000 |
| project | `UPDATE workbench_discovery_index_state SET epoch=?,revision=?,generation=?,definition_digest=?,key_version=?,total=?,indexed=?,pending=?,errors=?,phase=?,summaries=?,checkpoint=? WHERE matter_id=?` | 1001 | 1.000 | 87 | 1.000 |
| project | `IMPLICIT COMMIT` | 1001 | 1.000 | 87 | 1.000 |
| order | `SELECT event_id FROM workbench_discovery_code_event WHERE matter_id=? ORDER BY event_id DESC LIMIT 1` | 1200 | 3.000 | 1200 | 3.000 |
| order | `SELECT COUNT(*),COALESCE(SUM(active),0) FROM workbench_discovery_code WHERE matter_id=?` | 1200 | 3.000 | 1200 | 3.000 |
| order | `SELECT COUNT(*),MAX(updated_at),SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END) FROM workbench_discovery_production WHERE matter_id=?` | 1200 | 3.000 | 1200 | 3.000 |
| order | `SELECT epoch,revision,generation,definition_digest,key_version,total,indexed,pending,errors,phase,summaries,checkpoint FROM workbench_discovery_index_state WHERE matter_id=?` | 800 | 2.000 | 800 | 2.000 |
| order | `PRAGMA journal_mode` | 400 | 1.000 | 400 | 1.000 |
| order | `PRAGMA wal_autocheckpoint` | 400 | 1.000 | 400 | 1.000 |
| order | `PRAGMA page_size` | 400 | 1.000 | 400 | 1.000 |
| order | `PRAGMA wal_autocheckpoint=260616` | 400 | 1.000 | 400 | 1.000 |
| order | `BEGIN IMMEDIATE` | 400 | 1.000 | 400 | 1.000 |
| order | `UPDATE workbench_discovery_index_state SET epoch=?,revision=?,generation=?,definition_digest=?,key_version=?,total=?,indexed=?,pending=?,errors=?,phase=?,summaries=?,checkpoint=? WHERE matter_id=?` | 400 | 1.000 | 400 | 1.000 |
| order | `IMPLICIT COMMIT` | 400 | 1.000 | 400 | 1.000 |
| order | `PRAGMA wal_autocheckpoint=1000` | 400 | 1.000 | 400 | 1.000 |

| Project work SQL shape | Before count | Before/record | After count | After/record |
| --- | ---: | ---: | ---: | ---: |
| `SELECT * FROM workbench_source_catalog WHERE matter_id=? AND document_id=?` | 5002 | 5.002 | 2 | 0.002 |
| `SELECT * FROM workbench_discovery_production WHERE matter_id=? AND document_id=? AND status<>'stale'` | 2001 | 2.001 | 1 | 0.001 |
| `SELECT COUNT(*),MAX(created_at) FROM workbench_discovery_identifier WHERE matter_id=?` | 1001 | 1.001 | 85 | 0.085 |
| `SELECT event_id FROM workbench_discovery_code_event WHERE matter_id=? ORDER BY event_id DESC LIMIT 1` | 1001 | 1.001 | 85 | 0.085 |
| `SELECT COUNT(*),COALESCE(SUM(active),0) FROM workbench_discovery_code WHERE matter_id=?` | 1001 | 1.001 | 85 | 0.085 |
| `SELECT DISTINCT t.receipt_id,i.relative_path FROM workbench_intake_transfer t JOIN workbench_upload_item u ON u.upload_item_id=t.upload_item_id AND u.matter_id=t.matter_id JOIN workbench_intake_item i ON i.receipt_id=t.receipt_id AND i.matter_id=t.matter_id AND i.ordinal=t.ordinal JOIN workbench_source_catalog c ON c.matter_id=u.matter_id AND c.document_id=u.document_id WHERE t.matter_id=? AND u.document_id=? AND t.source_version_id=? AND c.version_id=? AND u.state='queued' AND c.origin='upload' AND i.relative_path=u.relative_path AND c.relative_path=u.relative_path ORDER BY t.receipt_id,i.relative_path LIMIT 2` | 3001 | 3.001 | 1 | 0.001 |
| `SELECT source_payload,payload,cache_payload,projection_fingerprint,readiness FROM workbench_discovery_index_item WHERE matter_id=? AND document_id=?` | 2004 | 2.004 | 5 | 0.005 |
| `SELECT payload FROM source_document WHERE document_id IN (?…)` | 1001 | 1.001 | 85 | 0.085 |
| `SELECT document_id,identifier FROM workbench_discovery_identifier WHERE matter_id=? AND document_id IN (?…)` | 2002 | 2.002 | 86 | 0.086 |
| `SELECT order_prefix FROM workbench_discovery_index_load_row WHERE matter_id=? AND attached_document=? ORDER BY order_prefix LIMIT 1` | 1001 | 1.001 | 1 | 0.001 |
| `SELECT e.document_id,e.code_id,c.label,c.active,e.created_by,e.created_at,c.position FROM workbench_discovery_code_event e JOIN workbench_discovery_code c ON c.matter_id=e.matter_id AND c.code_id=e.code_id WHERE e.matter_id=? AND e.document_id IN (?…) AND e.action='apply' AND e.event_id=(SELECT MAX(n.event_id) FROM workbench_discovery_code_event n WHERE n.matter_id=e.matter_id AND n.document_id=e.document_id AND n.code_id=e.code_id) ORDER BY c.position,c.code_id,e.document_id LIMIT ?` | 1001 | 1.001 | 1 | 0.001 |
| `UPDATE workbench_discovery_index_item SET payload=?,cache_payload=?,projection_fingerprint=?,readiness=?,ready_generation=? WHERE matter_id=? AND document_id=?` | 1001 | 1.001 | 85 | 0.085 |
| `SELECT document_id FROM workbench_discovery_index_item WHERE matter_id=? AND seen_generation=? AND document_id>? ORDER BY document_id LIMIT 1` | 1004 | 1.004 | 5 | 0.005 |
| `SELECT attempt_id FROM workbench_discovery_dat_record WHERE matter_id=? AND document_id=? LIMIT 1` | 1000 | 1.000 | 0 | 0.000 |
| `SELECT 1 FROM workbench_discovery_production WHERE matter_id=? AND root_key=? AND status='building' LIMIT 1` | 2000 | 2.000 | 0 | 0.000 |
| `SELECT 1 FROM workbench_discovery_dat_record r JOIN workbench_discovery_production p ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id WHERE r.matter_id=? AND r.document_id=? AND p.status='building' LIMIT 1` | 2000 | 2.000 | 0 | 0.000 |
| `SELECT 1 FROM workbench_discovery_production WHERE matter_id=? AND root_key=? AND source_format IN ('csv','tsv') AND status='failed' LIMIT 1` | 2000 | 2.000 | 0 | 0.000 |
| `SELECT 1 FROM workbench_discovery_dat_record r JOIN workbench_discovery_production p ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id WHERE r.matter_id=? AND r.document_id=? AND p.status IN ('building','complete') AND r.generation=p.generation AND r.association_status NOT IN ('matched','unmatched','pending') LIMIT 1` | 2000 | 2.000 | 0 | 0.000 |
| `SELECT r.*,p.document_id AS source_id,p.version_id,p.digest,p.name,p.relative_path,p.source_format,p.root_key,p.status AS attempt_status,p.generation AS attempt_generation FROM workbench_discovery_dat_record r JOIN workbench_discovery_production p ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id WHERE r.matter_id=? AND r.document_id=? AND p.status='complete' AND r.generation=p.generation ORDER BY CASE p.source_format WHEN ? THEN 0 WHEN ? THEN 1 WHEN ? THEN 2 ELSE 3 END,r.attempt_id,r.ordinal LIMIT ?` | 1000 | 1.000 | 0 | 0.000 |
| `SELECT * FROM workbench_discovery_production WHERE matter_id=? AND attempt_id=?` | 1000 | 1.000 | 0 | 0.000 |
| `SELECT r.*,p.document_id AS source_id,p.version_id,p.digest,p.name,p.relative_path,p.source_format,p.root_key,p.status AS attempt_status,p.generation AS attempt_generation FROM workbench_discovery_opt_page r JOIN workbench_discovery_production p ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id WHERE r.matter_id=? AND r.document_id=? AND r.association_status='matched' AND p.status='complete' AND r.generation=p.generation ORDER BY r.attempt_id,r.ordinal LIMIT 1` | 1000 | 1.000 | 0 | 0.000 |
| `SELECT field_key,direction FROM workbench_discovery_index_value WHERE matter_id=? AND document_id=? AND (field_key>? OR (field_key=? AND direction>?)) ORDER BY field_key,direction LIMIT 8` | 2 | 0.002 | 3 | 0.003 |
| `INSERT INTO workbench_discovery_index_value VALUES (?,?,?,?,?,?,?,?)` | 8 | 0.008 | 11 | 0.011 |
| `UPDATE workbench_discovery_index_item SET cache_payload=? WHERE matter_id=? AND document_id=?` | 2 | 0.002 | 2 | 0.002 |
| `UPDATE workbench_discovery_index_item SET readiness=? WHERE matter_id=? AND document_id=?` | 2 | 0.002 | 2 | 0.002 |
| `SELECT document_id,source_payload,payload,cache_payload,projection_fingerprint,readiness FROM workbench_discovery_index_item WHERE matter_id=? AND seen_generation=? AND document_id>? ORDER BY document_id LIMIT ?` | 0 | 0.000 | 86 | 0.086 |
| `SELECT * FROM (SELECT ROW_NUMBER() OVER (PARTITION BY r.document_id ORDER BY CASE p.source_format WHEN ? THEN 0 WHEN ? THEN 1 WHEN ? THEN 2 ELSE 3 END,r.attempt_id,r.ordinal) AS projection_rank,r.*,p.document_id AS source_id,p.version_id,p.digest,p.name,p.relative_path,p.source_format,p.root_key,p.status AS attempt_status,p.generation AS attempt_generation FROM workbench_discovery_dat_record r JOIN workbench_discovery_production p ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id WHERE r.matter_id=? AND r.document_id IN (?…) AND p.status='complete' AND r.generation=p.generation) ranked WHERE projection_rank=1` | 0 | 0.000 | 84 | 0.084 |
| `SELECT * FROM (SELECT ROW_NUMBER() OVER (PARTITION BY r.document_id ORDER BY r.attempt_id,r.ordinal) AS projection_rank,r.*,p.document_id AS source_id,p.version_id,p.digest,p.name,p.relative_path,p.source_format,p.root_key,p.status AS attempt_status,p.generation AS attempt_generation FROM workbench_discovery_opt_page r JOIN workbench_discovery_production p ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id WHERE r.matter_id=? AND r.document_id IN (?…) AND p.status='complete' AND r.generation=p.generation AND r.association_status='matched') ranked WHERE projection_rank=1` | 0 | 0.000 | 84 | 0.084 |
| `SELECT * FROM workbench_discovery_production WHERE matter_id=? AND ((document_id IN (?…) AND status<>'stale') OR attempt_id IN (?…)) AND attempt_id>? ORDER BY attempt_id LIMIT 24` | 0 | 0.000 | 84 | 0.084 |
| `SELECT * FROM workbench_source_catalog WHERE matter_id=? AND document_id IN (?…)` | 0 | 0.000 | 84 | 0.084 |
| `WITH proofs AS (SELECT DISTINCT u.document_id,c.version_id,t.receipt_id,i.relative_path FROM workbench_intake_transfer t JOIN workbench_upload_item u ON u.upload_item_id=t.upload_item_id AND u.matter_id=t.matter_id JOIN workbench_intake_item i ON i.receipt_id=t.receipt_id AND i.matter_id=t.matter_id AND i.ordinal=t.ordinal JOIN workbench_source_catalog c ON c.matter_id=u.matter_id AND c.document_id=u.document_id WHERE t.matter_id=? AND u.document_id IN (?…) AND t.source_version_id=c.version_id AND u.state='queued' AND c.origin='upload' AND i.relative_path=u.relative_path AND c.relative_path=u.relative_path), ranked AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY document_id ORDER BY receipt_id,relative_path) AS proof_rank FROM proofs) SELECT * FROM ranked WHERE proof_rank<=2 ORDER BY document_id,proof_rank` | 0 | 0.000 | 417 | 0.417 |
| `WITH requested(document_id,root_key) AS (VALUES (?,?)…) SELECT q.document_id,EXISTS(SELECT 1 FROM workbench_discovery_production p WHERE p.matter_id=? AND p.root_key=q.root_key AND q.root_key<>'' AND p.status='building') AS root_pending,EXISTS(SELECT 1 FROM workbench_discovery_dat_record r JOIN workbench_discovery_production p ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id WHERE r.matter_id=? AND r.document_id=q.document_id AND p.status='building') AS document_pending,EXISTS(SELECT 1 FROM workbench_discovery_production p WHERE p.matter_id=? AND p.root_key=q.root_key AND q.root_key<>'' AND p.source_format IN ('csv','tsv') AND p.status='failed') AS root_failed,(SELECT r.association_status FROM workbench_discovery_dat_record r JOIN workbench_discovery_production p ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id WHERE r.matter_id=? AND r.document_id=q.document_id AND p.status IN ('building','complete') AND r.generation=p.generation AND r.association_status NOT IN ('matched','unmatched','pending') ORDER BY CASE r.association_status WHEN 'conflicting_load_file_metadata' THEN 0 WHEN 'team_identifier_conflict' THEN 1 WHEN 'conflicting_match' THEN 2 WHEN 'conflicting_dat_rows' THEN 3 ELSE 4 END,r.association_status LIMIT 1) AS problem FROM requested q` | 0 | 0.000 | 84 | 0.084 |
| `SELECT document_id,source_payload,payload,cache_payload,projection_fingerprint,readiness FROM workbench_discovery_index_item WHERE matter_id=? AND document_id IN (?…)` | 0 | 0.000 | 84 | 0.084 |
| `SELECT document_id,version_id,display_name FROM workbench_source_catalog WHERE matter_id=? AND document_id IN (?…)` | 0 | 0.000 | 84 | 0.084 |
| `SELECT e.document_id,e.code_id,c.label,c.active,e.created_by,e.created_at,c.position FROM workbench_discovery_code_event e JOIN workbench_discovery_code c ON c.matter_id=e.matter_id AND c.code_id=e.code_id WHERE e.matter_id=? AND e.document_id IN (?…) AND e.action='apply' AND e.event_id=(SELECT MAX(n.event_id) FROM workbench_discovery_code_event n WHERE n.matter_id=e.matter_id AND n.document_id=e.document_id AND n.code_id=e.code_id) ORDER BY e.document_id,c.position,c.code_id LIMIT 12` | 0 | 0.000 | 84 | 0.084 |
| `SELECT attached_document,source_id,line,basis,values_json,source_payload FROM (SELECT r.attached_document,r.source_id,r.line,r.basis,r.values_json,i.source_payload,ROW_NUMBER() OVER (PARTITION BY r.attached_document ORDER BY r.order_prefix,r.source_order,r.line) AS choice FROM workbench_discovery_index_load_row r JOIN workbench_discovery_index_item i ON i.matter_id=r.matter_id AND i.document_id=r.source_id WHERE r.matter_id=? AND r.attached_document IN (?…)) selected WHERE choice=1` | 0 | 0.000 | 84 | 0.084 |

## Implementation and evidence integrity

Fix level: code. Header aliases are lazy and held in an eight-entry LRU whose key compares only the definition digest. Callers without a digest retain the per-call path.

Production preparation uses a bounded, read-only repository snapshot and the existing single-document projection method. It covers own-attempt states and diagnostics, format priority, family context, page references, provenance ambiguity and attempt validation. The two known selection edge cases below remain outside this fix. Every preparation read is capped at 24 touched rows; provenance reads retain at most two distinct proofs per document and split into primitives of at most 24 touches.

A project window prepares at most 12 simple records before the transaction. Inside the transaction it reads documents and source captures in batches, rechecks catalog versions and source types, reads identifiers and attachments in batches, and reads at most 12 coding rows in document/position/code order. A full coding page excludes its last document and everything after it. The remaining row allowance can further shorten the prefix. Publication uses bounded executemany and advances after_document only after the whole window is written. Rejected preparation and coding continuations use the existing single-record cursor. No partial window is persisted.

The design note's proposed project value read does not exist on this base: repository.values is an order-phase insertion primitive, and project does not read old value rows. Likewise the stored-index family-parent lookup belongs to order. These paths remain unchanged, following main with the smallest edit; family catalog context is batched in production preparation. The order phase retains its existing rows, keys, fields and single-record continuation. No frozen file, existing test or snapshot was edited.

Regression baseline: `dafce68a89d06e4d2538e56faceddd64e32b308a` (the kickoff commit has identical product source). Before implementation, the alias probe failed on the unsupported definition_digest argument and the production batch probe failed on the missing document_projections method. The new full-row test restores all derived tables, including attachment rows subject to cascading deletion, and compares complete item rows and every value/key byte against the in-tree single-record path with `prepare_window` patched to fall back, on the same generation. AC 7 does not run a separate dafce68 build for full-row comparison. It includes retired coding labels, more than 12 coding rows in a window, and a heavily coded record requiring fallback. Additional tests cover family budgets, a projection exception ending the window, cancellation/reopen, and single-record coding resume.

## Parent Gate A correction

The initial window integration did not reach a preparation boundary: it measured the baseline again, 47.048 statements and 1.001 commits per record. The allowed correction connected window preparation and combined bounded attempt/catalog reads. The next measurement passed at 2.841 statements and 0.086 commits per record. Subsequent correctness checks required yielding at a record boundary so a rejected window could actually take the single-record path, preserving coding resume, and rechecking classification inside the transaction. The parent-source measurement passes at 2.935 statements and 0.087 commits per record, as recorded above. Caps, budgets and window size were never increased.

## Parent validation

- New projection/window regressions: 26 passed.
- Repository policy checks: 2,069 passed, including fixture writes/inventory, import boundaries, source size and DB access. Snapshots are unchanged.
- Final focused suite: **359 passed, 1 existing opt-in skip**, including PostgreSQL portability and the unchanged lifecycle, checkpoint, resume, updates, perf, list, boundaries and source-load QC tests. The new regressions are included in this total.
- The focused run overlapped other validation. Its heavily coded lifecycle test took **126.61 seconds**, exceeding the existing 125-second exception ceiling; a serialized rerun was queued but never started because the shared lock remained held by another full CI job. That duplicate queued run was cancelled; the launcher must rerun this unchanged test as part of full parity. This duration gate remains unresolved. The 10,000-record row-budget test took 113.36 seconds, within its existing 135-second exception. No timeout or exception was changed.
- Parent Gate A: **PASS**, 1,000 records traced; 2.935 statements and 0.087 commits per record, controls included. The observed publication maximum was 236 touches per step; the new coded/family tests independently enforce 256 touches, 24 rows per read and 24 touches per primitive.
- Targeted Ruff and whitespace checks passed. The private-term scan passed on all nine changed paths. The working-tree publication scan passed with `--skip-history` after generated scratch artifacts and Ruff caches were moved out of the publishable tree; this does not claim a history scan.

| Fresh-process tracemalloc profile | Base ceiling (MiB) | Final peak (MiB) | Result |
| --- | ---: | ---: | --- |
| 500 records | 7.376 | 4.277 | Pass |
| 1,000 records | 11.529 | 6.843 | Pass |

The parent launcher subsequently completed full parity: `ran=45 failed=0 not_run=30 not_run_unexpected=0`, GREEN OVERALL. That supersedes the parent duration-gate uncertainty above; it is evidence for the parent revision only. The launcher will run full parity again after this fix commit. No fix-revision parity or readiness approval is claimed here.

Synthetic fixtures only. No push, merge, force-push, hook bypass, schema change, timeout change, durability change, skip or xfail was introduced.


## Review fix r1

Fix level: code, at the project transaction boundary. Selection now checks whether the next two records can form a simple window before yielding for preparation. Email, load-file, lone-simple and final records retain in-transaction selection; the existing single-record path still owns their preparation, coding and publication. A failed window still selects its first record for single-record preparation. No persistent cursor state, row cap, window size, timeout or parity configuration changes.

Regression baseline: `4a5a741ce0bb342b408503242bd9c9671753ba3a`. Before the fix, the traced phase-0 harness measured **51 project steps and 51 commits for 25 records**, for both email and load-file populations. Original-base measurements at `dafce68a89d06e4d2538e56faceddd64e32b308a` measured **25 steps and 25 commits** for each. The regression pins these measured base rates (1.0 step and commit per record); it does not substitute a mocked single path for dafce68. Each fixture contains one header-only DAT plus 24 ready email originals or 24 header-only CSV sources. Counts are taken at project-step entry, and commits are observed from SQLite transaction completion. Fixture generation uses the existing synthetic historical-manifest generator and real HTTP completion.

The first attempted correction restored continuation after publication but left one extra phase-entry step and prevented simple DAT records from returning to windows. It was discarded. The final correction checks actual adjacent eligibility before yielding. The final Gate A results below determine acceptance; no budget or cap was relaxed.

AC 8 now compares complete item rows as well as all value rows after cancellation/reopen. Test imports formerly in the middle of the new window test file are at the top.

Known P3 selection differences remain unchanged: batched attachments rank after joining to the source index item, whereas the single path selects its lowest-order attachment before that join; a missing source item can therefore change the chosen attachment. The public `document_projections` read model retains the last non-stale own attempt by attempt ID, whereas `document_projection` takes the first unordered current attempt; multiple non-stale own attempts can therefore differ. Load-file sources remain excluded from publication windows. Neither selection semantic is changed in this item.


### Traced profile results for r1

Original-base source for the new comparisons was extracted from `dafce68` into an isolated measurement directory; the same external observer ran against that source and the fix source. The observer uses context-local project-step attribution and counts SQL execute/executemany calls, actual commits and control statements as in phase 0. All runs below use tracemalloc. Shared-machine elapsed times are not timing acceptance evidence.

| Profile and denominator | Base project steps | Fix project steps | Base statements/record | Fix statements/record | Base commits/record | Fix commits/record | Base/fix controls/step |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Clean DAT: 1,000 generated data rows, 1,001 indexed sources | 1,001 | 86 | 47.048 | **2.923** | 1.001 | **0.086** | 13 / 13 |
| Email: 999 ready emails and one header-only DAT, 1,000 indexed sources | 1,000 | 1,000 | 36.016 | 36.016 | 1.000 | 1.000 | 13 / 13 |
| Load-file: 999 header-only CSV sources and one header-only DAT, 1,000 indexed sources | 1,000 | 1,000 | 29.023 | 29.023 | 1.000 | 1.000 | 13 / 13 |

Gate A passes for the clean DAT window profile: 2,923 project statements and 86 commits. The email and load-file profiles deliberately use the unchanged single-record path; both miss the numeric window thresholds, and neither is claimed as a Gate A pass. Their acceptance here is no regression against dafce68, which both meet. Every project SQL shape count, not just the totals, is identical between base and fix in these two profiles. The 1.583 ms publication ceiling remains unmet.

The 1,000-source load-file comparison uses a separate synthetic receipt per source. The initial shared-receipt runs were stopped during dependency matching, before project publication; they produced no Gate A result and are not included in this table. This changes only the measurement fixture, not importer behavior. The 25-record load-file regression retains a single shared receipt. Clean DAT and email profiles also retain their original shared-receipt layout.

Control shapes were counted at project-step entry, including the transition to order in the last project step. For each email and load-file run, base and fix both recorded 3,000 code-event-token SELECTs, 3,000 code-count SELECTs, 3,000 production-stamp SELECTs, 2,000 index-state SELECTs, 1,000 BEGINs and 1,000 state UPDATEs: 13,000 control statements across 1,000 steps. Actual commits are reported separately, consistently with phase 0. The clean fix has the same shape ratios: 258, 258, 258, 172, 86 and 86 respectively, totaling 1,118 controls across 86 steps.

Fresh-process existing pipeline memory profiles, without the SQL observer and including completion verification, measured 4.301 MiB at 500 records and 10.521 MiB at 1,000. Both are below the original 7.376/11.529 MiB ceilings. The traced clean publication maximum remains 236 touches per step.


### Fix validation

The new regression was executed against isolated source copies: **2 passed on dafce68**, and **2 expected failures on 4a5a741**, each reporting `steps=51, commits=51` against the measured 25-record baseline. The initial standalone fix command failed collection because its checkout PYTHONPATH was omitted; it produced no test result. The broader focused command explicitly selects this checkout and includes both regressions. It runs serially after acquiring the shared pytest lock; all tracing and memory profiles finished before it began.


The serial focused run completed with **2,413 passed, 2 existing skips and one test-policy failure**. All product tests passed, including the strengthened full-row resume check. The policy failure identified unguarded trace-callback setup/cleanup in the new regression; both operations now use the existing workspace-lock pattern from the unchanged lifecycle test. No product source changed after the measured profiles or focused run. One skip was the existing opt-in million-item benchmark; the other was PostgreSQL portability because no DSN was set, so this run does not claim real PostgreSQL validation.

JUnit total durations were **102.588 seconds** for heavily coded lifecycle resume (ceiling 125) and **42.275 seconds** for the 10,000-record budget test (ceiling 135). Every other executed test was below 30% of its pytest timeout. No timeout, exception or parity setting was changed.


The final affected-test and policy rerun passed **2,082 tests**: both new test files, fixture-write policy/inventory/coverage/reflective-denial checks, DB access ratchet and import boundaries. The final regression, including locked callback setup, was rechecked: 2 passed on dafce68 and 2 expected failures on 4a5a741, again recording 51 steps and commits per 25 records on that parent. The unchanged PostgreSQL portability test then passed against a disposable instance of the already-cached CI-pinned PostgreSQL 17/pgvector image; its synthetic container was removed afterward. The opt-in million-item benchmark remains the only unexecuted focused test. The short PostgreSQL and final historical checks ran directly after another full parity job acquired the shared queue; no timing acceptance claim uses those concurrent runs.

Ruff, whitespace validation, the PR-body check, the working-tree publication scan (`--skip-history`) and the required added-lines private-term scan pass. Full parity on the fix commit remains assigned to the launcher. No push or main merge was performed.


## Kickoff: merge-queue fix r2

- Goal: make cancellation and reopening publish exactly the same complete stored rows as uninterrupted indexing after integrating #527; preserve the bounded publication windows and visible pending-state behavior.
- Expected files: production/service.py and tests/test_production_document_projections.py; stored_index/rebuild.py or the publication test file only if the reproduced interaction requires it; PR.md.
- Acceptance check: branch-only reproduction five times; merged baseline once, twenty serial repeats and the two-file xdist run; a deterministic regression failing before the fix; twenty green repeats and xdist afterward, focused index/production suites, and launcher full parity on the final committed revision with existing duration gates. No weakened assertions or changed budgets.
- Issue: https://github.com/Exculpata/exculpata/issues/503; related PR https://github.com/Exculpata/exculpata/pull/528 and merged PR #527. Rechecked issue and PR context: no assignee or competing hold found. The supplied final-retry scope authorizes this integration correction and matches the outcome and checks above. Recorded before merge and fix coding; original kickoff remains in the first branch commit.


## Merge-queue correction r2

Starting branch revision: `81cab3c49cbc5429652e5588ebe68ce7b51afcb7`. Launch main: `718469390d50c49465fd7e7f64d6661c4ed91363`. Normal merge commit on the launcher branch: `f3e5c6e9301dbeeb528f7fb5004eb03473224365`; both requested revisions are ancestors. No other branch was changed.

**CI-ONLY MISS:** Local parity on the parent built on `dafce68` could not exercise #527, which was absent from that branch. #527 added a dependency-currentness check accepting the owning service epoch or a matching cached validation stamp (`production/service.py:237-245`). Cancellation removes the binding (`production/service.py:97-105`); rebinding creates an empty validation cache (`production/service.py:146-151`). The window adapter constructed a new ProductionService with a fresh epoch (`production/service.py:93,1115`) while sharing the caller's binding, so after cancel/reopen it rejected a completed attempt still owned and accepted by the caller. The deterministic regression proves status stays complete, the production stamp stays unchanged, the cache is empty, and single-document projection stays complete (`tests/test_production_document_projections.py:50-62`). That false pending payload had no subsequent production transition to invalidate it. Generated document IDs sometimes put the native on the single-record fallback, explaining intermittent detection; the fixture uses a fresh temporary matter and complete rows are already compared in document-key order. This is a semantic batch/single mismatch, not shared fixture leakage or unstable comparison order.

Hypothesis verdict: **#527 interaction confirmed; transient building-state explanation rejected for the reproduced failure.** `document_projections` already calls the shared `document_projection`; the missing input was its owning service epoch, not copied projection status logic. The explicit replacement-service regression also exercises the real `building`/`was_complete` scan and restoration before rebinding, and reproduces the same defect after restoration.

Fix level: **code**, at the batch adapter boundary. Preserve `self.epoch` on the read-only window service (`production/service.py:1117`), alongside the existing shared bindings/discovery state. The per-window validation-result cache stays local. This changes one product line, adds no reads or writes, and preserves the main implementation of attempt currentness, input revisions, recovery and 409 responses. No contract change, schema/key change, row/step-constant change or order-window work is needed. The unchanged full-row cancellation test checks stored payloads, fingerprints, item rows and value rows against uninterrupted publication.

Regression baseline: `f3e5c6e9301dbeeb528f7fb5004eb03473224365`. Before the product fix, both new deterministic cancel/rebind cases failed at batch-versus-single equality, including the case with a real replacement owner and retained `was_complete`; all five new foreign-owner/validation/pause/pending-revision controls passed. The setup proves the cause without relying on random IDs, sleeps, retries or altered publication assertions.

### Reproduction before the fix

| Revision and command | Passed | Failed |
| --- | ---: | ---: |
| Branch-only `81cab3c4`, unchanged failing test, five separate pytest invocations in a shell loop | 5 | 0 |
| Merged baseline, unchanged failing test alone | 0 | 1 |
| Merged baseline, unchanged failing test, twenty separate pytest invocations in a shell loop | 5 | 15 |
| Merged baseline, both publication-window and pending-list files with `-n 4` | 18 | 1 |
| Merged product source, seven new regression/control cases | 5 | 2 |

The merged xdist failure is the same full-item cancellation comparison; every pending-list test passed. Initial short correctness runs overlapped an unrelated parity job and make no timing acceptance claim. Raw logs are retained in the job's external `evidence/` directory.


### Validation after the fix

- Unchanged cancellation/full-row comparison: **20 passed, 0 failed**, twenty separate pytest invocations in a shell loop.
- Both unchanged publication-window and pending-list files with `-n 4`: **19 passed, 0 failed**. No rerun/repeat plugin was used.
- Complete projection file: **27 passed, 0 failed**, including both previously failing reopen regressions and the five new #527 state controls.
- Gate A remeasured through real synthetic HTTP publication using the r1 context-local SQL/commit observer: **2,923 statements, 86 commits, 86 project steps** per 1,000 generated records (1,001 indexed sources), unchanged from r1. That is **2.923 statements and 0.086 commits per record**, including 1,118 control statements, or 13 per step. Maximum publication step remains **236 touches**. This run counts statements without tracemalloc; it makes no new elapsed-time or memory qualification claim.

- Full requested focused index/DAT/OPT/production suite plus fixture-write policy, inventory, coverage, reflective-denial, DB-access, import-boundary and source-size checks: **2,266 passed, 0 failed, 2 existing skips**. The skips are the explicit million-item qualification and PostgreSQL portability without a configured DSN; this run does not claim PostgreSQL validation.
- JUnit setup/call/teardown totals: lifecycle resume **110.252 s** (ceiling 125), row_budget[10000] **49.932 s** (ceiling 135), slowest other test **60.053 s** (ceiling 108). All observed focused-test duration limits passed; the run overlapped another job and is not a replacement for the launcher's final parity timing gate.
- Ruff with caching disabled, whitespace validation, PR-body validation, working-tree publication scanning (`--skip-history`) and the required added-lines private-term scan pass. A locally generated Ruff cache was moved outside the worktree before the publication scan. The two requested existing test files are byte-for-byte unchanged from the merged baseline. Synthetic data only; no push, main-branch change, force-push, hook bypass, skip, xfail, retry plugin or assertion weakening.

Full CI parity on the final committed revision is assigned to the launcher by the task's final instruction. The r1 parity result is historical and does not establish r2 readiness. The launcher must enforce zero unexpected unrun steps, no NEAR TIMEOUT and the unchanged 108/125/135-second duration ceilings.


## Kickoff: merge-forward r3

- Goal: integrate origin/main including #524 into #528, preserving the discovery publication behavior and all accepted r2 fixes. This is the authorized merge-forward, outside the counted retry cap.
- Expected files: retain the branch version of PR.md and append this record; accept main's changes unchanged. No product, test, workflow or script edits beyond the merge.
- Acceptance check: normal two-parent merge from `52c36f0627dd8e1dde8bf778614e628a47fa8d0d` and fetched main; only PR.md may conflict; the three-dot diff retains only #528 changes; focused tests and publication checks pass; launcher full parity enforces zero unexpected unrun steps, no NEAR TIMEOUT and unchanged 108/125/135-second ceilings.
- Issue: https://github.com/Exculpata/exculpata/issues/503; PR: https://github.com/Exculpata/exculpata/pull/528. Live issue and PR comments, assignees and related #524 were checked. No competing assignment or hold was found. The supplied merge-forward scope matches this outcome. Recorded before conflict resolution; included in this attempt's first commit.

## Merge-forward r3

Starting branch revision: `52c36f0627dd8e1dde8bf778614e628a47fa8d0d`.
Merged revision and fetched origin/main: `af3ee519c6c0797b0e1b9f97e5bbe4de7de04522` (#524).
The resulting commit has these revisions as its first and second parents, respectively.

Only PR.md conflicted (add/add). Its complete branch-side contents are retained above, with this merge-forward record appended. Main's #524 changes are accepted unchanged. No product logic, tests, workflow, scripts, timeout, skip, xfail or assertions were changed by conflict resolution.

The supplied r2 launcher result was PASS with `ran=45 failed=0 not_run_unexpected=0`; it is historical evidence. Under the task's final launcher instruction, full CI parity on this final committed merge is assigned to the launcher after this attempt finishes. Merge-revision full parity and its lifecycle/row-budget seconds are pending; r2 timings do not establish acceptance of this merge.

### Merge-forward validation

- Focused synthetic validation: **282 passed, 0 failed, 0 skipped** across publication windows, production projections, CI parity contracts/concurrency/durations and application shard selection. The slowest JUnit total was **70.759 s** for the shard-collection contract, below 108 s. Lifecycle resume and row_budget[10000] were not part of this focused run; their required final parity timings remain pending with the launcher.
- Verified that the nine branch-only paths match the original #528 path set. All eight non-document branch paths are byte-for-byte unchanged from r2; all fourteen main-only paths are byte-for-byte identical to the merged main revision. The existing branch PR.md is retained in full.
- Required added-lines private-term scan, PR-body validation, whitespace check and generic working-tree publication scan passed. The working-tree scan used `--skip-history`; full committed-history validation remains part of launcher parity.
- No push, rebase, force-push, main-branch change or hook bypass. `last.md` is locally excluded and is not staged.

## Kickoff: r5 merge-forward rerun

- Goal: preserve the accepted #528 merge and add a real documentation commit so the launcher reaches the isolated parity rerun, retaining the same discovery publication behavior.
- Expected files: PR.md only. The existing local exclusion for last.md remains in effect.
- Acceptance check: retain the required merge ancestry and branch-only diff; pass focused documentation checks and the added-lines private-term scan; commit this note, then let the launcher run full parity on that committed revision alone. Require zero unexpected unrun steps, no NEAR TIMEOUT and unchanged 108/125/135-second ceilings.
- Issue: https://github.com/Exculpata/exculpata/issues/503; PR: https://github.com/Exculpata/exculpata/pull/528. Rechecked issue and PR comments, assignees and related #524. No competing assignment or hold was found. The supplied r5 instruction authorizes this documentation-only rerun and matches the outcome and checks above.

## r5 merge-forward rerun

Preserved merge commit `daa37eb6dde5c40e0e0493d520c93c2ea7c9c5a6`, whose parents are `52c36f0627dd8e1dde8bf778614e628a47fa8d0d` and `af3ee519c6c0797b0e1b9f97e5bbe4de7de04522`. After fetching, current origin/main remains `af3ee519c6c0797b0e1b9f97e5bbe4de7de04522`, so no additional merge is needed. The three-dot diff contains only the same nine #528 paths; #524's speedup files are unchanged from main.

Per the supplied r3 result, no test assertion failed. Shard 1 failed only NEAR TIMEOUT under the concurrent #554 job: `tests/test_documentation.py::test_rename_contract_is_checked_on_documentation_only_changes` took **136.93 s**, and `tests/test_discovery_review_dat_opt_storage.py::test_memory_contract_detects_materializing_controls[wide-8-32-262144-count_only]` took **134.21 s**, both above the unchanged **108 s** ceiling. r4 stopped with "no commit produced" before parity because the existing merge was already present.

r5 is scheduled to rerun parity alone after this new PR.md-only commit. Under the final launcher instruction, execution begins after this attempt finishes; the r5 result and measured lifecycle-resume/row_budget[10000] seconds are therefore pending, not claimed as a pass here. Full parity must report `not_run_unexpected=0` and no NEAR TIMEOUT. The serial post-shard ceilings remain **125 s** for lifecycle resume and **135 s** for row_budget[10000]; all other tests must stay at or below **108 s**. A further NEAR TIMEOUT while running alone fails this attempt; no test changes or retry are authorized.

No product, test, workflow or script changes are included in this rerun commit. No skip, xfail, timeout increase or assertion weakening is introduced. last.md remains excluded and must not be committed.

Focused r5 validation: **90 passed, 0 failed** in tests/test_pr_body.py. PR-body validation, whitespace validation and the required added-lines private-term scan passed. The retained product and publication-test paths are byte-for-byte unchanged from r2. Full parity remains pending with the launcher on the new committed revision.


## Kickoff: merge-forward r6

- Goal: merge origin/main including #554 into #528 while preserving publication windows, cached header aliases, and LFP/DII reconciliation so discovery continues to show complete, current production metadata. This is the authorized merge-forward only, outside the retry cap.
- Expected files: retain this branch's PR.md and append this record; accept main's changes, combining shared discovery/index changes only where conflict resolution is necessary. No feature, contract, test, workflow, launcher or scanner changes.
- Acceptance check: normal two-parent merge from `a3cb249be6a366f6ede831e5adad51a75f747ef1` and fetched origin/main; three-dot diff contains only #528 changes; both parents' focused tests and publication checks pass. Launcher full parity on the committed merge must report zero unexpected unrun steps, no NEAR TIMEOUT, and unchanged 108/125/135-second ceilings. Stop if integration requires new logic or a behavioral choice.
- Issue: https://github.com/Exculpata/exculpata/issues/503; PR: https://github.com/Exculpata/exculpata/pull/528; merged integration: https://github.com/Exculpata/exculpata/pull/554. Live issue/PR comments, assignments and related work were checked; no competing claim or hold was found. The supplied merge-forward scope matches this outcome and acceptance check. Recorded before conflict resolution, in this attempt's first commit; the original kickoff remains in the branch's first commit.

## Merge-forward r6

Merged origin/main including #554, whose merge SHA is `cfb8749508510895b9e6c48f0428501b9ec21f31`. The merge commit containing this record has first parent `a3cb249be6a366f6ede831e5adad51a75f747ef1` and second parent `cfb8749508510895b9e6c48f0428501b9ec21f31`.

Only PR.md conflicted. Its branch-side contents are retained in full above. Shared production/service.py and reader.py merged automatically. The original #528 additions/deletions in both files are preserved exactly alongside #554; no new product logic or behavior choice was needed.


### r6 validation and launcher handoff

- Focused synthetic suite: **166 passed, 0 failed, 0 skipped** across publication windows, production projections, LFP/DII integration and parsing, DAT/OPT, pending-list state and LFP/DII admission. This includes the existing full-row cancellation/reopen check and #554's changed-source withdrawal regressions. The initial invocation used a nonexistent pending-list filename and collected no tests; the corrected invocation above completed successfully.
- Slowest focused JUnit setup/call/teardown total: **40.069 s** for `test_opt_navigation_reaches_tail_on_both_endpoints`, below 108 s. No focused test exceeded the 30% duration limit.
- All six branch-only files and fourteen main-only files are byte-for-byte preserved. Shared source additions/deletions match #528 exactly; the diff versus merged main contains exactly the original nine #528 paths. The original branch PR.md is retained in full.
- Ruff for the shared source files, whitespace checks, PR-body validation, the working-tree publication scan (`--skip-history`) and the required added-lines private-term scan passed. No test edits, new skips/xfails, timeout increases or weakened assertions were introduced by this merge resolution. last.md is locally excluded and untracked.

Full CI parity on the committed merge is assigned to the launcher after this attempt finishes, per the final task instruction. **r6 full parity and its serial post-shard timings are pending**, not claimed green from r5 or this focused run. The launcher must report `not_run_unexpected=0`, no NEAR TIMEOUT, and every ordinary test at or below 108 s; record the measured lifecycle-resume seconds (ceiling 125 s) and row_budget[10000] seconds (ceiling 135 s) when available. Neither isolated test was part of this focused suite. No launcher, config, parity script, scanner or allowlist was edited. No push or main-branch update was performed.
