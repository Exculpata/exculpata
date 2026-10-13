"""Durable admission, logical storage charges, and legacy full-text migration."""
from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3

from .workspace_store import WorkspaceProblem

RUN_OVERHEAD_BYTES = 4_096
SOURCE_OVERHEAD_BYTES = 65_536  # Also reserves bounded source decisions and human notes.
ROW_OVERHEAD_BYTES = 512  # Conservative row/index allowance, in addition to UTF-8 fields.
MAX_LOCATOR_BYTES = 2_048


class TextReviewLimit(WorkspaceProblem):
    pass


@dataclass(frozen=True)
class TextReviewAdmission:
    actor_active: int = 1
    matter_active: int = 1
    instance_active: int = 4
    actor_characters: int = 10_000_000
    matter_characters: int = 10_000_000
    instance_characters: int = 40_000_000
    actor_calls: int = 5_000
    matter_calls: int = 5_000
    instance_calls: int = 20_000
    actor_ledger_bytes: int = 128 * 1024 * 1024
    matter_ledger_bytes: int = 256 * 1024 * 1024
    instance_ledger_bytes: int = 512 * 1024 * 1024
    actor_retained: int = 20
    matter_retained: int = 40
    instance_retained: int = 200


DEFAULT_TEXT_ADMISSION = TextReviewAdmission()


def admit_locked(db, matter_id, actor_id, policy, *, exclude_run=None):
    """The caller holds BEGIN IMMEDIATE; no process-local semaphore is authority."""
    for scope, clause, values in (
        ('actor', 'r.actor_id=?', (actor_id,)),
        ('matter', 'r.matter_id=?', (matter_id,)),
        ('instance', '1=1', ()),
    ):
        row = db.execute(
            "SELECT count(*) AS retained,"
            "COALESCE(sum(CASE WHEN r.state IN ('queued','running') THEN 1 ELSE 0 END),0) AS active,"
            "COALESCE(sum(CASE WHEN r.state IN ('queued','running') THEN b.max_characters ELSE 0 END),0) AS characters,"
            "COALESCE(sum(CASE WHEN r.state IN ('queued','running') THEN b.max_calls ELSE 0 END),0) AS calls,"
            "COALESCE(sum(CASE WHEN r.state IN ('queued','running') THEN b.max_ledger_bytes ELSE b.ledger_bytes END),0) AS ledger_bytes "
            "FROM workbench_text_review_budget b JOIN workbench_review_run r USING(run_id) WHERE " + clause
            + (" AND r.run_id!=?" if exclude_run else ""),
            (*values, *((exclude_run,) if exclude_run else ())),
        ).fetchone()
        requested = {'active': 1, 'retained': 1, 'characters': policy.max_characters,
                     'calls': policy.max_calls, 'ledger_bytes': policy.max_ledger_bytes}
        for resource, amount in requested.items():
            if row[resource] + amount > getattr(DEFAULT_TEXT_ADMISSION, f'{scope}_{resource}'):
                raise TextReviewLimit(
                    f"Full-text {scope} {resource.replace('_', ' ')} capacity is reserved or exhausted. "
                    "Wait for active work, or export and delete an unneeded saved text review before retrying."
                )


def policy_for(db, run_id):
    from .full_text_review import TextReviewPolicy
    row = db.execute("SELECT policy_json FROM workbench_text_review WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(run_id)
    return TextReviewPolicy(**json.loads(row[0]))


def reacquire_locked(db, run, policy):
    budget = db.execute("SELECT * FROM workbench_text_review_budget WHERE run_id=?", (run.run_id,)).fetchone()
    if budget is None:
        return
    if budget['legacy']:
        raise TextReviewLimit("This saved text review predates resource admission. Keep its ledger and start a new run.")
    if budget['limit_reason']:
        raise TextReviewLimit("This text review reached its frozen resource limit. Keep its ledger and use a smaller source set in a new run.")
    admit_locked(db, run.matter_id, run.actor_id, policy, exclude_run=run.run_id)
    charge_control_locked(db, run.run_id)


def charge_control_locked(db, run_id):
    """Reserve the next recovery/retry, claim and terminal event rows."""
    if not db.execute('SELECT 1 FROM workbench_text_review_budget WHERE run_id=?',(run_id,)).fetchone():
        return
    changed = db.execute("UPDATE workbench_text_review_budget SET ledger_bytes=ledger_bytes+2048 "
        "WHERE run_id=? AND legacy=0 AND ledger_bytes+2048<=max_ledger_bytes AND limit_reason=''",(run_id,)).rowcount
    if changed != 1:
        raise TextReviewLimit('Full-text recovery storage limit reached. Export and retain this ledger; start a new run with a smaller source set.')


def create_budget_locked(db, run_id, policy):
    if policy.max_ledger_bytes < RUN_OVERHEAD_BYTES:
        raise TextReviewLimit("The full-text ledger limit cannot hold its control record.")
    db.execute("INSERT INTO workbench_text_review_budget(run_id,max_characters,max_calls,max_ledger_bytes,ledger_bytes) VALUES(?,?,?,?,?)",
               (run_id, policy.max_characters, policy.max_calls, policy.max_ledger_bytes, RUN_OVERHEAD_BYTES))


def sql_limit(exc):
    if isinstance(exc, sqlite3.IntegrityError) and 'full-text ledger storage limit' in str(exc):
        return TextReviewLimit("Full-text ledger storage limit reached. Saved outcomes remain; unprocessed text is a coverage gap.")
    return None


def source_count_locked(db, matter_id, policy, *, scope_clause='', scope_parameters=()):
    count = db.execute("SELECT count(*) FROM (SELECT 1 FROM workbench_source_catalog c WHERE c.matter_id=?"
                       + scope_clause + " LIMIT ?)", (matter_id, *scope_parameters, policy.max_sources + 1)).fetchone()[0]
    if count > policy.max_sources:
        raise TextReviewLimit(f"Full-text review admits at most {policy.max_sources:,} frozen sources. Choose a smaller source set.")
    if RUN_OVERHEAD_BYTES + count * SOURCE_OVERHEAD_BYTES > policy.max_ledger_bytes:
        raise TextReviewLimit("The selected sources exceed the frozen full-text ledger storage reservation. Choose a smaller source set.")


def charge_inventory_locked(db, run_id, text_chars, chunk_count):
    policy = policy_for(db, run_id)
    units = db.execute("SELECT COALESCE(sum(count),0),COALESCE(sum(characters),0) FROM workbench_text_review_counter WHERE run_id=? AND kind='unit'", (run_id,)).fetchone()
    chunks = db.execute("SELECT COALESCE(sum(count),0) FROM workbench_text_review_counter WHERE run_id=? AND kind='chunk'", (run_id,)).fetchone()[0]
    if units[0] + 1 > policy.max_units:
        raise TextReviewLimit("Full-text unit inventory limit reached. The remaining denominator is unresolved.")
    if units[1] + text_chars > policy.max_characters:
        raise TextReviewLimit("Full-text character limit reached. The remaining denominator is unresolved.")
    if chunks + chunk_count > policy.max_calls:
        raise TextReviewLimit("Full-text range inventory exceeds the frozen classifier-call limit. The remaining denominator is unresolved.")


def charge_call_locked(db, run_id):
    """Charge before dispatch. A crash or cancelled reply never refunds model work."""
    changed = db.execute("UPDATE workbench_text_review_budget SET calls_used=calls_used+1 WHERE run_id=? AND legacy=0 AND calls_used<max_calls", (run_id,)).rowcount
    if changed != 1:
        raise TextReviewLimit("Full-text classifier-call limit reached. Saved outcomes remain; unprocessed ranges are coverage gaps.")


def backfill_legacy_ledgers(workspace):
    """Preserve 0028 outcomes while refusing automatic unbudgeted legacy resume."""
    from .full_text_review import DEFAULT_TEXT_POLICY
    db = workspace.connection
    with db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute("SELECT t.run_id FROM workbench_text_review t LEFT JOIN workbench_text_review_budget b USING(run_id) WHERE b.run_id IS NULL AND t.run_id IN (SELECT run_id FROM workbench_review_run WHERE matter_id IN (SELECT matter_id FROM workbench_matter_lifecycle WHERE state='active'))").fetchall()
        for row in rows:
            run_id = row[0]
            # Strip only duplicated source text. Preserve each stored locator,
            # digest and finding; large/invalid locators fail closed on use.
            db.execute("UPDATE workbench_text_review_unit SET citation_json=CASE WHEN json_valid(citation_json) "
                       "THEN json_remove(citation_json,'$.excerpt') ELSE citation_json END WHERE run_id=?", (run_id,))
            create_budget_locked(db, run_id, DEFAULT_TEXT_POLICY)
            db.execute("UPDATE workbench_text_review_budget SET legacy=1 WHERE run_id=?", (run_id,))
            total = RUN_OVERHEAD_BYTES
            fields_by_kind = {
                'source': (SOURCE_OVERHEAD_BYTES, ('run_id','document_id','source_version_id','source_basis_digest','source_name','source_kind','source_state','extraction_note','state')),
                'unit': (ROW_OVERHEAD_BYTES, ('run_id','document_id','location','unit_digest','state','citation_json')),
                'chunk': (ROW_OVERHEAD_BYTES, ('run_id','document_id','state','decision','rationale','finding_key')),
            }
            for kind, (overhead, fields) in fields_by_kind.items():
                expression = str(overhead) + ''.join(f'+length(CAST({field} AS BLOB))' for field in fields)
                table = 'workbench_text_review_' + kind
                total += db.execute(f"SELECT COALESCE(sum({expression}),0) FROM {table} WHERE run_id=?", (run_id,)).fetchone()[0]
                chars = 'coverage_end-coverage_start' if kind == 'chunk' else 'text_chars'
                db.execute(f"INSERT INTO workbench_text_review_counter(run_id,kind,state,count,characters) SELECT run_id,?,state,count(*),sum({chars}) FROM {table} WHERE run_id=? GROUP BY state", (kind, run_id))
            for label, condition in (('unresolved', "source_state='ready' AND inventory_sealed=0"),
                                     ('zero_units', "source_state='ready' AND inventory_sealed=1 AND unit_count=0")):
                db.execute("INSERT INTO workbench_text_review_counter(run_id,kind,state,count,characters) "
                           f"SELECT ?,'inventory',?,count(*),0 FROM workbench_text_review_source WHERE run_id=? AND {condition}", (run_id, label, run_id))
            # Legacy decisions may contain long copied excerpts and their event
            # history predates control charges. Account actual UTF-8 fields in
            # addition to source reservations; do not discard saved evidence.
            total += db.execute("SELECT COALESCE(sum(512+length(CAST(citations_json AS BLOB))+"
                "length(CAST(rationale AS BLOB))+length(CAST(error_message AS BLOB))+length(CAST(human_note AS BLOB))),0) "
                "FROM workbench_review_decision WHERE run_id=?",(run_id,)).fetchone()[0]
            total += db.execute("SELECT count(*)*2048 FROM workbench_review_event WHERE run_id=?",(run_id,)).fetchone()[0]
            db.execute("UPDATE workbench_text_review_budget SET ledger_bytes=?,characters_used=(SELECT COALESCE(sum(text_chars),0) FROM workbench_text_review_unit WHERE run_id=?),"
                       "calls_used=(SELECT count(*) FROM workbench_text_review_chunk WHERE run_id=? AND state IN ('processed','failed')) WHERE run_id=?", (total, run_id, run_id, run_id))
            db.execute("UPDATE workbench_review_run SET state='failed',stage='failed',worker_id=NULL,message='Legacy text review retained; start a new run with resource admission.' WHERE run_id=? AND state IN ('queued','running')", (run_id,))
