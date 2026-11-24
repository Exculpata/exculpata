"""Durable coverage and incremental findings for every extracted text range."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import sqlite3
from typing import Iterable

from .generation_matter import generation_matter_scope
from .workspace_store import WorkspaceProblem


@dataclass(frozen=True)
class TextReviewPolicy:
    packet_chars: int = 6_000
    overlap_chars: int = 256
    output_tokens: int = 1_200
    max_characters: int = 10_000_000
    max_calls: int = 5_000
    max_ledger_bytes: int = 64 * 1024 * 1024
    max_sources: int = 1_000
    max_units: int = 20_000

    def __post_init__(self):
        if type(self.packet_chars) is not int or not 512 <= self.packet_chars <= 6_000:
            raise ValueError("Text review packets must contain 512 to 6,000 characters.")
        if type(self.overlap_chars) is not int or not 0 <= self.overlap_chars < self.packet_chars // 2:
            raise ValueError("Text overlap must be smaller than half a packet.")
        if type(self.output_tokens) is not int or not 1 <= self.output_tokens <= 1_200:
            raise ValueError("Text review output must be between 1 and 1,200 tokens.")
        for name, ceiling in (("max_characters", 10_000_000), ("max_calls", 5_000),
                              ("max_ledger_bytes", 64 * 1024 * 1024),
                              ("max_sources", 1_000), ("max_units", 20_000)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError(f"Text review {name} must be between 1 and {ceiling}.")



DEFAULT_TEXT_POLICY = TextReviewPolicy()


def text_ranges(text: str, policy: TextReviewPolicy = DEFAULT_TEXT_POLICY):
    """Yield canonical coverage plus overlapping context; no suffix is omitted."""
    width = policy.packet_chars - 2 * policy.overlap_chars
    for start in range(0, len(text), width):
        end = min(len(text), start + width)
        packet_start, packet_end = max(0, start - policy.overlap_chars), min(len(text), end + policy.overlap_chars)
        yield start, end, packet_start, packet_end


class FullTextReviewLedger:
    def __init__(self, workspace):
        self.workspace = workspace

    def enabled(self, run_id):
        with self.workspace._lock:
            return self.workspace.connection.execute(
                "SELECT 1 FROM workbench_text_review WHERE run_id=?", (run_id,)
            ).fetchone() is not None

    def enable_locked(self, run_id, *, policy=None, scope_clause="", scope_parameters=()):
        """Called inside the source-run admission transaction."""
        from .full_text_review_budget import create_budget_locked
        policy = policy or DEFAULT_TEXT_POLICY
        db = self.workspace.connection
        run = db.execute("SELECT * FROM workbench_review_run WHERE run_id=?", (run_id,)).fetchone()
        db.execute("INSERT INTO workbench_text_review(run_id,policy_json) VALUES (?,?)",
                   (run_id, json.dumps(asdict(policy), sort_keys=True)))
        create_budget_locked(db, run_id, policy)
        db.execute(
            "INSERT INTO workbench_text_review_source(run_id,document_id,source_version_id,source_basis_digest,"
            "source_name,source_kind,source_state,extraction_note,reported_pages,state) "
            "SELECT ?,c.document_id,c.version_id,c.content_basis_digest,c.display_name,c.kind,c.source_state,"
            "c.state_label,c.page_count,CASE WHEN c.source_state='ready' THEN 'pending' ELSE 'unavailable' END "
            "FROM workbench_source_catalog c WHERE c.matter_id=?" + scope_clause,
            (run_id, run['matter_id'], *scope_parameters),
        )

    @contextmanager
    def writing(self, run, *, allow_cancel=False):
        store = self.workspace
        with store._lock, store.connection:
            store.connection.execute("BEGIN IMMEDIATE")
            current = store.connection.execute("SELECT * FROM workbench_review_run WHERE run_id=?", (run.run_id,)).fetchone()
            if (current is None or current['state'] != 'running' or current['attempts'] != run.attempts
                or current['worker_id'] != run.worker_id or (current['cancellation_requested'] and not allow_cancel)):
                raise WorkspaceProblem("This text review attempt is no longer active.")
            store.membership(run.matter_id, run.actor_id)
            try:
                yield store.connection
            except Exception as exc:
                from .full_text_review_budget import sql_limit
                limit = sql_limit(exc)
                if limit:
                    raise limit from exc
                raise

    def source_current_locked(self, db, run_id, document_id):
        return db.execute("SELECT 1 FROM workbench_text_review_source s JOIN workbench_review_run r ON r.run_id=s.run_id "
            "JOIN workbench_source_catalog c ON c.matter_id=r.matter_id AND c.document_id=s.document_id "
            "WHERE s.run_id=? AND s.document_id=? AND c.source_state='ready' AND c.version_id=s.source_version_id "
            "AND s.source_basis_digest!='' AND c.content_basis_digest=s.source_basis_digest", (run_id, document_id)).fetchone() is not None

    def inventory(self, run, decision, units: Iterable, *, current_source, citation_for=None):
        """Stage metadata in bounded transactions; only a sealed inventory is read."""
        with self.writing(run) as db:
            source = db.execute("SELECT state FROM workbench_text_review_source WHERE run_id=? AND document_id=?",
                                (run.run_id, decision.document_id)).fetchone()
            if source is None:
                raise WorkspaceProblem("The frozen text source is missing.")
            if source['state'] != 'pending':
                return
            if not self.source_current_locked(db, run.run_id, decision.document_id) or not current_source():
                self._invalidate_locked(db, run.run_id, decision.document_id)
                return
            # An interrupted inventory has no findings; rebuild its metadata.
            db.execute("DELETE FROM workbench_text_review_unit WHERE run_id=? AND document_id=?", (run.run_id, decision.document_id))
        from .full_text_review_budget import charge_inventory_locked, policy_for
        with self.workspace._lock:
            policy = policy_for(self.workspace.connection, run.run_id)
        unit_count = chars = chunks = empty = 0
        for ordinal, unit in enumerate(units, 1):
            digest = hashlib.sha256(unit.text.encode('utf-8')).hexdigest()
            width = policy.packet_chars - 2 * policy.overlap_chars
            chunk_count = (len(unit.text) + width - 1) // width
            with self.writing(run) as db:
                charge_inventory_locked(db, run.run_id, len(unit.text), chunk_count)
                db.execute("INSERT INTO workbench_text_review_unit(run_id,document_id,unit_ordinal,unit_number,"
                           "location,unit_digest,text_chars,chunk_count,state,citation_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (run.run_id, decision.document_id, ordinal, unit.number, unit.location, digest,
                            len(unit.text), chunk_count, 'pending' if chunk_count else 'empty',
                            json.dumps(citation_for(unit, ordinal) if citation_for else {}, ensure_ascii=False, separators=(',', ':'))))
                cursor_start = db.execute("SELECT COALESCE(max(cursor),0)+1 FROM workbench_text_review_chunk WHERE run_id=?", (run.run_id,)).fetchone()[0]
                db.executemany("INSERT INTO workbench_text_review_chunk(run_id,document_id,unit_ordinal,chunk_ordinal,cursor,"
                               "coverage_start,coverage_end,packet_start,packet_end) VALUES (?,?,?,?,?,?,?,?,?)",
                               ((run.run_id, decision.document_id, ordinal, number, cursor_start + number - 1, *bounds)
                                for number, bounds in enumerate(text_ranges(unit.text, policy), 1)))
            unit_count += 1
            chars += len(unit.text)
            chunks += chunk_count
            empty += not chunk_count
        with self.writing(run) as db:
            if not self.source_current_locked(db, run.run_id, decision.document_id) or not current_source():
                self._invalidate_locked(db, run.run_id, decision.document_id)
                return
            db.execute("UPDATE workbench_text_review_source SET state='inventoried',inventory_sealed=1,unit_count=?,text_chars=?,chunk_count=?,empty_units=? "
                       "WHERE run_id=? AND document_id=?", (unit_count, chars, chunks, empty, run.run_id, decision.document_id))

    def _invalidate_locked(self, db, run_id, document_id):
        db.execute("UPDATE workbench_text_review_source SET state='invalidated' WHERE run_id=? AND document_id=?", (run_id, document_id))
        db.execute("UPDATE workbench_text_review_unit SET state='invalidated',citation_json='{}' WHERE run_id=? AND document_id=?", (run_id, document_id))
        db.execute("UPDATE workbench_text_review_chunk SET state='invalidated',decision='',rationale='',finding_key='' "
                   "WHERE run_id=? AND document_id=?", (run_id, document_id))

    def invalidate(self, run, document_id):
        with self.writing(run, allow_cancel=True) as db:
            self._invalidate_locked(db, run.run_id, document_id)

    def unit_chunks(self, run_id, document_id, ordinal):
        with self.workspace._lock:
            return tuple(dict(row) for row in self.workspace.connection.execute(
                "SELECT c.*,u.unit_digest,u.text_chars FROM workbench_text_review_chunk c JOIN workbench_text_review_unit u "
                "USING(run_id,document_id,unit_ordinal) WHERE c.run_id=? AND c.document_id=? AND c.unit_ordinal=? ORDER BY c.chunk_ordinal",
                (run_id, document_id, ordinal)))

    def record(self, run, decision, chunk, *, state, label='', rationale='', current_source):
        if state not in {'processed', 'failed'} or label not in {'', 'include', 'exclude'}:
            raise ValueError('Invalid text review outcome')
        if state == 'processed' and not label:
            raise ValueError('Processed text needs a verified outcome')
        with self.writing(run) as db:
            if not self.source_current_locked(db, run.run_id, decision.document_id) or not current_source():
                self._invalidate_locked(db, run.run_id, decision.document_id)
                return False
            key = hashlib.sha256((' '.join(rationale.casefold().split())).encode()).hexdigest() if label == 'include' else ''
            changed = db.execute("UPDATE workbench_text_review_chunk SET state=?,decision=?,rationale=?,finding_key=?,attempt=? "
                "WHERE run_id=? AND document_id=? AND unit_ordinal=? AND chunk_ordinal=? AND state='pending'",
                (state, label, rationale[:4_000], key, run.attempts, run.run_id, decision.document_id,
                 chunk['unit_ordinal'], chunk['chunk_ordinal'])).rowcount
            db.execute("UPDATE workbench_text_review_unit SET state=CASE "
                "WHEN EXISTS(SELECT 1 FROM workbench_text_review_chunk c WHERE c.run_id=workbench_text_review_unit.run_id "
                "AND c.document_id=workbench_text_review_unit.document_id AND c.unit_ordinal=workbench_text_review_unit.unit_ordinal AND c.state='pending') THEN 'pending' "
                "WHEN EXISTS(SELECT 1 FROM workbench_text_review_chunk c WHERE c.run_id=workbench_text_review_unit.run_id "
                "AND c.document_id=workbench_text_review_unit.document_id AND c.unit_ordinal=workbench_text_review_unit.unit_ordinal AND c.state='failed') THEN 'failed' ELSE 'processed' END "
                "WHERE run_id=? AND document_id=? AND unit_ordinal=?", (run.run_id, decision.document_id, chunk['unit_ordinal']))
            return bool(changed)

    def source_summary(self, run_id, document_id):
        with self.workspace._lock:
            db = self.workspace.connection
            source = dict(db.execute("SELECT * FROM workbench_text_review_source WHERE run_id=? AND document_id=?", (run_id, document_id)).fetchone())
            source['states'] = dict(db.execute("SELECT state,count(*) FROM workbench_text_review_chunk WHERE run_id=? AND document_id=? GROUP BY state", (run_id, document_id)))
            source['included'] = db.execute("SELECT count(DISTINCT unit_ordinal || ':' || finding_key) FROM workbench_text_review_chunk WHERE run_id=? AND document_id=? AND decision='include' AND state='processed'", (run_id, document_id)).fetchone()[0]
            source['citations'] = tuple(read_locator(row[0]) for row in db.execute(
                "SELECT u.citation_json FROM workbench_text_review_unit u WHERE u.run_id=? AND u.document_id=? AND EXISTS "
                "(SELECT 1 FROM workbench_text_review_chunk c WHERE c.run_id=u.run_id AND c.document_id=u.document_id AND c.unit_ordinal=u.unit_ordinal AND c.decision='include' AND c.state='processed') "
                "ORDER BY u.unit_ordinal LIMIT 12", (run_id, document_id)))
            return source

    def extraction_rows(self, matter_id, actor_id, run_id, *, after='', limit=100, administrator_override=False):
        self.workspace.review_run(matter_id, actor_id, run_id, administrator_override=administrator_override)
        with self.workspace._lock:
            return tuple(dict(row) for row in self.workspace.connection.execute(
                "SELECT * FROM workbench_text_review_source WHERE run_id=? AND document_id>? ORDER BY document_id LIMIT ?",
                (run_id, after, min(500, max(1, int(limit))))))

    def citation(self, matter_id, actor_id, run_id, document_id, unit_ordinal, *, administrator_override=False):
        self.workspace.review_run(matter_id, actor_id, run_id, administrator_override=administrator_override)
        with self.workspace._lock:
            row = self.workspace.connection.execute("SELECT citation_json FROM workbench_text_review_unit WHERE run_id=? AND document_id=? AND unit_ordinal=? AND state!='invalidated'",
                (run_id, document_id, unit_ordinal)).fetchone()
        if row is None:
            raise KeyError(unit_ordinal)
        return read_locator(row[0])

    def coverage(self, matter_id, actor_id, run_id, *, administrator_override=False):
        self.workspace.review_run(matter_id, actor_id, run_id, administrator_override=administrator_override)
        with self.workspace._lock:
            db = self.workspace.connection
            settings = db.execute("SELECT policy_json FROM workbench_text_review WHERE run_id=?", (run_id,)).fetchone()
            if settings is None:
                return None
            counters = tuple(db.execute("SELECT kind,state,count,characters FROM workbench_text_review_counter WHERE run_id=?", (run_id,)))
            budget = dict(db.execute("SELECT * FROM workbench_text_review_budget WHERE run_id=?", (run_id,)).fetchone())
        counts = {kind: {row['state']: row['count'] for row in counters if row['kind'] == kind and row['count']} for kind in ('source', 'unit', 'chunk', 'inventory')}
        unresolved = counts['inventory'].get('unresolved', 0)
        zero_units = counts['inventory'].get('zero_units', 0)
        characters = sum(row['characters'] for row in counters if row['kind'] == 'chunk')
        processed = sum(row['characters'] for row in counters if row['kind'] == 'chunk' and row['state'] == 'processed')
        has_gaps = bool(unresolved or zero_units or budget['legacy'] or budget['limit_reason'] or
            counts['source'].get('unavailable') or counts['source'].get('invalidated') or
            any(counts['unit'].get(state) for state in ('failed','pending','empty','invalidated')))
        notice = 'Every extracted text range has its own recorded outcome. Missing extraction and failed analysis remain separate. Completion does not establish that every relevant fact was recognized.'
        # Conditional wording covers frozen/removed sources without a population scan.
        from .pdf_coverage import PDF_SAVED_RESULT_NOTICE
        notice += ' ' + PDF_SAVED_RESULT_NOTICE
        return {'mode': 'full_text', 'policy': json.loads(settings[0]), 'sources': counts['source'], 'units': counts['unit'],
                'chunks': counts['chunk'], 'inventoried_characters': characters, 'processed_characters': processed,
                'inventory_complete': unresolved == 0, 'unresolved_inventory_sources': unresolved,
                'zero_unit_sources': zero_units, 'budget': budget, 'has_gaps': has_gaps,
                'notice': notice}

    def charge_call(self, run):
        from .full_text_review_budget import charge_call_locked
        with self.writing(run) as db:
            charge_call_locked(db, run.run_id)

    def delete(self, matter_id, actor_id, run_id):
        with self.workspace._lock, self.workspace.connection:
            self.workspace.connection.execute('BEGIN IMMEDIATE')
            run = self.workspace.review_run(matter_id, actor_id, run_id)
            if run.actor_id != self.workspace.membership(matter_id, actor_id).principal_id:
                raise WorkspaceProblem('Only the creator can delete this saved text review.')
            if run.state in ('queued', 'running'):
                raise WorkspaceProblem('Cancel the review and wait for its worker to stop before deleting it.')
            if not self.enabled(run_id):
                raise KeyError(run_id)
            if self.workspace._text_review_export_leases.get(run_id, 0):
                raise WorkspaceProblem('Wait for the active text-review download to finish before deleting this saved review.')
            self.workspace.connection.execute('DELETE FROM workbench_review_run WHERE run_id=?', (run_id,))
            return run

    def rows(self, matter_id, actor_id, run_id, *, after=0, limit=100, administrator_override=False):
        """Keyset-page sealed source inventories within frozen resource limits.

        Final synthesis consumers bind the terminal run/revision; active-run
        callers must refresh pending rows because outcomes can still change.
        """
        self.workspace.review_run(matter_id, actor_id, run_id, administrator_override=administrator_override)
        with self.workspace._lock:
            return tuple(dict(row) for row in self.workspace.connection.execute(
                "SELECT c.*,u.unit_number,u.location,u.unit_digest,u.citation_json,s.source_name,s.source_version_id,s.source_basis_digest "
                "FROM workbench_text_review_chunk c JOIN workbench_text_review_unit u USING(run_id,document_id,unit_ordinal) "
                "JOIN workbench_text_review_source s USING(run_id,document_id) WHERE c.run_id=? AND s.inventory_sealed=1 AND c.cursor>? ORDER BY c.cursor LIMIT ?",
                (run_id, max(0, int(after)), min(500, max(1, int(limit))))))


def _release_text_export_lease(workspace, run_id):
    with workspace._lock:
        leases = workspace._text_review_export_leases
        remaining = leases[run_id] - 1
        if remaining:
            leases[run_id] = remaining
        else:
            del leases[run_id]


@contextmanager
def review_export_snapshot(workspace, matter_id, actor_id, run_id, *, administrator_override=False):
    """Keep a bundle's ledger and adjacent review artifacts on one read snapshot."""
    with workspace._lock:
        workspace.review_run(matter_id, actor_id, run_id, administrator_override=administrator_override)
        guarded = FullTextReviewLedger(workspace).enabled(run_id)
        if guarded:
            leases = workspace._text_review_export_leases
            leases[run_id] = leases.get(run_id, 0) + 1
    db = None
    try:
        db = sqlite3.connect(workspace.path.resolve().as_uri() + '?mode=ro', uri=True, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        yield db
    finally:
        try:
            if db is not None:
                db.close()
        finally:
            if guarded:
                _release_text_export_lease(workspace, run_id)


class _LeasedTextExport:
    """Sequential iterator whose eager run lease also covers an unstarted body."""
    def __init__(self, workspace, run_id, iterator):
        self.workspace = workspace
        self.run_id = run_id
        self.iterator = iterator
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        try:
            return next(self.iterator)
        except BaseException:
            self.close()
            raise

    def close(self):
        with self.workspace._lock:
            if self.closed:
                return
            self.closed = True
        try:
            self.iterator.close()
        finally:
            _release_text_export_lease(self.workspace, self.run_id)


def iter_text_export(workspace, matter_id, actor_id, run_id, format_name='json', *, administrator_override=False, _snapshot=None):
    """Admit a run-specific reader now; exhaust or close it on every exit.

    Admission and creator deletion share the workspace lock. This is deliberately
    an eager factory: a generator would leave a race before its first next().
    Bundle consumers use the same lease and must close on an abandoned export.
    """
    if format_name not in ('json', 'csv'):
        raise ValueError('Choose JSON or CSV for the full-text ledger.')
    with workspace._lock:
        workspace.review_run(matter_id, actor_id, run_id, administrator_override=administrator_override)
        if not FullTextReviewLedger(workspace).enabled(run_id):
            raise KeyError(run_id)
        stream = _LeasedTextExport(workspace, run_id, _iter_text_export(
            workspace, matter_id, actor_id, run_id, format_name, administrator_override=administrator_override, _snapshot=_snapshot))
        leases = workspace._text_review_export_leases
        leases[run_id] = leases.get(run_id, 0) + 1
        return stream


def _iter_text_export(workspace, matter_id, actor_id, run_id, format_name, *, administrator_override, _snapshot=None):
    """Stream one consistent snapshot, including failures and extraction records."""
    import csv
    import io
    workspace.review_run(matter_id, actor_id, run_id, administrator_override=administrator_override)
    # StreamingResponse awaits each next() serially, but successive calls (and
    # close()) may run on different workers. Standalone iterators own their
    # connection; bundle artifacts share their caller-owned read snapshot
    # sequentially. Neither connection is shared with workspace writes.
    db = _snapshot
    if db is None:
        db = sqlite3.connect(workspace.path.resolve().as_uri() + '?mode=ro', uri=True, check_same_thread=False)
        db.row_factory = sqlite3.Row
    try:
        if _snapshot is None:
            db.execute('BEGIN')
        run = db.execute("SELECT r.run_id,r.state,r.criterion_version_id,r.source_set_id,r.created_at,r.finished_at,"
            "v.instructions,v.include_guidance,v.exclude_guidance,t.policy_json FROM workbench_review_run r "
            "JOIN workbench_review_criterion_version v ON v.criterion_version_id=r.criterion_version_id "
            "JOIN workbench_text_review t ON t.run_id=r.run_id WHERE r.matter_id=? AND r.run_id=?", (matter_id, run_id)).fetchone()
        if run is None:
            raise KeyError(run_id)
        def records():
            method = dict(run)
            method['policy'] = json.loads(method.pop('policy_json'))
            notice = 'Full extracted-text coverage, with overlapping context. Canonical character ranges are not double-counted. Failed, empty, unavailable and invalidated work remains incomplete. Recognition of every relevant fact is not established.'
            from .pdf_coverage import PDF_SAVED_RESULT_NOTICE
            notice += ' ' + PDF_SAVED_RESULT_NOTICE
            yield {'record_type': 'method', **method, 'notice': notice}
            budget = db.execute('SELECT * FROM workbench_text_review_budget WHERE run_id=?',(run_id,)).fetchone()
            if budget is not None:
                yield {'record_type': 'budget', **dict(budget)}
            for table, order, kind in (
                ('workbench_text_review_source', 'document_id', 'source'),
                ('workbench_review_decision', 'ordinal,document_id', 'decision'),
                ('workbench_text_review_unit', 'document_id,unit_ordinal', 'unit'),
                ('workbench_text_review_chunk', 'document_id,unit_ordinal,chunk_ordinal', 'range'),
            ):
                cursor = db.execute(f'SELECT * FROM {table} WHERE run_id=? ORDER BY {order}', (run_id,))
                while batch := cursor.fetchmany(100):
                    workspace.review_run(matter_id, actor_id, run_id, administrator_override=administrator_override)
                    for row in batch:
                        record = {'record_type': kind, **dict(row)}
                        for key in ('policy_json', 'citation_json', 'citations_json'):
                            if key in record:
                                raw = record.pop(key)
                                try:
                                    record[key[:-5]] = json.loads(raw)
                                except (ValueError, TypeError):
                                    record[key] = raw
                                    record['locator_error'] = 'Stored locator is malformed; raw evidence retained for export.'
                        yield record
        if format_name == 'json':
            yield b'{"records":['
            for number, record in enumerate(records()):
                yield ((',' if number else '') + json.dumps(record, ensure_ascii=False, separators=(',', ':'))).encode('utf-8')
            yield b']}'
        elif format_name == 'csv':
            output = io.StringIO(newline='')
            writer = csv.writer(output)
            writer.writerow(('Record type', 'Source identifier', 'Unit ordinal', 'Range ordinal', 'State', 'Decision', 'Coverage start', 'Coverage end', 'Saved record JSON'))
            yield output.getvalue().encode('utf-8')
            for record in records():
                output.seek(0); output.truncate(0)
                # All free text is JSON-encoded so cells cannot begin as formulas.
                writer.writerow((record['record_type'], record.get('document_id',''), record.get('unit_ordinal',''),
                    record.get('chunk_ordinal',''), record.get('state',''), record.get('machine_decision', record.get('decision','')),
                    record.get('coverage_start',''), record.get('coverage_end',''), json.dumps(record, ensure_ascii=False)))
                yield output.getvalue().encode('utf-8')
        else:
            raise ValueError('Choose JSON or CSV for the full-text ledger.')
    finally:
        if _snapshot is None:
            db.close()


class _TextDigestMismatch(ValueError):
    pass


def _process_text_source(bench, run, decision, cancelled):
    """Process every frozen unit in order, resuming only unrecorded ranges."""
    from .generation import EvidenceItem, GenerationRejected
    from .workflow_jobs import ReviewDecisionResult, WorkflowFailure

    ledger = FullTextReviewLedger(bench.workspace)
    matter = bench._matter_by_id(run.matter_id)
    store = bench.source_store(matter)

    def source_identity(document):
        if document.units_file:
            path = store.derived / document.units_file
            if not store._units_file_is_safe(document.units_file):
                return None
            metadata = path.stat()
            return (document.version_id, document.state, document.units_file, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        return (document.version_id, document.state, bench._document_content_basis(document))

    with store.mutation_guard():
        try:
            document = store.get(decision.document_id)
            valid = (document.state == 'ready' and document.version_id == decision.source_version_id
                     and decision.source_basis_digest and bench._document_content_basis(document) == decision.source_basis_digest)
            identity = source_identity(document)
        except (KeyError, OSError, RuntimeError):
            valid = False
            identity = None
        if not valid or identity is None:
            ledger.invalidate(run, decision.document_id)
            return ReviewDecisionResult('needs_attention', 'The frozen source is unavailable or changed; its replacement was not analyzed.', (), 'Start a new run for the current source.')

    def current_source():
        try:
            current = store.get(decision.document_id)
            return source_identity(current) == identity
        except (KeyError, OSError, RuntimeError):
            return False

    def safe_point():
        if cancelled():
            raise WorkflowFailure('Review cancelled.')
        bench.workspace.membership(run.matter_id, run.actor_id)

    def citation_for(unit, ordinal):
        return compact_locator(bench._workflow_citation_payload(bench._citation(matter, bench._candidate(matter, document, unit, ordinal))), ordinal, unit.text)

    def inventory_units():
        for unit in document.iter_parsed_units():
            safe_point()
            if hashlib.sha256(unit.text.encode('utf-8')).hexdigest() != unit.excerpt_digest:
                raise _TextDigestMismatch('Extracted source text has a stale digest; repair extraction before review.')
            yield unit

    from .full_text_review_budget import policy_for
    with bench.workspace._lock:
        policy = policy_for(bench.workspace.connection, run.run_id)
    ledger.inventory(run, decision, inventory_units(), current_source=current_source, citation_for=citation_for)
    version = bench.workspace.review_criterion_version(run.matter_id, run.criterion_version_id)
    for ordinal, unit in enumerate(inventory_units(), 1):
        chunks = ledger.unit_chunks(run.run_id, decision.document_id, ordinal)
        if chunks and (chunks[0]['unit_digest'] != hashlib.sha256(unit.text.encode()).hexdigest() or not current_source()):
            ledger.invalidate(run, decision.document_id)
            break
        for chunk in chunks:
            if chunk['state'] != 'pending':
                continue
            safe_point()
            packet = unit.text[chunk['packet_start']:chunk['packet_end']]
            evidence = (EvidenceItem('S1', document.display_name, unit.location, packet,
                        'transcript' if document.media_type.startswith(('audio/', 'video/')) else 'document',
                        document_id=decision.document_id),)
            state, label = 'failed', ''
            rationale = 'This text range could not be classified; direct review is required.'
            ledger.charge_call(run)
            try:
                with generation_matter_scope(bench.workspace, run.matter_id):
                    classified = bench.generator.classify_source(criterion=version.instructions,
                        include_guidance=version.include_guidance, exclude_guidance=version.exclude_guidance, evidence=evidence,
                        output_tokens=policy.output_tokens)
                if classified.decision not in {'include', 'not_identified'} or (classified.decision == 'include' and classified.used_evidence_ids != ('S1',)):
                    raise GenerationRejected('Text classification did not retain exact source support')
                state, label, rationale = 'processed', ('include' if classified.decision == 'include' else 'exclude'), classified.rationale
            except Exception:
                pass
            safe_point()
            with store.mutation_guard():
                if not ledger.record(run, decision, chunk, state=state, label=label, rationale=rationale, current_source=current_source):
                    break
    with store.mutation_guard():
        safe_point()
        if not current_source() or bench._document_content_basis(store.get(decision.document_id)) != decision.source_basis_digest:
            ledger.invalidate(run, decision.document_id)
        summary = ledger.source_summary(run.run_id, decision.document_id)
    states = summary['states']
    incomplete = summary['state'] != 'inventoried' or bool(summary['empty_units']) or not summary['unit_count'] or any(states.get(key) for key in ('failed', 'pending', 'invalidated'))
    label = 'included' if summary['included'] else 'needs_attention' if incomplete else 'excluded'
    rationale = (f"Reviewed {states.get('processed', 0):,} of {summary['chunk_count']:,} extracted text ranges "
        f"across {summary['unit_count']:,} units. {summary['included']:,} distinct supported findings; "
        f"{states.get('failed', 0):,} failed ranges; {states.get('pending', 0):,} pending; "
        f"{summary['empty_units']:,} empty units. See the full-text ledger for every recorded range.")
    return ReviewDecisionResult(label, rationale, summary['citations'],
        'Extraction or text-analysis gaps require direct review.' if incomplete else '')


def process_text_source(bench, run, decision, cancelled):
    from .full_text_review_budget import TextReviewLimit
    from .workflow_jobs import WorkflowFailure
    from .unit_stream import UnitRecordLimit
    try:
        return _process_text_source(bench, run, decision, cancelled)
    except _TextDigestMismatch as exc:
        from .workflow_jobs import ReviewDecisionResult
        FullTextReviewLedger(bench.workspace).invalidate(run, decision.document_id)
        return ReviewDecisionResult('needs_attention', str(exc), (), 'Repair extraction and start a new review.')
    except (TextReviewLimit, UnitRecordLimit) as exc:
        with bench.workspace._lock, bench.workspace.connection:
            bench.workspace.connection.execute("UPDATE workbench_text_review_budget SET limit_reason=? WHERE run_id=? AND EXISTS "
                "(SELECT 1 FROM workbench_review_run r WHERE r.run_id=workbench_text_review_budget.run_id AND r.state='running' AND r.attempts=? AND r.worker_id=?)",
                (str(exc), run.run_id, run.attempts, run.worker_id))
        raise WorkflowFailure(str(exc)) from exc


def compact_locator(payload, ordinal, text):
    from .full_text_review_budget import MAX_LOCATOR_BYTES, TextReviewLimit
    payload = {key: value for key, value in payload.items() if key not in {'excerpt', 'bates_label'}}
    payload.update(locator_kind='text_unit', unit_ordinal=ordinal, unit_digest=hashlib.sha256(text.encode()).hexdigest())
    if len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()) > MAX_LOCATOR_BYTES:
        raise TextReviewLimit('A full-text unit locator exceeds its storage limit. Its identity was not truncated; this source requires direct review.')
    return payload


def read_locator(raw):
    from .full_text_review_budget import MAX_LOCATOR_BYTES
    if len(raw.encode()) > MAX_LOCATOR_BYTES:
        raise WorkspaceProblem('Saved full-text locator exceeds the supported bound; use its export for direct review.')
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise WorkspaceProblem('Saved full-text locator is malformed; its original value remains in the export.') from exc
    if not isinstance(value, dict) or (value.get('href') and not str(value['href']).startswith('/matters/')):
        raise WorkspaceProblem('Saved full-text locator cannot be opened safely; use its export for direct review.')
    return value


def _resolve_text_citations(bench, matter, values, *, hydrate, source_basis_digests=None, source_versions=None, export_bytes_remaining=None, scan_units=None):
    """Bound selected support while validating whole frozen sources in one pass."""
    from .workflow_jobs import WorkflowFailure
    if export_bytes_remaining is not None and (type(export_bytes_remaining) is not int or not 0 <= export_bytes_remaining <= 100 * 1024 * 1024):
        raise WorkflowFailure('Full-text portable export exceeds its citation budget.')
    grouped, order = {}, []
    for value in values:
        if len(order) >= 100:
            raise WorkflowFailure('A Report can resolve at most 100 full-text citations.')
        value = read_locator(json.dumps(value, ensure_ascii=False, separators=(',', ':')))
        if value.get('locator_kind') != 'text_unit' or type(value.get('unit_ordinal')) is not int:
            raise WorkflowFailure('Saved full-text locator does not identify a current extracted unit.')
        expected = grouped.setdefault(value['document_id'], {})
        if value['unit_ordinal'] in expected:
            raise WorkflowFailure('Saved full-text citations repeat an extracted unit.')
        expected[value['unit_ordinal']] = value
        order.append((value['document_id'], value['unit_ordinal']))
    if source_basis_digests is not None:
        if len(source_basis_digests) > 100 or any(document_id not in source_basis_digests for document_id in grouped):
            raise WorkflowFailure('The selected full-text source basis is missing or exceeds its bound.')
        for document_id in source_basis_digests:
            grouped.setdefault(document_id, {})
    resolved = {}
    store = bench.source_store(matter)
    with store.mutation_guard():
        for document_id, expected in grouped.items():
            document = store.get(document_id)
            if document.state != 'ready' or (source_versions is not None and document.version_id != source_versions.get(document_id)):
                raise WorkflowFailure('The frozen full-text source is unavailable or changed.')
            basis = hashlib.sha256()
            basis.update(('{"source_version":' + json.dumps(document.version_id) + ',"units":[').encode())
            units = document.iter_parsed_units() if scan_units is None else scan_units(document)
            for ordinal, unit in enumerate(units, 1):
                # Validate actual text even in uncited units, so stale per-unit
                # digest metadata cannot make a changed source look frozen.
                if hashlib.sha256(unit.text.encode()).hexdigest() != unit.excerpt_digest:
                    raise WorkflowFailure('The frozen full-text source contains changed text or stale digest metadata.')
                if ordinal > 1:
                    basis.update(b',')
                basis.update(json.dumps({'number': unit.number, 'digest': unit.excerpt_digest,
                    'line_start': unit.line_start, 'line_end': unit.line_end,
                    'start_ms': unit.start_ms, 'end_ms': unit.end_ms}, separators=(',', ':'), sort_keys=True).encode())
                if ordinal not in expected:
                    continue
                canonical = bench._workflow_citation_payload(bench._citation(matter, bench._candidate(matter, document, unit, ordinal)))
                current = compact_locator(canonical, ordinal, unit.text)
                saved = {key: val for key, val in expected.pop(ordinal).items() if key != 'bates_label'}
                if current != saved:
                    raise WorkflowFailure('The cited source unit changed before its outcome could be saved.')
                if hydrate:
                    if export_bytes_remaining is None:
                        if len(unit.text) > 6_000:
                            raise WorkflowFailure('This full-text unit exceeds the 6,000-character Report citation limit. Use the original ledger for its complete support.')
                    else:
                        export_bytes_remaining -= len(unit.text.encode('utf-8'))
                        if export_bytes_remaining < 0:
                            raise WorkflowFailure('Full-text portable export exceeds its citation budget; no partial export was created.')
                resolved[(document_id, ordinal)] = canonical if hydrate else current
            basis.update(b']}')
            if expected:
                raise WorkflowFailure('The cited source unit is no longer available.')
            if source_basis_digests is not None and basis.hexdigest() != source_basis_digests[document_id]:
                raise WorkflowFailure('The full-text source no longer matches its frozen content basis.')
    return tuple(resolved[key] for key in order)


def validate_text_citations(bench, matter, values, *, source_basis_digests=None, source_versions=None):
    """Stream each cited source once without materializing its unit population."""
    _resolve_text_citations(bench, matter, values, hydrate=False,
        source_basis_digests=source_basis_digests, source_versions=source_versions)


def resolve_text_report_citations(bench, matter, values, *, source_basis_digests=None, source_versions=None, scan_units=None):
    """Return <=100 canonical citations with complete, unsliced excerpts <=6,000.

    Optional frozen basis/version maps also validate listed sources with no
    citation. Each source is streamed once and every unit's actual digest is
    checked. An optional scan_units iterator can enforce one aggregate budget
    across multiple batches. Existing Report callers retain their own source
    budgets and snapshot/revision transaction.
    """
    return _resolve_text_citations(bench, matter, values, hydrate=True,
        source_basis_digests=source_basis_digests, source_versions=source_versions, scan_units=scan_units)


def resolve_text_export_citations(bench, matter, values, *, maximum_bytes, source_basis_digests, source_versions):
    """Hydrate complete source-check excerpts within the caller's remaining byte budget."""
    return _resolve_text_citations(bench, matter, values, hydrate=True,
        source_basis_digests=source_basis_digests, source_versions=source_versions,
        export_bytes_remaining=maximum_bytes)
