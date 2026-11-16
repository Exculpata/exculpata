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

The counter run overlapped unrelated validation. Its elapsed time and instrumented peak are not timing or memory acceptance evidence. Uninstrumented fresh-process memory checks remain required.

| Metric | Before | After |
| --- | ---: | ---: |
| Project steps | 1001 | Pending |
| Project statements/record, including control | 47.048 | Pending |
| Project commits/record | 1.001 | Pending |
| Project control statements/step, excluding commit | 13 | Pending |
| Order control statements/step, excluding commit | 18 | Unchanged scope |

### Statement shapes

Counts below are measured, not inferred. All project rows use the 1,000-record denominator. Commit is listed separately and is excluded from the statement total, consistently with the existing harness.

| Phase | Kind | SQL shape | Before count | Before/record | After count |
| --- | --- | --- | ---: | ---: | ---: |
| project | control | `SELECT event_id FROM workbench_discovery_code_event WHERE matter_id=? ORDER BY event_id DESC LIMIT 1` | 3003 | 3.003 | Pending |
| project | control | `SELECT COUNT(*),COALESCE(SUM(active),0) FROM workbench_discovery_code WHERE matter_id=?` | 3003 | 3.003 | Pending |
| project | control | `SELECT COUNT(*),MAX(updated_at),SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END) FROM workbench_discovery_production WHERE matter_id=?` | 3003 | 3.003 | Pending |
| project | control | `SELECT epoch,revision,generation,definition_digest,key_version,total,indexed,pending,errors,phase,summaries,checkpoint FROM workbench_discovery_index_state WHERE matter_id=?` | 2002 | 2.002 | Pending |
| project | work | `SELECT * FROM workbench_source_catalog WHERE matter_id=? AND document_id=?` | 5002 | 5.002 | Pending |
| project | work | `SELECT * FROM workbench_discovery_production WHERE matter_id=? AND document_id=? AND status<>'stale'` | 2001 | 2.001 | Pending |
| project | work | `SELECT COUNT(*),MAX(created_at) FROM workbench_discovery_identifier WHERE matter_id=?` | 1001 | 1.001 | Pending |
| project | work | `SELECT event_id FROM workbench_discovery_code_event WHERE matter_id=? ORDER BY event_id DESC LIMIT 1` | 1001 | 1.001 | Pending |
| project | work | `SELECT COUNT(*),COALESCE(SUM(active),0) FROM workbench_discovery_code WHERE matter_id=?` | 1001 | 1.001 | Pending |
| project | work | `SELECT DISTINCT t.receipt_id,i.relative_path FROM workbench_intake_transfer t JOIN workbench_upload_item u ON u.upload_item_id=t.upload_item_id AND u.matter_id=t.matter_id JOIN workbench_intake_item i ON i.receipt_id=t.receipt_id AND i.matter_id=t.matter_id AND i.ordinal=t.ordinal JOIN workbench_source_catalog c ON c.matter_id=u.matter_id AND c.document_id=u.document_id WHERE t.matter_id=? AND u.document_id=? AND t.source_version_id=? AND c.version_id=? AND u.state='queued' AND c.origin='upload' AND i.relative_path=u.relative_path AND c.relative_path=u.relative_path ORDER BY t.receipt_id,i.relative_path LIMIT 2` | 3001 | 3.001 | Pending |
| project | control | `BEGIN IMMEDIATE` | 1001 | 1.001 | Pending |
| project | work | `SELECT source_payload,payload,cache_payload,projection_fingerprint,readiness FROM workbench_discovery_index_item WHERE matter_id=? AND document_id=?` | 2004 | 2.004 | Pending |
| project | work | `SELECT payload FROM source_document WHERE document_id IN (?)` | 1001 | 1.001 | Pending |
| project | work | `SELECT document_id,identifier FROM workbench_discovery_identifier WHERE matter_id=? AND document_id IN (?)` | 2002 | 2.002 | Pending |
| project | work | `SELECT order_prefix FROM workbench_discovery_index_load_row WHERE matter_id=? AND attached_document=? ORDER BY order_prefix LIMIT 1` | 1001 | 1.001 | Pending |
| project | work | `SELECT e.document_id,e.code_id,c.label,c.active,e.created_by,e.created_at,c.position FROM workbench_discovery_code_event e JOIN workbench_discovery_code c ON c.matter_id=e.matter_id AND c.code_id=e.code_id WHERE e.matter_id=? AND e.document_id IN (?) AND e.action='apply' AND e.event_id=(SELECT MAX(n.event_id) FROM workbench_discovery_code_event n WHERE n.matter_id=e.matter_id AND n.document_id=e.document_id AND n.code_id=e.code_id) ORDER BY c.position,c.code_id,e.document_id LIMIT ?` | 1001 | 1.001 | Pending |
| project | work | `UPDATE workbench_discovery_index_item SET payload=?,cache_payload=?,projection_fingerprint=?,readiness=?,ready_generation=? WHERE matter_id=? AND document_id=?` | 1001 | 1.001 | Pending |
| project | work | `SELECT document_id FROM workbench_discovery_index_item WHERE matter_id=? AND seen_generation=? AND document_id>? ORDER BY document_id LIMIT 1` | 1004 | 1.004 | Pending |
| project | work | `SELECT attempt_id FROM workbench_discovery_dat_record WHERE matter_id=? AND document_id=? LIMIT 1` | 1000 | 1.000 | Pending |
| project | control | `UPDATE workbench_discovery_index_state SET epoch=?,revision=?,generation=?,definition_digest=?,key_version=?,total=?,indexed=?,pending=?,errors=?,phase=?,summaries=?,checkpoint=? WHERE matter_id=?` | 1001 | 1.001 | Pending |
| project | control | `IMPLICIT COMMIT` | 1001 | 1.001 | Pending |
| project | work | `SELECT 1 FROM workbench_discovery_production WHERE matter_id=? AND root_key=? AND status='building' LIMIT 1` | 2000 | 2.000 | Pending |
| project | work | `SELECT 1 FROM workbench_discovery_dat_record r JOIN workbench_discovery_production p ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id WHERE r.matter_id=? AND r.document_id=? AND p.status='building' LIMIT 1` | 2000 | 2.000 | Pending |
| project | work | `SELECT 1 FROM workbench_discovery_production WHERE matter_id=? AND root_key=? AND source_format IN ('csv','tsv') AND status='failed' LIMIT 1` | 2000 | 2.000 | Pending |
| project | work | `SELECT 1 FROM workbench_discovery_dat_record r JOIN workbench_discovery_production p ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id WHERE r.matter_id=? AND r.document_id=? AND p.status IN ('building','complete') AND r.generation=p.generation AND r.association_status NOT IN ('matched','unmatched','pending') LIMIT 1` | 2000 | 2.000 | Pending |
| project | work | `SELECT r.*,p.document_id AS source_id,p.version_id,p.digest,p.name,p.relative_path,p.source_format,p.root_key,p.status AS attempt_status,p.generation AS attempt_generation FROM workbench_discovery_dat_record r JOIN workbench_discovery_production p ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id WHERE r.matter_id=? AND r.document_id=? AND p.status='complete' AND r.generation=p.generation ORDER BY CASE p.source_format WHEN ? THEN 0 WHEN ? THEN 1 WHEN ? THEN 2 ELSE 3 END,r.attempt_id,r.ordinal LIMIT ?` | 1000 | 1.000 | Pending |
| project | work | `SELECT * FROM workbench_discovery_production WHERE matter_id=? AND attempt_id=?` | 1000 | 1.000 | Pending |
| project | work | `SELECT r.*,p.document_id AS source_id,p.version_id,p.digest,p.name,p.relative_path,p.source_format,p.root_key,p.status AS attempt_status,p.generation AS attempt_generation FROM workbench_discovery_opt_page r JOIN workbench_discovery_production p ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id WHERE r.matter_id=? AND r.document_id=? AND r.association_status='matched' AND p.status='complete' AND r.generation=p.generation ORDER BY r.attempt_id,r.ordinal LIMIT 1` | 1000 | 1.000 | Pending |
| project | work | `SELECT field_key,direction FROM workbench_discovery_index_value WHERE matter_id=? AND document_id=? AND (field_key>? OR (field_key=? AND direction>?)) ORDER BY field_key,direction LIMIT 8` | 2 | 0.002 | Pending |
| project | work | `INSERT INTO workbench_discovery_index_value VALUES (?,?,?,?,?,?,?,?)` | 8 | 0.008 | Pending |
| project | work | `UPDATE workbench_discovery_index_item SET cache_payload=? WHERE matter_id=? AND document_id=?` | 2 | 0.002 | Pending |
| project | work | `UPDATE workbench_discovery_index_item SET readiness=? WHERE matter_id=? AND document_id=?` | 2 | 0.002 | Pending |
| order | control | `SELECT event_id FROM workbench_discovery_code_event WHERE matter_id=? ORDER BY event_id DESC LIMIT 1` | 1200 | 1.200 | Pending |
| order | control | `SELECT COUNT(*),COALESCE(SUM(active),0) FROM workbench_discovery_code WHERE matter_id=?` | 1200 | 1.200 | Pending |
| order | control | `SELECT COUNT(*),MAX(updated_at),SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END) FROM workbench_discovery_production WHERE matter_id=?` | 1200 | 1.200 | Pending |
| order | control | `SELECT epoch,revision,generation,definition_digest,key_version,total,indexed,pending,errors,phase,summaries,checkpoint FROM workbench_discovery_index_state WHERE matter_id=?` | 800 | 0.800 | Pending |
| order | control | `PRAGMA journal_mode` | 400 | 0.400 | Pending |
| order | control | `PRAGMA wal_autocheckpoint` | 400 | 0.400 | Pending |
| order | control | `PRAGMA page_size` | 400 | 0.400 | Pending |
| order | control | `PRAGMA wal_autocheckpoint=260616` | 400 | 0.400 | Pending |
| order | control | `BEGIN IMMEDIATE` | 400 | 0.400 | Pending |
| order | control | `UPDATE workbench_discovery_index_state SET epoch=?,revision=?,generation=?,definition_digest=?,key_version=?,total=?,indexed=?,pending=?,errors=?,phase=?,summaries=?,checkpoint=? WHERE matter_id=?` | 400 | 0.400 | Pending |
| order | control | `IMPLICIT COMMIT` | 400 | 0.400 | Pending |
| order | control | `PRAGMA wal_autocheckpoint=1000` | 400 | 0.400 | Pending |

## Validation

Implementation and acceptance checks pending. No full parity result is claimed; the launcher runs full CI parity after completion.
