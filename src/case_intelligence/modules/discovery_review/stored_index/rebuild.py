"""Request-owned, row-bounded projection with a persisted continuation."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import json
import math
import sqlite3
from pathlib import PurePosixPath
import threading
from uuid import uuid4
from weakref import WeakSet

from case_intelligence.modules.discovery_review import load_files, reader
from case_intelligence.modules.discovery_review.fields import Field
from case_intelligence.modules.discovery_review.stored_index import keys
from case_intelligence.modules.discovery_review.stored_index.models import Budget, CapacityPlan, Job, Superseded, View
from case_intelligence.modules.discovery_review.stored_index.repository import Repository, digest
from case_intelligence.pilot_source_changes import SourceSnapshot

MAX_STEP_ROWS = 256
MAX_REQUEST_STEPS = 128
STEP_CONTROL_ROWS = 32
MAX_PRIMITIVE_ROWS = 32
SERVICES = WeakSet()
PHASES = ('inventory', 'delete_stale', 'load_sources', 'parse', 'match', 'reports', 'project')
MIN_CAPACITY = 2 * 1024 * 1024


def cancel(matter_root, matter_id):
    for service in tuple(SERVICES):
        if service.context.matters_root == matter_root.parent:
            service.cancel(matter_id)


def work_bound(*, R, F=0, L=0, A=0, C=0, I=0, V=0, Z=0, H=0, P=0, U=0, Q=0):
    work = 64 + 64 * R + 32 * F + 16 * L + 8 * A + 8 * C + 4 * I + 4 * V + 4 * Z + 8 * H + U
    early = 16 + 8 * R + 8 * F + 2 * P + Q
    steps = math.ceil(work / 192) + early + 2
    return work, early, steps


def fresh_cursor(target, total):
    return {'version': 2, 'generation': uuid4().hex, 'target': list(target), 'sequence': 0,
            'phase': 'inventory', 'after_document': '', 'after_source': '', 'after_line': 0,
            'subphase': '', 'active': None, 'eof': dict.fromkeys(PHASES, False),
            'counts': {'total': total, 'indexed': 0, 'errors': 0},
            'accumulators': {'work': 0, 'early': 0}}


def active_document(document_id, row, subphase):
    return {'document_id': document_id, 'source': json.loads(row[0]), 'fingerprint': digest(json.loads(row[0])),
            'subphase': subphase, 'child': ['', -1], 'line': 0, 'parser': None,
            'tier': 0, 'candidate': '', 'qualifying': [], 'ambiguous': False, 'match': None,
            'matched': 0, 'extra': 0, 'coding_after': None, 'coding_eof': False,
            'value_position': 0, 'value_eof': False, 'identifier': '', 'cache_digest': ''}


def decode_cursor(state):
    """Reject unknown shapes and inconsistent published controls before any resume."""
    if not state:
        return None
    try:
        cursor = json.loads(state['checkpoint'])
        prototype = fresh_cursor([], 0)
        if set(cursor) != set(prototype) or type(cursor['version']) is not int or cursor['version'] != 2:
            return None
        for name in ('generation', 'phase', 'after_document', 'after_source', 'subphase'):
            if not isinstance(cursor[name], str):
                return None
        for name in ('sequence', 'after_line'):
            if type(cursor[name]) is not int or cursor[name] < 0:
                return None
        target = cursor['target']
        if (not isinstance(target, list) or len(target) != 8 or not isinstance(target[0], str)
                or any(type(target[i]) is not int for i in (1, 2, 5, 6, 7))
                or not isinstance(target[3], list) or len(target[3]) != 3
                or any(type(value) is not int for value in target[3]) or not isinstance(target[4], str)):
            return None
        if set(cursor['eof']) != set(PHASES) or any(type(value) is not bool for value in cursor['eof'].values()):
            return None
        for name, expected in (('counts', {'total', 'indexed', 'errors'}), ('accumulators', {'work', 'early'})):
            if set(cursor[name]) != expected or any(type(v) is not int or v < 0 for v in cursor[name].values()):
                return None
        counts = cursor['counts']
        if counts['indexed'] + counts['errors'] > counts['total']:
            return None
        if (cursor['generation'] != state['generation'] or cursor['phase'] != state['phase']
                or target[0] != state['epoch'] or target[1] != state['revision']
                or target[4] != state['definition_digest'] or target[6] != state['key_version']
                or any(counts[k] != state[k] for k in counts)
                or state['pending'] != counts['total'] - counts['indexed'] - counts['errors']):
            return None
        phase, active = cursor['phase'], cursor['active']
        if phase not in (*PHASES, 'complete'):
            return None
        if active is not None:
            example = active_document('', ('{}',), '')
            if not isinstance(active, dict) or set(active) != set(example):
                return None
            for name, value in example.items():
                if value is not None and type(active[name]) is not type(value):
                    return None
            SourceSnapshot(**active['source'])
            if digest(active['source']) != active['fingerprint'] or len(active['qualifying']) > 2:
                return None
            if active['subphase'] not in ('values_clear', 'load_clear', 'parse', 'refresh', 'match', 'report',
                                         'prepare', 'coding', 'values', 'publish'):
                return None
            if (len(active['child']) != 2 or not isinstance(active['child'][0], str) or type(active['child'][1]) is not int
                    or active['tier'] not in range(3) or active['line'] < 0 or active['value_position'] < 0):
                return None
            if active['coding_after'] is not None and (not isinstance(active['coding_after'], list) or len(active['coding_after']) != 3):
                return None
            if active['parser'] is not None:
                parser = active['parser']
                if (set(parser) != {'offset', 'line', 'count', 'headers', 'path_header', 'bates_header', 'problem', 'eof', 'recognized'}
                        or any(type(parser[k]) is not int or parser[k] < 0 for k in ('offset', 'line', 'count'))
                        or type(parser['eof']) is not bool or type(parser['recognized']) is not bool
                        or not isinstance(parser['headers'], list) or any(not isinstance(h, str) for h in parser['headers'])
                        or any(not isinstance(parser[k], str) for k in ('path_header', 'bates_header', 'problem'))):
                    return None
        completed_before = {
            'inventory': (), 'delete_stale': ('inventory',),
            'load_sources': ('inventory', 'delete_stale'), 'parse': ('inventory', 'delete_stale'),
            'match': ('inventory', 'delete_stale', 'load_sources', 'parse'),
            'reports': ('inventory', 'delete_stale', 'load_sources', 'parse', 'match'),
            'project': ('inventory', 'delete_stale', 'load_sources', 'parse', 'match', 'reports'),
            'complete': PHASES,
        }
        if {name for name, ended in cursor['eof'].items() if ended} != set(completed_before[phase]):
            return None
        permitted = {'inventory': (), 'delete_stale': ('values_clear', 'load_clear'),
                     'load_sources': ('load_clear', 'refresh'), 'parse': ('parse',),
                     'match': ('match',), 'reports': ('report',),
                     'project': ('prepare', 'coding', 'values_clear', 'values', 'publish'), 'complete': ()}
        if cursor['subphase'] != (active['subphase'] if active else ''):
            return None
        if active and active['subphase'] not in permitted[phase]:
            return None
        if phase == 'complete' and (active is not None or not all(cursor['eof'].values())
                                    or counts['indexed'] + counts['errors'] != counts['total']):
            return None
        return cursor
    except (ValueError, TypeError, KeyError):
        return None


class Service:
    def __init__(self, context, identifiers, codes):
        self.context, self.identifiers, self.codes = context, identifiers, codes
        self.repository = Repository(context.db)
        self.jobs = {}
        self.jobs_lock = threading.Lock()
        self.step_lock = threading.Lock()
        SERVICES.add(self)

    def cancel(self, matter_id):
        with self.jobs_lock:
            job = self.jobs.pop(matter_id, None)
            if job is not None:
                job.cancelled = True
                job.serial += 1
        if job is not None:
            job.store.source_changes.unsubscribe(job.subscription)

    def invalidate(self, matter_id):
        with self.jobs_lock:
            job = self.jobs.get(matter_id)
            if job is not None:
                job.serial += 1
                job.error = ''

    def signature(self, job, budget=None):
        token = self.codes.change_token(job.matter_id)
        if budget is not None:
            budget.charge(2)
        return (job.store.source_changes.epoch, job.store.revision, self.identifiers.mark(job.matter_id),
                list(token), job.definition_digest, job.store.source_changes.failed_revision, keys.VERSION, job.serial)

    def current(self, matter_id, store, definition):
        reader.retain(matter_id)
        definition_digest = digest(asdict(definition))
        with store.mutation_guard():
            with self.jobs_lock:
                job = self.jobs.get(matter_id)
            if job is not None and job.store is not store:
                self.cancel(matter_id)
                job = None
            if job is None:
                candidate = Job(matter_id, store, definition, definition_digest)
                candidate.subscription, _, _ = store.source_changes.subscribe(lambda change: self.invalidate(matter_id))
                with self.jobs_lock:
                    job = self.jobs.setdefault(matter_id, candidate)
                if job is not candidate:
                    store.source_changes.unsubscribe(candidate.subscription)
            with self.jobs_lock:
                job.definition, job.definition_digest = definition, definition_digest
            with self.context.db.lock:
                state = self.repository.state(matter_id)
                cursor = decode_cursor(state)
                complete = cursor is not None and cursor['target'] == list(self.signature(job)) and cursor['phase'] == 'complete'
        if not complete:
            self.advance(job, MAX_REQUEST_STEPS)
        with store.mutation_guard(), self.context.db.lock:
            state = self.repository.state(matter_id) or {}
            cursor = decode_cursor(state)
            target = self.signature(job)
            valid = cursor is not None and cursor['target'] == list(target)
            complete = valid and cursor['phase'] == 'complete' and not cursor['counts']['errors']
            generation = cursor['generation'] if valid else digest(target)
            return View(matter_id, store, definition, definition_digest, generation, target, job.error,
                        self.repository, lambda: self.signature(job), state, complete, store.source_changes.count(),
                        json.loads(state.get('summaries', '{}')) if valid else {})

    def advance(self, job, max_steps):
        if type(max_steps) is not int or not 0 <= max_steps <= MAX_REQUEST_STEPS:
            raise ValueError('request step budget must be between zero and 128')
        attempts = 0
        raw = {}
        for attempt in range(max_steps):
            attempts = attempt + 1
            with self.jobs_lock:
                plan = job.plan
                cancelled = job.cancelled
            if cancelled:
                break
            capacity = max(MIN_CAPACITY, plan[2] if plan else 0)
            try:
                self.context.ensure_capacity(capacity)
            except (OSError, RuntimeError, ValueError):
                job.error = 'Metadata unavailable: indexing error'
                break
            if not self.step_lock.acquire(blocking=False):
                break
            budget = Budget(MAX_STEP_ROWS, MAX_STEP_ROWS - STEP_CONTROL_ROWS)
            job.last_budget = budget
            try:
                done = self.step(job, budget, capacity, raw, plan)
                if done:
                    break
            except CapacityPlan as needed:
                with self.jobs_lock:
                    job.plan = (self.expected_generation, self.expected_sequence, needed.size,
                                (plan[3] if plan else 0) + budget.work, (plan[4] if plan else 0) + 1)
            except Superseded:
                raw.clear()
                with self.jobs_lock:
                    job.plan = None
                    job.serial += 1
            except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
                job.error = 'Metadata unavailable: indexing error'
                self.failure = str(exc)
                break
            finally:
                self.step_lock.release()
        job.attempts = attempts
        return attempts

    def step(self, job, budget, capacity, raw, plan):
        budget.in_control = True
        with job.store.mutation_guard(), self.context.db.lock:
            target = self.signature(job, budget)
            state = self.repository.state(job.matter_id)
            budget.charge(int(state is not None))
            cursor = decode_cursor(state)
            if cursor is not None and cursor['target'] == list(target) and cursor['phase'] == 'complete':
                return True
            initializing = cursor is None or cursor['target'] != list(target)
            if initializing:
                cursor = fresh_cursor(target, job.store.source_changes.count())
                budget.charge(1)
                summaries = {'headers': [], 'filled': [], 'reports': [], 'report_total': 0, 'matched': 0}
            else:
                summaries = json.loads(state['summaries'])
            expected = (state.get('generation'), state.get('checkpoint')) if state else None
            self.expected_generation, self.expected_sequence = cursor['generation'], cursor['sequence']
        if plan and plan[:2] != (cursor['generation'], cursor['sequence']):
            with self.jobs_lock:
                job.plan = None
            raise Superseded('capacity plan changed')
        budget.in_control = False
        prepared = {} if initializing else self.prepare(job, cursor, budget, raw)
        if initializing:
            budget.charge(0, credit=True)
        with self.commit(job, target, expected, cursor, budget, capacity) as tx:
            if prepared.get('records') is not None:
                self.parsed(tx, job, cursor, summaries, prepared)
            while not initializing and budget.remaining >= MAX_PRIMITIVE_ROWS + 1 and cursor['phase'] != 'complete':
                before = budget.work
                before_phase = cursor['phase']
                if not self.primitive(tx, job, cursor, summaries, prepared):
                    cursor['accumulators']['early'] += int(budget.work < 192)
                    break
                if budget.work == before or cursor['phase'] != before_phase:
                    budget.charge(0, credit=True)
                if budget.work - before > MAX_PRIMITIVE_ROWS:
                    raise RuntimeError('index primitive row budget exceeded')
            pending = prepared.get('pending_cache')
            if pending is not None:
                document_id, cache = pending
                cursor['active']['cache_digest'] = digest(cache['projection'])
                self.repository.cache(tx, job.matter_id, document_id, cache)
            cursor['subphase'] = cursor['active']['subphase'] if cursor['active'] else ''
            cursor['accumulators']['work'] += budget.work + (plan[3] if plan else 0)
            cursor['accumulators']['early'] += plan[4] if plan else 0
            cursor['sequence'] += 1
            budget.in_control = True
            self.repository.save(tx, job.matter_id, cursor, summaries, state is not None)
        with self.jobs_lock:
            job.plan = None
            job.error = ''
        return cursor['phase'] == 'complete'

    @contextmanager
    def commit(self, job, target, expected, cursor, budget, capacity):
        locked = False
        try:
            with job.store.mutation_guard(), self.repository.transaction(budget, capacity) as tx:
                budget.in_control = True
                actual = self.repository.state(job.matter_id, tx)
                observed = (actual.get('generation'), actual.get('checkpoint')) if actual else None
                if observed != expected or self.signature(job, budget) != target:
                    raise Superseded('cursor or source target changed')
                budget.in_control = False
                yield tx
                budget.in_control = True
                if self.signature(job, budget) != target:
                    raise Superseded('projection inputs changed')
                self.jobs_lock.acquire()
                locked = True
                if job.cancelled or self.jobs.get(job.matter_id) is not job or job.serial != target[7]:
                    raise Superseded('projection cancelled')
        finally:
            if locked:
                self.jobs_lock.release()

    def prepare(self, job, cursor, budget, raw):
        active = cursor['active']
        if active is None:
            return {}
        source = SourceSnapshot(**active['source'])
        key = (source.document_id, source.version_id, source.digest)
        if active['subphase'] == 'parse':
            if source.state != 'ready':
                return {'parse_error': 'source is not ready for review', 'records': []}
            if source.size > load_files.MAX_LOAD_FILE_BYTES:
                return {'parse_error': 'file exceeds the byte limit', 'records': []}
            if key not in raw:
                try:
                    content = job.store.source_path(source.document_id, verify_digest=True).read_bytes()
                    if len(content) > load_files.MAX_LOAD_FILE_BYTES:
                        raise ValueError('load file exceeds byte limit')
                    raw[key] = content.decode('utf-8-sig')
                except UnicodeDecodeError:
                    return {'parse_error': 'invalid UTF-8 encoding', 'records': []}
                except (OSError, ValueError, KeyError, RuntimeError):
                    return {'parse_error': 'source could not be read', 'records': []}
            records, parser, projected = [], active['parser'], 0
            while projected < 192:
                parser, row, touched = load_files.resume_record(raw[key], source.display_name, job.definition, parser)
                budget.charge(touched, credit=True)
                projected += max(1, touched) + int(row is not None)
                records.append((parser, row))
                if parser['eof']:
                    break
            return {'records': records}
        if active['subphase'] == 'prepare':
            suffix = PurePosixPath(source.display_name).suffix.casefold()
            if (source.origin == 'upload' and source.state == 'ready'
                    and (source.media_type in job.definition.email_media_types or suffix in job.definition.email_suffixes)):
                with job.store.mutation_guard(), self.context.db.lock:
                    found = self.repository.document_read(job.matter_id, source.document_id)
                    budget.charge(int(found is not None))
                cache = json.loads(found[2])
                email_key = digest((source.version_id, source.digest, job.definition_digest))
                if cache.get('email_key') != email_key:
                    try:
                        email = reader.email_headers(job.store.source_path(source.document_id, verify_digest=True),
                                                     tuple(f.email_header for f in job.definition.fields if f.email_header))
                        return {'email': email, 'email_key': email_key}
                    except (OSError, ValueError, KeyError, RuntimeError):
                        return {'email': {}, 'email_key': email_key, 'email_error': True}
            return {'prepared': source.document_id}
        return {}

    def transition(self, cursor, phase):
        cursor['eof'][cursor['phase']] = True
        cursor.update(phase=phase, after_document='', after_source='', after_line=0, subphase='', active=None)

    def parsed(self, tx, job, cursor, summaries, prepared):
        active = cursor['active']
        source = SourceSnapshot(**active['source'])
        if prepared.get('parse_error'):
            parser = {'offset': 0, 'line': 0, 'count': 0, 'headers': [], 'path_header': '', 'bates_header': '',
                      'problem': f"Load file was not read: {prepared['parse_error']}.", 'eof': True, 'recognized': True}
            active['parser'] = parser
        for parser, row in prepared['records']:
            if row is not None:
                self.repository.put_load_row(tx, job.matter_id, source, parser, row)
            active['parser'] = parser
        if active['parser']['eof']:
            row = self.repository.document(tx, job.matter_id, source.document_id)
            cache = json.loads(row[2])
            parser = active['parser']
            if parser['recognized'] or parser['problem']:
                cache['table'] = {'headers': parser['headers'], 'rows': parser['count'], 'problem': parser['problem'], 'matched': 0, 'extra': 0}
            else:
                cache.pop('table', None)
            cache['table_key'] = self.table_key(source, job)
            cache.pop('table_error', None)
            if prepared.get('parse_error'):
                cache['table_error'] = True
            self.repository.cache(tx, job.matter_id, source.document_id, cache, is_loadfile='table' in cache)
            self.add_headers(summaries, cache)
            cursor.update(phase='load_sources', after_document=source.document_id, active=None)
        prepared['records'] = None

    def table_key(self, source, job):
        return digest((source.version_id, source.digest, job.definition_digest, source.state, source.origin,
                       PurePosixPath(source.display_name).suffix.casefold()))

    def add_headers(self, summaries, cache):
        for header in cache.get('table', {}).get('headers', ()):
            if header not in summaries['headers']:
                summaries['headers'].append(header)

    def primitive(self, tx, job, cursor, summaries, prepared):
        phase, active = cursor['phase'], cursor['active']
        repo, matter_id, generation = self.repository, job.matter_id, cursor['generation']
        if phase == 'inventory':
            page = job.store.source_changes.seek(cursor['after_document'], limit=8)
            tx.budget.charge(len(page), credit=not page)
            for source in page:
                repo.inventory(tx, matter_id, source, generation)
                cursor['after_document'] = source.document_id
            if not page:
                self.transition(cursor, 'delete_stale')
        elif phase == 'delete_stale':
            if active is None:
                rows = tx.read('SELECT document_id,seen_generation,source_payload FROM workbench_discovery_index_item '
                               'WHERE matter_id=? AND document_id>? ORDER BY document_id LIMIT 1', (matter_id, cursor['after_document']))
                if not rows:
                    self.transition(cursor, 'load_sources')
                elif rows[0][1] == generation:
                    cursor['after_document'] = rows[0][0]
                else:
                    cursor['active'] = active_document(rows[0][0], (rows[0][2],), 'values_clear')
            elif active['subphase'] == 'values_clear':
                rows = repo.clear_values(tx, matter_id, active['document_id'], active['child'])
                if rows:
                    active['child'] = list(rows[-1])
                else:
                    active['subphase'] = 'load_clear'
            elif active['subphase'] == 'load_clear':
                rows = repo.clear_load_rows(tx, matter_id, active['document_id'], active['line'])
                if rows:
                    active['line'] = rows[-1][0]
                else:
                    tx.write('DELETE FROM workbench_discovery_index_item WHERE matter_id=? AND document_id=?', (matter_id, active['document_id']))
                    cursor.update(after_document=active['document_id'], active=None)
        elif phase in ('load_sources', 'parse'):
            if active is None:
                rows = repo.documents(tx, matter_id, cursor['after_document'], generation)
                if not rows:
                    cursor['eof']['parse'] = True
                    self.transition(cursor, 'match')
                    return True
                document_id = rows[0][0]
                row = repo.document(tx, matter_id, document_id)
                source, cache = SourceSnapshot(**json.loads(row[0])), json.loads(row[2])
                eligible = (PurePosixPath(source.display_name).suffix.casefold() in load_files.DELIMITERS and source.origin == 'upload')
                if cache.get('table_key') == self.table_key(source, job):
                    self.add_headers(summaries, cache)
                    cursor['active'] = active_document(document_id, row, 'refresh') if 'table' in cache else None
                    cursor['after_document'] = document_id
                elif not eligible and 'table' not in cache:
                    cache['table_key'] = self.table_key(source, job)
                    repo.cache(tx, matter_id, document_id, cache, is_loadfile=False)
                    cursor['after_document'] = document_id
                else:
                    cursor['active'] = active_document(document_id, row, 'load_clear')
            elif active['subphase'] == 'load_clear':
                rows = repo.clear_load_rows(tx, matter_id, active['document_id'], active['line'])
                if rows:
                    active['line'] = rows[-1][0]
                else:
                    source = SourceSnapshot(**active['source'])
                    eligible = PurePosixPath(source.display_name).suffix.casefold() in load_files.DELIMITERS and source.origin == 'upload'
                    if not eligible:
                        row = repo.document(tx, matter_id, source.document_id)
                        cache = json.loads(row[2])
                        cache.pop('table', None)
                        cache.pop('table_error', None)
                        cache['table_key'] = self.table_key(source, job)
                        repo.cache(tx, matter_id, source.document_id, cache, is_loadfile=False)
                        cursor.update(after_document=source.document_id, active=None)
                    else:
                        active.update(subphase='parse', line=0)
                        cursor['phase'] = 'parse'
                        return False
            elif active['subphase'] == 'parse':
                return False
            elif active['subphase'] == 'refresh':
                rows = repo.load_rows(tx, matter_id, active['document_id'], active['line'])
                order = keys.encode((reader.natural_key(active['source']['relative_path']), active['document_id']))
                for row in rows:
                    tx.write('UPDATE workbench_discovery_index_load_row SET source_order=?,order_prefix=? WHERE matter_id=? AND source_id=? AND line=?',
                             (order, order[:keys.PREFIX_BYTES], matter_id, active['document_id'], row[0]))
                    active['line'] = row[0]
                if not rows:
                    cursor.update(after_document=active['document_id'], active=None)
        elif phase == 'match':
            self.match(tx, job, cursor)
        elif phase == 'reports':
            self.report(tx, job, cursor, summaries)
        elif phase == 'project':
            return self.project(tx, job, cursor, summaries, prepared)
        return True

    def match(self, tx, job, cursor):
        repo, mid, active = self.repository, job.matter_id, cursor['active']
        if active is None:
            rows = tx.read('SELECT source_id,line,path_value,bates_value FROM workbench_discovery_index_load_row WHERE matter_id=? '
                           'AND (source_id>? OR (source_id=? AND line>?)) ORDER BY source_id,line LIMIT 1',
                           (mid, cursor['after_source'], cursor['after_source'], cursor['after_line']))
            if not rows:
                self.transition(cursor, 'reports')
                return
            source_id, line, path, bates = rows[0]
            source_row = repo.document(tx, mid, source_id)
            active = active_document(source_id, source_row, 'match')
            active.update(line=line, match=[path, bates], tier=0 if path else 2)
            cursor['active'] = active
        path, bates = active['match']
        tier = active['tier']
        value = (path, path.rsplit('/', 1)[-1], bates)[tier]
        rows = repo.candidates(tx, mid, cursor['generation'], ('path', 'name', 'stem')[tier], value, active['candidate']) if value else ()
        for document_id, candidate in rows:
            active['candidate'] = document_id
            if tier != 1 or candidate.endswith('/' + path) or path.endswith('/' + candidate) or candidate == path:
                active['qualifying'].append(document_id)
                if len(active['qualifying']) == 2:
                    break
        found = active['qualifying']
        if len(found) < 2 and rows:
            return
        if len(found) == 1:
            repo.attach_row(tx, mid, active['document_id'], active['line'], found[0], ('path', 'file name', 'Bates-named file')[tier])
        elif tier == 0 or (tier == 1 and not found and not active['ambiguous']):
            active.update(tier=tier + 1, candidate='', qualifying=[], ambiguous=active['ambiguous'] or bool(found))
            return
        else:
            repo.attach_row(tx, mid, active['document_id'], active['line'], '', '')
        cursor.update(after_source=active['document_id'], after_line=active['line'], active=None)

    def report(self, tx, job, cursor, summaries):
        repo, mid, active = self.repository, job.matter_id, cursor['active']
        if active is None:
            rows = repo.documents(tx, mid, cursor['after_document'], cursor['generation'])
            if not rows:
                self.transition(cursor, 'project')
                return
            document_id = rows[0][0]
            row = repo.document(tx, mid, document_id)
            cursor['after_document'] = document_id
            if 'table' not in json.loads(row[2]):
                return
            active = active_document(document_id, row, 'report')
            cursor['active'] = active
        rows = repo.load_rows(tx, mid, active['document_id'], active['line'], limit=4)
        for line, path, bates, values, attached, basis in rows:
            if attached:
                winner = repo.attachment(tx, mid, attached)
                if winner and winner[:2] == (active['document_id'], line):
                    active['matched'] += 1
                else:
                    active['extra'] += 1
            active['line'] = line
        if rows:
            return
        row = repo.document(tx, mid, active['document_id'])
        cache = json.loads(row[2])
        table = cache['table']
        table.update(matched=active['matched'], extra=active['extra'])
        report = dict(document_id=active['document_id'], name=active['source']['display_name'],
                      rows=table['rows'], matched=table['matched'], extra=table['extra'], problem=table['problem'])
        cache['report'] = report
        repo.cache(tx, mid, active['document_id'], cache)
        summaries['report_total'] += 1
        summaries['matched'] += table['matched']
        if len(summaries['reports']) < 100:
            summaries['reports'].append(report)
        cursor['active'] = None

    def project(self, tx, job, cursor, summaries, prepared):
        repo, mid, active = self.repository, job.matter_id, cursor['active']
        if active is None:
            rows = repo.documents(tx, mid, cursor['after_document'], cursor['generation'])
            if not rows:
                cursor['eof']['project'] = True
                counts = cursor['counts']
                if all(cursor['eof'].values()) and counts['indexed'] + counts['errors'] == counts['total']:
                    cursor['phase'] = 'complete'
                else:
                    raise RuntimeError('inconsistent index completion markers')
                return True
            row = repo.document(tx, mid, rows[0][0])
            active = active_document(rows[0][0], row, 'prepare')
            cursor['active'] = active
            source = SourceSnapshot(**active['source'])
            suffix = PurePosixPath(source.display_name).suffix.casefold()
            if source.media_type in job.definition.email_media_types or suffix in job.definition.email_suffixes:
                return False
        cached = prepared.get('document')
        if cached is None or cached[0] != active['document_id']:
            row = repo.document(tx, mid, active['document_id'])
            cache = json.loads(row[2])
            if active['subphase'] != 'prepare':
                self.checked_projection(cache, cursor, active)
            prepared['document'] = (active['document_id'], row, cache)
        else:
            document_id, row, cache = cached
        source = SourceSnapshot(**active['source'])
        subphase = active['subphase']
        if subphase == 'prepare':
            captures = job.store.source_changes.read([source.document_id])
            tx.budget.charge(len(captures))
            if not captures or digest(asdict(captures[0])) != active['fingerprint']:
                raise Superseded('source metadata changed')
            assigned = self.identifiers.read_documents(mid, [source.document_id])
            tx.budget.charge(len(assigned))
            active['identifier'] = assigned.get(source.document_id, '')
            attachment = repo.attachment(tx, mid, source.document_id)
            if 'email_key' in prepared:
                cache.update(email=prepared['email'], email_key=prepared['email_key'])
                cache.pop('email_error', None)
                if prepared.get('email_error'):
                    cache['email_error'] = True
            cache['projection'] = {'generation': cursor['generation'], 'source': active['fingerprint'],
                                   'attachment': attachment, 'item': None, 'codes': []}
            active.update(subphase='coding', coding_after=None, coding_eof=False)
            prepared['pending_cache'] = (source.document_id, cache)
        elif subphase == 'coding':
            projection = cache['projection']
            page = self.codes.current_page(mid, [source.document_id], after=active['coding_after'], limit=8)
            tx.budget.charge(len(page) * 2, credit=not page)
            for document_id, code_id, label, enabled, actor, created, position in page:
                projection['codes'].append({'value': label if enabled else f'{label} (retired)', 'active': bool(enabled),
                                            'created_by': actor, 'created_at': created})
            if page:
                active['coding_after'] = [page[-1][6], page[-1][1], page[-1][0]]
            else:
                active.update(coding_eof=True, subphase='values_clear')
                fingerprint = digest((active['source'], active['identifier'], projection['codes'], projection['attachment'],
                                      summaries['headers'], job.definition_digest, keys.VERSION,
                                      cache.get('email', {}), cache.get('email_error'), cache.get('table_error')))
                projection['fingerprint'] = fingerprint
                if fingerprint == row[3] and row[1]:
                    projection['item'] = json.loads(row[1])
                    active.update(subphase='publish', value_eof=True)
                else:
                    attachment = projection['attachment']
                    match = load_files.Match(json.loads(attachment[4])['display_name'], attachment[1], attachment[2],
                                            json.loads(attachment[3])) if attachment else None
                    item = reader.project_item(source, job.definition, {source.document_id: active['identifier']},
                                               match, cache.get('email', {}))
                    projection['item'] = asdict(item)
                    for chosen in job.definition.fields:
                        if chosen.computed == 'review_codes':
                            projection['item']['values'][chosen.key] = ', '.join(entry['value'] for entry in projection['codes'])
                            projection['item']['details'][chosen.key] = projection['codes']
                    projection['item']['metadata_state'] = 'error' if cache.get('email_error') or cache.get('table_error') else 'complete'
            prepared['pending_cache'] = (source.document_id, cache)
        elif subphase == 'values_clear':
            rows = repo.clear_values(tx, mid, source.document_id, active['child'])
            if rows:
                active['child'] = list(rows[-1])
            else:
                active['subphase'] = 'values'
        elif subphase == 'values':
            projection = cache['projection']
            item = reader.Item(**projection['item'])
            choices = [Field('', '', '', sort='natural'), *job.definition.fields,
                       *(job.definition.field('lf:' + header) for header in summaries['headers'])]
            start = active['value_position']
            for position in range(start, min(start + 16, 2 * len(choices))):
                repo.value(tx, mid, item, choices[position // 2], bool(position % 2))
                active['value_position'] = position + 1
            if active['value_position'] == 2 * len(choices):
                active.update(value_eof=True, subphase='publish')
        elif subphase == 'publish':
            projection = cache['projection']
            assigned = self.identifiers.read_documents(mid, [source.document_id])
            tx.budget.charge(len(assigned))
            if assigned.get(source.document_id, '') != active['identifier'] or not active['coding_eof'] or not active['value_eof']:
                raise Superseded('human work changed before publish')
            item = reader.Item(**projection['item'])
            for key, value in item.values.items():
                if value and key not in summaries['filled']:
                    summaries['filled'].append(key)
            cache.pop('projection')
            prepared['pending_cache'] = None
            prepared['document'] = None
            repo.publish(tx, mid, item, cursor['generation'], projection['fingerprint'], cache)
            cursor['counts']['indexed' if item.metadata_state == 'complete' else 'errors'] += 1
            cursor.update(after_document=source.document_id, active=None)
        return True

    def checked_projection(self, cache, cursor, active):
        projection = cache['projection']
        if (projection['generation'] != cursor['generation'] or projection['source'] != active['fingerprint']
                or digest(projection) != active['cache_digest']):
            raise Superseded('partial projection changed')
        return projection
