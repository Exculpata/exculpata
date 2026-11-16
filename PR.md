# Atomic project publication windows

## Kickoff

- Goal: reuse header aliases by definition digest and publish consecutive simple records in atomic windows of at most 12, reducing time spent preparing the discovery list while preserving every displayed value, coding value, stored item and key byte. Only A1 and the project portion of A2 are authorized.
- Expected files: reader.py, stored_index/rebuild.py, stored_index/repository.py, production/service.py (one added method after document_projection), production/repository.py, codes.py if required for the bounded document-first coding read; new tests/test_production_document_projections.py and tests/test_discovery_review_index_publish_windows.py; this review record.
- Acceptance check: traced 1,000-record project publication at most 0.1 commits and 3 statements per record, including control statements; exact single-record projection/coding/storage parity; atomic cancellation/reopen; reads and primitives at most 24 rows and steps at most 256 touches; unchanged existing tests, memory limits and local parity. Stop if Gate A fails after one correction.
- Issue: https://github.com/Exculpata/exculpata/issues/503. The issue has no assignees or comments. The supplied narrowed task is the scope authority. Related merged PR #525 supplies the baseline. The concurrent record-window work is outside this change.

This kickoff was recorded before implementation, after phase 0, and is in the branch's first commit. It matches the requested scope and user-visible outcome.

## Limits

The 1.583 ms publication ceiling is not a goal of this item and remains unmet. The broader performance item stays stopped. No order windows, contract changes, cursor/key version changes, field-build changes or existing-test restatements are included.

## Phase 0

Base: `dafce68a89d06e4d2538e56faceddd64e32b308a`, matching origin/main at launch; clean source tree. Existing synthetic DAT/OPT pipeline generator, clean profile, 1,000 records, width 512, warm-up, tracemalloc enabled. Real HTTP publication, SQLite execute/executemany counted as one statement per invocation; implicit commits counted separately. Phase means the phase at entry to the step, so the final project step includes its transition into order. Counters are context-local to the step thread; an initial diagnostic with a global context was discarded because it included background SQL.

The counter run overlapped unrelated validation. Its elapsed time and instrumented peak are not timing or memory acceptance evidence. Separate fresh-process memory checks without the SQL observer are reported below.

| Metric | Before | After |
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

Production preparation uses a bounded, read-only repository snapshot and the existing single-document projection method. It preserves own-attempt states and diagnostics, format priority, family context, page references, provenance ambiguity and attempt validation. Every preparation read is capped at 24 touched rows; provenance reads retain at most two distinct proofs per document and split into primitives of at most 24 touches.

A project window prepares at most 12 simple records before the transaction. Inside the transaction it reads documents and source captures in batches, rechecks catalog versions and source types, reads identifiers and attachments in batches, and reads at most 12 coding rows in document/position/code order. A full coding page excludes its last document and everything after it. The remaining row allowance can further shorten the prefix. Publication uses bounded executemany and advances after_document only after the whole window is written. Rejected preparation and coding continuations use the existing single-record cursor. No partial window is persisted.

The design note's proposed project value read does not exist on this base: repository.values is an order-phase insertion primitive, and project does not read old value rows. Likewise the stored-index family-parent lookup belongs to order. These paths remain unchanged, following main with the smallest edit; family catalog context is batched in production preparation. The order phase retains its existing rows, keys, fields and single-record continuation. No frozen file, existing test or snapshot was edited.

Regression baseline: `dafce68a89d06e4d2538e56faceddd64e32b308a` (the kickoff commit has identical product source). Before implementation, the alias probe failed on the unsupported definition_digest argument and the production batch probe failed on the missing document_projections method. The new full-row test restores all derived tables, including attachment rows subject to cascading deletion, and compares complete item rows and every value/key byte against the single-record path on the same generation. It includes retired coding labels, more than 12 coding rows in a window, and a heavily coded record requiring fallback. Additional tests cover family budgets, a projection exception ending the window, cancellation/reopen, and single-record coding resume.

## Gate A correction

The initial window integration did not reach a preparation boundary: it measured the baseline again, 47.048 statements and 1.001 commits per record. The allowed correction connected window preparation and combined bounded attempt/catalog reads. The next measurement passed at 2.841 statements and 0.086 commits per record. Subsequent correctness checks required yielding at a record boundary so a rejected window could actually take the single-record path, preserving coding resume, and rechecking classification inside the transaction. The final-source measurement passes at 2.935 statements and 0.087 commits per record, as recorded above. Caps, budgets and window size were never increased.

## Validation

- New projection/window regressions: 26 passed.
- Repository policy checks: 2,069 passed, including fixture writes/inventory, import boundaries, source size and DB access. Snapshots are unchanged.
- Final focused suite: **359 passed, 1 existing opt-in skip**, including PostgreSQL portability and the unchanged lifecycle, checkpoint, resume, updates, perf, list, boundaries and source-load QC tests. The new regressions are included in this total.
- The focused run overlapped other validation. Its heavily coded lifecycle test took **126.61 seconds**, exceeding the existing 125-second exception ceiling; a serialized rerun was queued but never started because the shared lock remained held by another full CI job. That duplicate queued run was cancelled; the launcher must rerun this unchanged test as part of full parity. This duration gate remains unresolved. The 10,000-record row-budget test took 113.36 seconds, within its existing 135-second exception. No timeout or exception was changed.
- Final Gate A: **PASS**, 1,000 records traced; 2.935 statements and 0.087 commits per record, controls included. The observed publication maximum was 236 touches per step; the new coded/family tests independently enforce 256 touches, 24 rows per read and 24 touches per primitive.
- Targeted Ruff and whitespace checks passed. The private-term scan passed on all nine changed paths. The working-tree publication scan passed with `--skip-history` after generated scratch artifacts and Ruff caches were moved out of the publishable tree; this does not claim a history scan.

| Fresh-process tracemalloc profile | Base ceiling (MiB) | Final peak (MiB) | Result |
| --- | ---: | ---: | --- |
| 500 records | 7.376 | 4.277 | Pass |
| 1,000 records | 11.529 | 6.843 | Pass |

Full CI parity is assigned to the launcher after this work finishes. Acceptance 12 remains open: no full-parity result, zero `not_run_unexpected` claim, clean duration-gate claim or readiness approval is made here.

Synthetic fixtures only. No push, merge, force-push, hook bypass, schema change, timeout change, durability change, skip or xfail was introduced.
