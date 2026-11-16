"""Request-owned, resumable production import and bounded record projection."""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import uuid

from .. import load_files
from .matching import (
    candidate_agrees, compatible_metadata, finish_match, initial_match, label_key,
    literal_identifiers, observe_candidate, path_key, project_record,
)
from .repository import (
    MAX_BATCH_BYTES, MAX_BATCH_RECORDS, MAX_ORDINAL, ProductionRepository,
    ProductionStale, ProductionUnavailable, SourceIdentity,
)
from case_intelligence.source_load_files import RecordEvent
from case_intelligence.pilot_uploads import UploadProblem
from case_intelligence.source_load_file_qc import (
    family_record, family_diagnostics, family_pair_diagnostics, family_order_key,
)

PARSER_REVISION = "production-record-events-1"
MAX_RESTARTS = 3
def source_identity(store):
    return store.source_changes.epoch


def definition_digest(definition) -> str:
    data = asdict(definition)
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=lambda value: sorted(value)).encode()).hexdigest()


def decoded_payload_bytes(event) -> int:
    """Charge the same complete ordered arrays stored by the repository."""
    return sum(len(json.dumps(value, ensure_ascii=False,
        separators=(",", ":")).encode()) for value in
        (event.headers, event.cells, event.diagnostics, event.optional_values))


def source_from_attempt(attempt) -> SourceIdentity:
    return SourceIdentity(*(attempt[key] for key in
        ("document_id", "version_id", "digest", "name", "relative_path", "source_format", "root_key")))


def interval_key(number):
    digits = str(number)
    return f"{len(digits):010d}:{digits}"


def record_cursor(category, attempt_id, ordinal) -> str:
    return f"{'dat' if category == 'document' else 'opt'}:{attempt_id}:{ordinal}"


def source_fingerprint(path, digest):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ProductionUnavailable("The admitted load file is not a regular file.")
        hasher = hashlib.sha256()
        while chunk := os.read(descriptor, 65536):
            hasher.update(chunk)
        after = os.fstat(descriptor)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                   value.st_mtime_ns, value.st_ctime_ns)
        if hasher.hexdigest() != digest or identity(before) != identity(after):
            raise ProductionStale("The admitted load-file bytes changed.")
        return identity(after)
    finally:
        os.close(descriptor)


class BatchBoundary(Exception):
    """Return to the request with one exact persisted parser continuation."""


class ProductionService:
    def __init__(self, context, *, repository=None):
        self.context = context
        self.repository = repository or ProductionRepository(context.db)
        self.bindings = {}
        self.marks = {}
        self.discovery = {}
        self.positions = {}
        self.discovery_done = set()
        self.epoch = uuid.uuid4().hex
        self.step_lock = threading.Lock()
        self.paused = {}

    def cancel(self, matter_id):
        binding = self.bindings.pop(matter_id, None)
        if binding is not None:
            binding['store'].source_changes.unsubscribe(binding['subscription'])
        self.discovery.pop(matter_id, None)
        self.discovery_done.discard(matter_id)
        self.positions.pop(matter_id, None)
        self.marks.pop(matter_id, None)
        self.paused.pop(matter_id, None)

    def notified(self, matter_id, change):
        binding = self.bindings.get(matter_id)
        if binding is not None:
            inputs, changed_roots = binding['inputs'], set()
            for document_id in change.removed_document_ids:
                prior = inputs.pop(document_id, None)
                if prior:
                    changed_roots.add(prior[1])
            for item in change.upserted:
                prior = inputs.get(item.document_id)
                root = self.repository.provenance(matter_id, item.document_id, item.version_id) if item.origin == 'upload' else ''
                current = (self.input_identity(item), root)
                if prior != current:
                    if prior:
                        changed_roots.add(prior[1])
                    if root:
                        changed_roots.add(root)
                if root:
                    inputs[item.document_id] = current
                else:
                    inputs.pop(item.document_id, None)
            for root in changed_roots:
                binding['roots'][root] = change.revision
            binding['dirty'].update(attempt_id for attempt_id, root in binding['attempt_roots'].items()
                                    if root in changed_roots)
            self.discovery[matter_id] = ''
            self.discovery_done.discard(matter_id)
            self.changed(matter_id)

    def bind(self, matter, store, definition, *, target=None, target_validator=None):
        """Bind only authorized capabilities, with the index owner's exact target."""
        if target is not None and (not isinstance(target, str) or not target):
            raise ValueError('The bound production target must be nonempty text.')
        prior = self.bindings.get(matter.matter_id)
        digest = definition_digest(definition)
        with store.mutation_guard():
            if prior is not None and prior['store'] is not store:
                prior['store'].source_changes.unsubscribe(prior['subscription'])
                prior = None
            if prior is None:
                subscription, identity, revision = store.source_changes.subscribe(
                    lambda change: self.notified(matter.matter_id, change))
                prior = dict(subscription=subscription, source_identity='production-v2-' + identity, revision=0,
                             inputs={}, roots={}, scan_start=revision, attempt_roots={}, dirty=set(), validated={})
                self.discovery[matter.matter_id] = ''
                self.discovery_done.discard(matter.matter_id)
            if prior.get('digest') not in (None, digest):
                self.discovery[matter.matter_id] = ''
                self.discovery_done.discard(matter.matter_id)
            self.bindings[matter.matter_id] = {**prior,
                'matter': matter, 'store': store, 'definition': definition, 'digest': digest,
                'validator': target_validator, 'target': target, 'human': self.repository.identifier_stamp(matter.matter_id)}

    @staticmethod
    def input_identity(item):
        return (item.version_id, item.digest, item.display_name, item.relative_path,
                item.media_type, item.state, item.origin)

    def restart_state(self, matter_id, attempt, state):
        """Persist the lineage limit before allowing any replacement work."""
        state = dict(state)
        count = 0 if attempt['status'] == 'complete' else state.get('restarts', 0)
        if count >= MAX_RESTARTS:
            state.update(paused=True, restarts=MAX_RESTARTS, reload=uuid.uuid4().hex)
            self.repository.update_attempt(matter_id, attempt['attempt_id'], status='building', phase='paused')
            self.remember_pause(matter_id, attempt, state)
        else:
            state['restarts'] = count + 1
        self.repository.save_dependencies(matter_id, attempt['attempt_id'], state, attempt['input_revision'])
        return state

    def remember_pause(self, matter_id, attempt, state):
        self.paused.setdefault(matter_id, {})[attempt['attempt_id']] = {
            'attempt_id': attempt['attempt_id'], 'reload': state['reload'], 'restarts': state['restarts']}

    def resume(self, matter_id, attempt_id, token):
        with self.binding(matter_id)['store'].mutation_guard(), self.repository.transaction():
            attempt = self.repository.attempt(matter_id, attempt_id)
            state = self.dependency_state(attempt) if attempt else {}
            if not state.get('paused') or state.get('reload') != token:
                return
            state = dict(state, paused=False, restarts=0, reload='')
            self.paused.get(matter_id, {}).pop(attempt_id, None)
            self.repository.save_dependencies(matter_id, attempt_id, state, attempt['input_revision'])
            self.repository.update_attempt(matter_id, attempt_id, status='building', phase=state['resume_phase'])
            self.discovery_done.discard(matter_id)
            self.discovery[matter_id] = ''
            self.changed(matter_id)

    def begin_attempt(self, matter_id, source, **options):
        with self.repository.transaction():
            prior = self.repository.current_attempt(matter_id, source.document_id)
            state = self.dependency_state(prior) if prior else {}
            if state.get('paused'):
                return prior['attempt_id']
            if prior and (source_from_attempt(prior) != source
                    or prior['parser_revision'] != options['parser_revision']
                    or prior['definition_digest'] != options['definition_digest']):
                state = self.restart_state(matter_id, prior, dict(state, resume_phase=prior['phase']))
                if state.get('paused'):
                    return prior['attempt_id']
            elif prior and prior['target'] != options['target']:
                self.repository.retarget(matter_id, prior['attempt_id'], options['target'])
            attempt_id = self.repository.begin_attempt(matter_id, source, **options)
            if prior and attempt_id != prior['attempt_id']:
                binding = self.binding(matter_id)
                binding['dirty'].discard(prior['attempt_id'])
                binding['attempt_roots'].pop(prior['attempt_id'], None)
                binding['validated'].pop(prior['attempt_id'], None)
                self.repository.save_dependencies(matter_id, attempt_id,
                    {'restarts': state.get('restarts', 0)}, options['input_revision'])
            return attempt_id

    def root_revision(self, matter_id, attempt):
        binding = self.binding(matter_id)
        binding['attempt_roots'][attempt['attempt_id']] = attempt['root_key']
        return binding['roots'].get(attempt['root_key'], binding['scan_start'])

    def input_revision(self, matter_id):
        binding = self.binding(matter_id)
        return f"{binding['source_identity']}:{binding['revision']}:{binding['human']}"

    def dependency_state(self, attempt):
        state = attempt.get('dependencies', {})
        return state if isinstance(state, dict) else {}

    @staticmethod
    def dependency_stamp(state):
        return tuple(state.get(key) for key in ('epoch', 'revision', 'human', 'hash'))

    def dependencies_current(self, matter_id, attempt):
        binding = self.binding(matter_id)
        state = self.dependency_state(attempt)
        return (attempt['input_revision'] == self.input_revision(matter_id)
                and state.get('epoch') == binding['source_identity']
                and state.get('revision') == self.root_revision(matter_id, attempt)
                and state.get('state') == 'complete'
                and (state.get('owner') == self.epoch
                     or binding['validated'].get(attempt['attempt_id']) == self.dependency_stamp(state)))

    def attempt_current(self, matter_id, attempt):
        binding = self.binding(matter_id)
        return (not self.dependency_state(attempt).get('paused')
            and (attempt['status'] == 'failed' or self.dependencies_current(matter_id, attempt))
            and attempt['parser_revision'] == PARSER_REVISION
            and attempt['definition_digest'] == binding['digest']
            and self.valid(matter_id, source_from_attempt(attempt), attempt['target'], binding['store'].revision,
                           check_path=False))

    def reconcile_attempt(self, matter_id, attempt):
        """Validate one bounded root window; only changed dependencies rematch."""
        binding = self.binding(matter_id)
        with binding['store'].mutation_guard(), self.repository.transaction():
            attempt = self.repository.attempt(matter_id, attempt['attempt_id'])
            state = self.dependency_state(attempt)
            if state.get('paused'):
                self.remember_pause(matter_id, attempt, state)
                return attempt
            revision = binding['store'].revision
            root_revision = self.root_revision(matter_id, attempt)
            source = source_from_attempt(attempt)
            target = binding['target'] or attempt['target']
            if not self.valid(matter_id, source, target, revision, check_path=False) or (
                    attempt['parser_revision'] != PARSER_REVISION or attempt['definition_digest'] != binding['digest']):
                if self.repository.catalog(matter_id, source.document_id) is None:
                    self.repository.update_attempt(matter_id, attempt['attempt_id'], status='stale', phase='stale')
                    binding['dirty'].discard(attempt['attempt_id'])
                    binding['attempt_roots'].pop(attempt['attempt_id'], None)
                    binding['validated'].pop(attempt['attempt_id'], None)
                self.discovery[matter_id] = ''
                self.discovery_done.discard(matter_id)
            else:
                state = self.dependency_state(attempt)
                if (state.get('epoch') != binding['source_identity'] or state.get('revision') != root_revision
                        or state.get('human') != binding['human'] or state.get('owner') != self.epoch):
                    was_complete = attempt['status'] == 'complete' or state.get('was_complete', False)
                    counted = bool(state.get('epoch') and state.get('owner') == self.epoch
                                   and attempt['status'] != 'failed')
                    if counted:
                        state = self.restart_state(matter_id, attempt, dict(state, resume_phase=attempt['phase']))
                        if state.get('paused'):
                            self.changed(matter_id)
                            return self.repository.attempt(matter_id, attempt['attempt_id'])
                    state = dict(restarts=state.get('restarts', 0), owner=self.epoch, counted=counted, was_complete=was_complete,
                                 epoch=binding['source_identity'], revision=root_revision, human=binding['human'], state='scan',
                                 hash='', after='', previous=state.get('previous', state.get('hash', '')),
                                 reopened=bool((attempt['status'] == 'complete' and not state) or state.get('epoch')
                                     and (state['epoch'] != binding['source_identity'] or state.get('human') != binding['human'])))
                    if attempt['status'] == 'complete':
                        self.repository.update_attempt(matter_id, attempt['attempt_id'], status='building', phase='complete')
                        attempt = self.repository.attempt(matter_id, attempt['attempt_id'])
                if state.get('state') != 'complete':
                    rows = self.repository.candidates(matter_id, source.root_key, after=state.get('after', ''), limit=8)
                    for row in rows:
                        snapshot = binding['store'].source_changes.read([row['document_id']])
                        budget = self.repository.budget()
                        if budget is not None:
                            budget.charge(len(snapshot), credit=not snapshot)
                        for item in snapshot:
                            binding['inputs'][item.document_id] = (self.input_identity(item), source.root_key)
                        data = [(item.document_id, self.input_identity(item)) for item in snapshot]
                        state['hash'] = hashlib.sha256((state.get('hash', '') + json.dumps(data, sort_keys=True)).encode()).hexdigest()
                        state['after'] = row['document_id']
                    if not rows:
                        state['state'] = 'complete'
                        if (state.get('reopened') or state.get('previous') and state['previous'] != state['hash']) and attempt['phase'] != 'parse' and attempt['status'] != 'failed':
                            if not state.get('counted'):
                                state = self.restart_state(matter_id, attempt, dict(state, resume_phase=attempt['phase']))
                                if state.get('paused'):
                                    self.changed(matter_id)
                                    return self.repository.attempt(matter_id, attempt['attempt_id'])
                            self.repository.restart_matching(matter_id, attempt['attempt_id'], target,
                                                             input_revision=self.input_revision(matter_id) + ":pending")
                            attempt = self.repository.attempt(matter_id, attempt['attempt_id'])
                        state.pop('previous', None)
                        state.pop('reopened', None)
                        if state.get('was_complete') and attempt['phase'] == 'complete':
                            self.repository.update_attempt(matter_id, attempt['attempt_id'], status='complete', phase='complete')
                            state['restarts'] = 0
                        binding['dirty'].discard(attempt['attempt_id'])
                        binding['validated'][attempt['attempt_id']] = self.dependency_stamp(state)
                    self.repository.save_dependencies(matter_id, attempt['attempt_id'], state,
                        self.input_revision(matter_id) if state['state'] == 'complete' else self.input_revision(matter_id) + ':pending')
                if attempt['target'] != target:
                    self.repository.retarget(matter_id, attempt['attempt_id'], target)
        self.changed(matter_id)
        return self.repository.attempt(matter_id, attempt['attempt_id'])

    def revision(self, matter_id):
        return self.marks.get(matter_id, 0)

    def changed(self, matter_id):
        self.marks[matter_id] = self.revision(matter_id) + 1

    def binding(self, matter_id):
        if matter_id not in self.bindings:
            raise ProductionUnavailable("Production work requires an authorized source-store binding.")
        return self.bindings[matter_id]

    def valid(self, matter_id, source, target, revision, *, check_path=True):
        binding = self.binding(matter_id)
        if binding["store"].revision != revision or self.repository.identifier_stamp(matter_id) != binding['human']:
            return False
        if binding["target"] is not None and target != binding["target"]:
            return False
        validator = binding["validator"]
        if validator is not None and not validator(matter_id, target):
            return False
        current = self.repository.catalog(matter_id, source.document_id)
        try:
            document = binding["store"].get(source.document_id)
            if check_path:
                binding["store"].source_path(source.document_id)
        except (KeyError, UploadProblem, OSError):
            return False
        return bool(current and current["version_id"] == source.version_id
                    and document.digest == source.digest and document.version_id == source.version_id
                    and current["source_state"] == "ready" and current["origin"] == "upload"
                    and self.repository.provenance(matter_id, source.document_id, source.version_id) == source.root_key)

    def import_source(self, matter_id, source, path, definition, *, target,
                      max_records=MAX_BATCH_RECORDS, max_bytes=MAX_BATCH_BYTES):
        if type(max_records) is not int or not 1 <= max_records <= MAX_BATCH_RECORDS:
            raise ValueError("The production batch record bound is invalid.")
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BATCH_BYTES:
            raise ValueError("The production batch byte bound is invalid.")
        binding = self.binding(matter_id)
        self.context.ensure_capacity(max_bytes)
        with binding["store"].mutation_guard():
            revision = binding["store"].revision
            if not self.valid(matter_id, source, target, revision):
                raise ProductionStale("The admitted source or production root changed.")
            digest = definition_digest(definition)
            prior = self.repository.current_attempt(matter_id, source.document_id)
            identity = ("document_id", "version_id", "digest", "name", "relative_path", "root_key")
            reusable = (prior is not None and tuple(prior[key] for key in identity) == (source.document_id, source.version_id, source.digest,
                    source.name, source.relative_path, source.root_key)
                and prior["parser_revision"] == PARSER_REVISION and prior["definition_digest"] == digest
                )
            detected = prior["source_format"] if reusable else load_files.classify_source(path, name=source.name)
            recognized = bool(load_files.record_category(detected))
            if recognized:
                source = replace(source, source_format=detected)
            attempt_id = self.begin_attempt(matter_id, source,
                parser_revision=PARSER_REVISION, definition_digest=digest, target=target,
                input_revision=self.input_revision(matter_id) + ":pending")
            if not recognized:
                with self.repository.step(matter_id, attempt_id, target=target,
                        validate=lambda: self.valid(matter_id, source, target, revision)):
                    self.repository.update_attempt(matter_id, attempt_id, status="failed", phase="error",
                        failure="The admitted load-file format could not be identified; no records were interpreted.")
                self.changed(matter_id)
                return self.repository.attempt(matter_id, attempt_id)
            attempt = self.repository.attempt(matter_id, attempt_id)
            if attempt["status"] != "building" or attempt["phase"] != "parse":
                return attempt
            fingerprint = tuple(attempt["cursor"].get("fingerprint") or source_fingerprint(path, source.digest))
            batch, size, exhausted = [], 0, True
            cursor = attempt["cursor"].get("parser")
            failure = ""

            def collect(event):
                nonlocal size, cursor
                amount = decoded_payload_bytes(event)
                if batch and (len(batch) >= max_records or size + amount > max_bytes):
                    raise BatchBoundary
                budget = self.repository.budget()
                if budget is not None:
                    budget.charge(1)
                batch.append(event)
                size += amount
                cursor = event.next_cursor
                if event.next_cursor is not None and (len(batch) >= max_records or size >= max_bytes):
                    raise BatchBoundary

            iterator = load_files.parse_events(path, source_format=source.source_format,
                                              on_event=collect, cursor=cursor)
            try:
                for unused in iterator:
                    del unused
            except BatchBoundary:
                exhausted = False
            except (OSError, ValueError) as error:
                failure = "The load file could not be read completely; saved records remain incomplete."
                del error
            finally:
                iterator.close()
            current_stat = Path(path).stat(follow_symlinks=False)
            if fingerprint != (current_stat.st_dev, current_stat.st_ino, current_stat.st_size,
                               current_stat.st_mtime_ns, current_stat.st_ctime_ns):
                raise ProductionStale("The load file changed while its records were read.")
        if size > max_bytes:
            self.context.ensure_capacity(size)
        with binding["store"].mutation_guard(), self.repository.step(matter_id, attempt_id,
                target=target, expected_cursor=attempt["cursor"],
                validate=lambda: self.valid(matter_id, source, target, revision)):
            if batch:
                self.repository.append_events(matter_id, attempt_id, batch, cursor={"parser": cursor})
                for event in batch:
                    if event.kind == "accepted":
                        self.prepare_record(matter_id, attempt_id, event, definition)
            current = self.repository.attempt(matter_id, attempt_id)
            failure = failure or current["file_failure"]
            self.repository.update_attempt(matter_id, attempt_id,
                status="failed" if failure else "building", phase="error" if failure else "match" if exhausted else "parse",
                cursor={"ordinal": 0} if exhausted else {"parser": cursor, "fingerprint": fingerprint}, failure=failure)
        self.changed(matter_id)
        return self.repository.attempt(matter_id, attempt_id)

    def prepare_record(self, matter_id, attempt_id, event, definition):
        if event.category == "document":
            projection = project_record(event.headers, event.cells, definition)
            keys = projection.keys
            family = family_record(projection.values).attachment_bounds
            self.repository.set_projection_fields(matter_id, attempt_id, event.ordinal,
                semantic=projection.values, projection_fingerprint=projection.fingerprint,
                path_key=keys.get("path", ""), begin_key=keys.get("begin_bates", ""),
                end_key=keys.get("end_bates", ""), control_key=keys.get("control_number", ""),
                family_begin_key=keys.get("begin_attachment", ""), family_end_key=keys.get("end_attachment", ""),
                family_prefix=family[0] if family else "", family_low=interval_key(family[1]) if family else "",
                family_high=interval_key(family[2]) if family else "")
            self.repository.set_diagnostics(matter_id, attempt_id, event.ordinal, projection.diagnostics)
        elif event.category == "page":
            page = load_files.record_semantics(event)
            self.repository.set_projection_fields(matter_id, attempt_id, event.ordinal, category="page",
                page_label_key=label_key(page["page_label"]), path_key=path_key(page["path"]),
                group_key=label_key(page["page_label"]) if page["document_break"] else "")

    def import_legacy_table(self, matter_id, source, table, definition, *, target, max_records=MAX_BATCH_RECORDS):
        """Persist every existing legacy row, including rows its display calls extras."""
        binding = self.binding(matter_id)
        self.context.ensure_capacity(MAX_BATCH_BYTES)
        with binding["store"].mutation_guard():
            revision = binding["store"].revision
            attempt_id = self.begin_attempt(matter_id, source,
                parser_revision=PARSER_REVISION, definition_digest=definition_digest(definition), target=target,
                input_revision=self.input_revision(matter_id) + ":pending")
            attempt = self.repository.attempt(matter_id, attempt_id)
            if attempt["phase"] != "parse" or attempt["status"] != "building":
                return attempt
            position, batch, size = attempt["cursor"].get("legacy_position", 0), [], 0
            if not isinstance(table.rows, (tuple, list)):
                raise ValueError("Legacy imports require the existing retained row sequence.")
            while position < len(table.rows) and len(batch) < max_records:
                row = table.rows[position]
                event = RecordEvent(kind="accepted", category="document", source_format=source.source_format,
                    ordinal=position + 1, line=row.line, headers=tuple(table.headers),
                    cells=tuple(row.values.get(header, "") for header in table.headers),
                    diagnostics=(), optional_values=(), next_cursor=None)
                amount = decoded_payload_bytes(event)
                if batch and size + amount > MAX_BATCH_BYTES:
                    break
                batch.append(event)
                size += amount
                position += 1
                if size >= MAX_BATCH_BYTES:
                    break
        if size > MAX_BATCH_BYTES:
            self.context.ensure_capacity(size)
        with binding["store"].mutation_guard(), self.repository.step(matter_id, attempt_id, target=target,
                expected_cursor=attempt["cursor"],
                validate=lambda: self.valid(matter_id, source, target, revision)):
            self.repository.append_events(matter_id, attempt_id, batch)
            for event in batch:
                self.prepare_record(matter_id, attempt_id, event, definition)
            complete = position == len(table.rows)
            self.repository.update_attempt(matter_id, attempt_id,
                status="failed" if complete and table.problem else "building",
                phase="error" if complete and table.problem else "match" if complete else "parse",
                cursor={"ordinal": 0} if complete else {"legacy_position": position},
                failure="The legacy candidate was incompletely read." if complete and table.problem else "")
        self.changed(matter_id)
        return self.repository.attempt(matter_id, attempt_id)

    def import_legacy_stream(self, matter_id, source, path, *, target):
        """Resume the parent's CSV grammar without retaining its row population."""
        binding = self.binding(matter_id)
        self.context.ensure_capacity(MAX_BATCH_BYTES)
        with binding['store'].mutation_guard():
            revision = binding['store'].revision
            if not self.valid(matter_id, source, target, revision):
                raise ProductionStale('The admitted source changed.')
            attempt_id = self.begin_attempt(matter_id, source, parser_revision=PARSER_REVISION,
                definition_digest=binding['digest'], target=target, input_revision=self.input_revision(matter_id) + ":pending")
            attempt = self.repository.attempt(matter_id, attempt_id)
            if attempt['status'] != 'building' or attempt['phase'] != 'parse':
                return attempt
            parser = attempt['cursor'].get('legacy_parser')
            failure = ''
            try:
                with path.open('rb') as stream:
                    raw = stream.read(load_files.MAX_LOAD_FILE_BYTES + 1)
                if len(raw) > load_files.MAX_LOAD_FILE_BYTES:
                    raise ValueError('legacy byte limit')
                text = raw.decode('utf-8-sig')
            except (UnicodeError, ValueError):
                failure = 'The legacy candidate was incompletely read.'
                text = ''
            batch = []
            for unused in range(4):
                if failure:
                    break
                parser, row, touched = load_files.resume_record(text, source.name, binding['definition'], parser)
                budget = self.repository.budget()
                if budget is not None:
                    budget.charge(touched, credit=not touched)
                if row is not None:
                    batch.append(RecordEvent('accepted', 'document', source.source_format,
                        parser['count'], row.line, tuple(parser['headers']),
                        tuple(row.values[header] for header in parser['headers'])))
                if parser['eof']:
                    break
        with binding['store'].mutation_guard(), self.repository.step(matter_id, attempt_id, target=target,
                expected_cursor=attempt['cursor'], validate=lambda: self.valid(matter_id, source, target, revision)):
            if batch:
                self.repository.append_events(matter_id, attempt_id, batch)
                for event in batch:
                    self.prepare_record(matter_id, attempt_id, event, binding['definition'])
            failure = failure or ('The legacy candidate was incompletely read.' if parser and parser['problem'] else '')
            complete = bool(parser and parser['eof'])
            self.repository.update_attempt(matter_id, attempt_id,
                status='failed' if failure else 'building', phase='error' if failure else 'match' if complete else 'parse',
                cursor={'ordinal': 0} if complete else {'legacy_parser': parser}, failure=failure)
        self.changed(matter_id)
        return self.repository.attempt(matter_id, attempt_id)

    def independent_values(self, matter_id, document_id, attempt_id):
        for row in self.repository.document_records(matter_id, document_id, limit=1,
                preferred_formats=load_files.document_format_order()):
            if row["attempt_id"] != attempt_id and row["association_status"] == "matched":
                return dict(row["semantic"])
        return {}

    def match_step(self, matter_id, attempt, definition):
        category = load_files.record_category(attempt["source_format"])
        if category == "page":
            self.repository.update_attempt(matter_id, attempt["attempt_id"], status="building",
                                           phase="groups", cursor={"ordinal": 0})
            return
        dependencies = load_files.matching_dependencies(attempt["source_format"])
        if dependencies and self.repository.root_work(
                matter_id, attempt["root_key"], formats=dependencies, excluding=attempt["attempt_id"]):
            return
        cursor = attempt["cursor"]
        active = cursor.get("active")
        rows = self.repository.records(matter_id, attempt["attempt_id"], category="document",
                                       after=cursor.get("ordinal", 0), limit=1)
        if not rows:
            self.repository.update_attempt(matter_id, attempt["attempt_id"], status="building",
                                           phase="conflicts", cursor={"ordinal": 0})
            return
        row = rows[0]
        projection = project_record(row["headers"], row["cells"], definition)
        status, document_id, basis = "unmatched", "", ""
        if row["kind"] == "rejected":
            status = "rejected"
        elif not attempt["root_key"]:
            status = "ambiguous_production"
        else:
            active = active or initial_match(projection)
            candidates = self.repository.candidate_lookup(matter_id, attempt["root_key"], paths=projection.paths,
                labels={key: projection.keys[key] for key in ("begin_bates", "end_bates", "control_number")},
                after=active["after"], limit=16, excluded_document_id=attempt["document_id"])
            for candidate in candidates:
                active["after"] = candidate["document_id"]
                if (candidate["root_key"] != attempt["root_key"] or candidate["document_id"] == attempt["document_id"]
                        or candidate["source_state"] != "ready"):
                    continue
                observe_candidate(active, projection, candidate)
            if candidates:
                self.repository.update_attempt(matter_id, attempt["attempt_id"], status="building", phase="match",
                    cursor={"ordinal": cursor.get("ordinal", 0), "active": active})
                return
            document_id, basis, status = finish_match(active, projection)
            if document_id:
                candidate = self.repository.catalog(matter_id, document_id)
                candidate["produced_values"] = self.independent_values(matter_id, document_id, attempt["attempt_id"])
                if not candidate_agrees(projection, candidate):
                    status = "conflicting_match"
            if self.repository.team_conflicts(matter_id, literal_identifiers(projection)):
                status = "team_identifier_conflict"
        self.repository.set_association(matter_id, attempt["attempt_id"], row["ordinal"], category="document",
            document_id=document_id, basis=basis, status=status, generation=attempt["generation"],
            family={"root": "", "parent": "", "order": ""})
        self.repository.update_attempt(matter_id, attempt["attempt_id"], status="building", phase="match",
                                       cursor={"ordinal": row["ordinal"]})

    def conflict_step(self, matter_id, attempt, definition):
        cursor = attempt["cursor"]
        rows = self.repository.records(matter_id, attempt["attempt_id"], category="document",
                                       after=cursor.get("ordinal", 0), limit=1)
        if not rows:
            self.repository.update_attempt(matter_id, attempt["attempt_id"], status="building",
                                           phase="families", cursor={"ordinal": 0})
            return
        row = rows[0]
        if row["kind"] == "accepted" and row["begin_key"]:
            duplicates = self.repository.key_count(matter_id, key="begin_key", value=row["begin_key"],
                root_key=attempt["root_key"], maximum=2)
            if duplicates > 1:
                self.repository.set_diagnostics(matter_id, attempt["attempt_id"], row["ordinal"],
                    sorted(set(row.get("association_diagnostics", ())) | {"duplicate_begin"}))
        active = cursor.get("active", {"after": None, "conflict": ""})
        if row["document_id"] and row["kind"] == "accepted":
            projection = project_record(row["headers"], row["cells"], definition)
            others = self.repository.competing_records(matter_id, row["document_id"], after=active["after"], limit=1)
            for other in others:
                active["after"] = [other["attempt_id"], other["ordinal"]]
                if (other["attempt_id"], other["ordinal"]) == (row["attempt_id"], row["ordinal"]):
                    continue
                other_projection = project_record(other["headers"], other["cells"], definition)
                policy = load_files.comparison_policy(other["source_format"], row["source_format"])
                if policy == "compatible" and not compatible_metadata(projection, other_projection):
                    active["conflict"] = "conflicting_load_file_metadata"
                elif policy != "compatible" and other_projection.fingerprint != projection.fingerprint:
                    active["conflict"] = "conflicting_dat_rows"
            if others:
                self.repository.update_attempt(matter_id, attempt["attempt_id"], status="building", phase="conflicts",
                    cursor={"ordinal": cursor.get("ordinal", 0), "active": active})
                return
            if active["conflict"]:
                self.repository.set_association(matter_id, attempt["attempt_id"], row["ordinal"], category="document",
                    document_id=row["document_id"], basis=row["basis"], status=active["conflict"], generation=attempt["generation"])
        self.repository.update_attempt(matter_id, attempt["attempt_id"], status="building", phase="conflicts",
                                       cursor={"ordinal": row["ordinal"]})

    def group_step(self, matter_id, attempt):
        cursor = attempt["cursor"]
        rows = self.repository.records(matter_id, attempt["attempt_id"], category="page",
                                       after=cursor.get("ordinal", 0), limit=1)
        if not rows:
            self.repository.update_attempt(matter_id, attempt["attempt_id"], status="building",
                                           phase="group_check", cursor={"ordinal": 0})
            return
        row, group = rows[0], cursor.get("group", 0)
        values = load_files.record_semantics(row)
        status = "unmatched"
        if row["kind"] != "accepted":
            group, status = 0, "rejected"
        elif not values["break_valid"]:
            group, status = 0, "invalid_document_break"
        elif not values["valid"]:
            group, status = 0, "rejected"
        elif values["document_break"]:
            group = row["ordinal"]
        elif not group:
            status = "orphan_page"
        self.repository.set_page_group(matter_id, attempt["attempt_id"], row["ordinal"],
            group_ordinal=group or None,
            group_key=label_key(values["page_label"]) if values["document_break"] else "",
            supplied_count=values["page_count"])
        self.repository.set_association(matter_id, attempt["attempt_id"], row["ordinal"], category="page",
            status=status, generation=attempt["generation"], group_ordinal=group or None)
        self.repository.update_attempt(matter_id, attempt["attempt_id"], status="building", phase="groups",
                                       cursor={"ordinal": row["ordinal"], "group": group})

    def group_check_step(self, matter_id, attempt):
        formats = tuple(key for key in load_files.SOURCE_FORMATS if load_files.record_category(key) == "document")
        if self.repository.root_work(matter_id, attempt["root_key"], formats=formats,
                                     excluding=attempt["attempt_id"]):
            return
        cursor = attempt["cursor"]
        active = cursor.get("active")
        if active is None:
            rows = self.repository.records(matter_id, attempt["attempt_id"], category="page",
                                           after=cursor.get("ordinal", 0), limit=1)
            if not rows:
                self.repository.update_attempt(matter_id, attempt["attempt_id"], status="building",
                                               phase="publish", cursor={"ordinal": 0})
                return
            row = rows[0]
            if row["group_ordinal"] != row["ordinal"]:
                self.repository.update_attempt(matter_id, attempt["attempt_id"], status="building",
                                               phase="group_check", cursor={"ordinal": row["ordinal"]})
                return
            active = {"group": row["ordinal"], "key": row["page_label_key"], "phase": "starts",
                      "after": None, "starts": 0, "document": "", "class": "", "class_format": "", "count": 0,
                      "last": row["ordinal"], "problem": "", "supplied": load_files.record_semantics(row)["page_count"]}
            if not attempt["root_key"]:
                active["problem"] = "ambiguous_production"
        if active["phase"] == "starts":
            active["starts"] = self.repository.key_count(matter_id, key="group_key", value=active["key"],
                category="page", root_key=attempt["root_key"], maximum=2)
            if active["starts"] != 1:
                active["problem"] = "ambiguous_page_group"
            active.update(phase="documents", after=None)
        elif active["phase"] == "documents":
            rows = self.repository.records_by_key(matter_id, key="begin_key", value=active["key"],
                root_key=attempt["root_key"], after=active["after"], limit=1)
            for row in rows:
                active["after"] = [row["attempt_id"], row["ordinal"]]
                if row["association_status"] != "matched" or not row["document_id"]:
                    active["problem"] = "ambiguous_page_group"
                    continue
                if active["document"] and (active["document"] != row["document_id"]
                        or (load_files.comparison_policy(active["class_format"], row["source_format"]) == "exact"
                            and active["class"] != row["projection_fingerprint"])):
                    active["problem"] = "ambiguous_page_group"
                active["document"], active["class"] = row["document_id"], row["projection_fingerprint"]
                active["class_format"] = row["source_format"]
            if not rows:
                if not active["document"]:
                    active["problem"] = active["problem"] or "unmatched"
                active.update(phase="pages", after=0)
        elif active["phase"] == "pages":
            rows = self.repository.records(matter_id, attempt["attempt_id"], category="page",
                after=active["after"], group_ordinal=active["group"], limit=1)
            for row in rows:
                active["after"], active["last"] = row["ordinal"], row["ordinal"]
                active["count"] += 1
                matches = self.repository.key_count(matter_id, key="page_label_key", value=row["page_label_key"],
                    category="page", root_key=attempt["root_key"], maximum=2)
                if matches != 1:
                    active["problem"] = "ambiguous_page_key"
                if self.repository.team_conflicts(matter_id, (load_files.record_semantics(row)["page_label"],)):
                    active["problem"] = "team_identifier_conflict"
            if not rows:
                supplied = active["supplied"]
                if supplied and (not supplied.isascii() or not supplied.isdigit()
                                 or len(supplied) > 19 or int(supplied) != active["count"]):
                    active["problem"] = "page_count_mismatch"
                active.update(phase="apply", after=0)
        else:
            rows = self.repository.records(matter_id, attempt["attempt_id"], category="page",
                after=active["after"], group_ordinal=active["group"], limit=1)
            for row in rows:
                self.repository.set_association(matter_id, attempt["attempt_id"], row["ordinal"], category="page",
                    document_id=active["document"] if not active["problem"] else "", basis="production Begin Bates",
                    status=active["problem"] or "matched", generation=attempt["generation"], group_ordinal=active["group"])
                self.repository.set_page_group(matter_id, attempt["attempt_id"], row["ordinal"],
                    group_ordinal=active["group"], group_key=active["key"] if row["ordinal"] == active["group"] else "",
                    supplied_count=active["supplied"], observed_count=active["count"])
                active["after"] = row["ordinal"]
            if not rows:
                self.repository.update_attempt(matter_id, attempt["attempt_id"], status="building",
                                               phase="group_check", cursor={"ordinal": active["last"]})
                return
        self.repository.update_attempt(matter_id, attempt["attempt_id"], status="building", phase="group_check",
                                       cursor={"ordinal": cursor.get("ordinal", 0), "active": active})

    def family_step(self, matter_id, attempt):
        formats = tuple(name for name in load_files.document_format_order()
            if attempt["source_format"] not in load_files.matching_dependencies(name))
        if attempt["source_format"] not in {"csv", "tsv"} and self.repository.root_work(
                matter_id, attempt["root_key"], formats=formats, phases=("parse", "match", "conflicts"),
                excluding=attempt["attempt_id"]):
            return
        cursor = attempt["cursor"]
        rows = self.repository.records(matter_id, attempt["attempt_id"], category="document",
                                       after=cursor.get("ordinal", 0), limit=1)
        if not rows:
            self.repository.update_attempt(matter_id, attempt["attempt_id"], status="building",
                                           phase="publish", cursor={"ordinal": 0})
            return
        row = rows[0]
        record = family_record(dict(row["semantic"]))
        if cursor.get("active") is None and (row["family_checked_generation"] == attempt["generation"] or not record.declared):
            self.repository.update_attempt(matter_id, attempt["attempt_id"], status="building", phase="families",
                                           cursor={"ordinal": row["ordinal"]})
            return
        pair = [row["family_begin_key"], row["family_end_key"]]
        active = cursor.get("active") or {"pair": pair, "after": None, "parents": [], "terminals": [],
                                          "diagnostics": [], "phase": "members"}
        if active["phase"] == "members":
            members = self.repository.family_members(matter_id, attempt["root_key"], begin=pair[0], end=pair[1],
                                                     after=active["after"], limit=1)
            for member in members:
                active["after"] = [member["attempt_id"], member["ordinal"]]
                member_record = family_record(dict(member["semantic"]))
                active["diagnostics"] = sorted(set(active["diagnostics"]) | set(member_record.diagnostics))
                if (member_record.begin_attachment, member_record.end_attachment) != (
                        record.begin_attachment, record.end_attachment):
                    active["diagnostics"] = sorted(set(active["diagnostics"]) | {"family_conflict"})
                if member["association_status"] != "matched":
                    active["diagnostics"] = sorted(set(active["diagnostics"]) | {"family_conflict"})
                for key, field in (("parents", "begin_key"), ("terminals", "end_key")):
                    if member[field] == pair[0 if key == "parents" else 1] and member["document_id"]:
                        values = active[key]
                        if member["document_id"] not in values and len(values) < 2:
                            values.append(member["document_id"])
            if not members:
                active.update(phase="overlaps", after=None)
        elif active["phase"] == "overlaps":
            others = self.repository.interval_overlap(matter_id, attempt["root_key"], prefix=row["family_prefix"],
                low=row["family_low"], high=row["family_high"], begin=pair[0], end=pair[1],
                limit=1) if record.attachment_bounds else ()
            for other in others:
                other_record = family_record(dict(other["semantic"]))
                active["diagnostics"] = sorted(set(active["diagnostics"]) | set(family_pair_diagnostics(record, other_record)))
            findings = family_diagnostics(record, parent_count=len(active["parents"]), terminal_count=len(active["terminals"]))
            active["diagnostics"] = sorted(set(active["diagnostics"]) | set(findings))
            active.update(phase="apply", after=None)
        else:
            members = self.repository.family_members(matter_id, attempt["root_key"], begin=pair[0], end=pair[1],
                                                     after=active["after"], limit=1)
            for member in members:
                active["after"] = [member["attempt_id"], member["ordinal"]]
                if member["attempt_id"] != attempt["attempt_id"]:
                    continue
                family = {"root": "", "parent": "", "order": ""}
                if not active["diagnostics"] and active["parents"] and member["association_status"] == "matched":
                    parent = active["parents"][0]
                    family = {"root": parent, "parent": "" if member["document_id"] == parent else parent,
                        "order": json.dumps(family_order_key(family_record(dict(member["semantic"])),
                            member["source_id"], member["ordinal"]), separators=(",", ":"))}
                self.repository.set_association(matter_id, attempt["attempt_id"], member["ordinal"], category="document",
                    document_id=member["document_id"], basis=member["basis"], status=member["association_status"],
                    generation=attempt["generation"], family=family)
                self.repository.set_diagnostics(matter_id, attempt["attempt_id"], member["ordinal"],
                    sorted(set(member.get("association_diagnostics", ())) | set(active["diagnostics"])))
                self.repository.mark_family_checked(matter_id, attempt["attempt_id"], member["ordinal"], attempt["generation"])
            if not members:
                self.repository.update_attempt(matter_id, attempt["attempt_id"], status="building", phase="families",
                                               cursor={"ordinal": row["ordinal"]})
                return
        self.repository.update_attempt(matter_id, attempt["attempt_id"], status="building", phase="families",
                                       cursor={"ordinal": cursor.get("ordinal", 0), "active": active})

    def publish_step(self, matter_id, attempt):
        category = load_files.record_category(attempt["source_format"])
        rows = self.repository.records(matter_id, attempt["attempt_id"], category=category,
                                       after=attempt["cursor"].get("ordinal", 0), limit=1)
        if rows:
            row = rows[0]
            labels = (load_files.record_semantics(row)["page_label"],) if category == "page" else tuple(
                value for key, value in row["semantic"] if key in {"begin_bates", "end_bates", "control_number"})
            if row["association_status"] == "matched" and self.repository.team_conflicts(matter_id, labels):
                self.repository.set_association(matter_id, attempt["attempt_id"], row["ordinal"], category=category,
                    document_id=row["document_id"], basis=row["basis"], status="team_identifier_conflict",
                    generation=attempt["generation"], family={"root": "", "parent": "", "order": ""})
            self.repository.update_attempt(matter_id, attempt["attempt_id"], status="building", phase="publish",
                                           cursor={"ordinal": row["ordinal"]})
        else:
            self.repository.update_attempt(matter_id, attempt["attempt_id"], status="complete", phase="complete",
                cursor={"eof": True}, counts=self.repository.coverage(matter_id, attempt["attempt_id"]))
            self.repository.save_dependencies(matter_id, attempt['attempt_id'],
                dict(self.dependency_state(attempt), restarts=0), attempt['input_revision'])

    def discover_source(self, matter_id):
        binding = self.binding(matter_id)
        if matter_id in self.discovery_done:
            return None
        rows = self.repository.source_catalog_page(matter_id, after=self.discovery.get(matter_id, ""), limit=8)
        if not rows:
            self.discovery[matter_id] = ""
            self.discovery_done.add(matter_id)
            return None
        for row in rows:
            self.discovery[matter_id] = row["document_id"]
            suffix = load_files.source_format(row["display_name"])
            if not suffix or row["source_state"] != "ready" or row["origin"] != "upload":
                continue
            document = binding["store"].get(row["document_id"])
            source = SourceIdentity(document.document_id, document.version_id, document.digest,
                document.display_name, document.relative_path or document.display_name, suffix,
                self.repository.provenance(matter_id, document.document_id, document.version_id))
            prior = self.repository.current_attempt(matter_id, source.document_id)
            if prior is not None and self.dependency_state(prior).get('paused'):
                continue
            if (prior is not None and prior["version_id"] == source.version_id and prior["digest"] == source.digest
                    and prior["definition_digest"] == binding["digest"] and prior["parser_revision"] == PARSER_REVISION):
                continue
            target = binding["target"] or f"{self.epoch}:{binding['store'].revision}:{binding['digest']}"
            return source, target
        return None

    def advance(self, matter_id, *, max_steps=128):
        if type(max_steps) is not int or not 1 <= max_steps <= 128:
            raise ValueError("The production request-step bound is invalid.")
        binding = self.binding(matter_id)
        binding['human'] = self.repository.identifier_stamp(matter_id)
        steps = 0
        self.last_budget = None
        from case_intelligence.modules.discovery_review.stored_index.models import Budget
        from case_intelligence.modules.discovery_review.stored_index.rebuild import MAX_STEP_ROWS
        for unused in range(max_steps):
            del unused
            self.context.ensure_capacity(MAX_BATCH_BYTES)
            if not self.step_lock.acquire(blocking=False):
                break
            discovered = selected = None
            budget = Budget(MAX_STEP_ROWS, MAX_STEP_ROWS - 16)
            steps += 1
            self.last_budget = budget
            with self.repository.counted(budget):
                try:
                    discovered = self.discover_source(matter_id)
                    attempts = self.repository.attempts(matter_id, after=self.positions.get(matter_id, ""), limit=1)
                    if not attempts:
                        self.positions[matter_id] = ""
                        attempts = self.repository.attempts(matter_id, limit=1)
                    if attempts:
                        selected = attempts[0]
                        self.positions[matter_id] = selected["attempt_id"]
                        if self.dependency_state(selected).get('paused'):
                            self.remember_pause(matter_id, selected, self.dependency_state(selected))
                            selected = None
                        elif not self.attempt_current(matter_id, selected) or not self.dependencies_current(matter_id, selected):
                            selected = self.reconcile_attempt(matter_id, selected)
                            selected = None
                    if selected is not None and selected["status"] == "building" and selected["phase"] != "parse":
                        source = source_from_attempt(selected)
                        with binding["store"].mutation_guard():
                            revision = binding["store"].revision
                            with self.repository.step(matter_id, selected["attempt_id"], target=selected["target"],
                                    expected_cursor=selected["cursor"],
                                    validate=lambda: self.valid(matter_id, source, selected["target"], revision)):
                                phase = selected["phase"]
                                if phase == "match":
                                    self.match_step(matter_id, selected, binding["definition"])
                                elif phase == "conflicts":
                                    self.conflict_step(matter_id, selected, binding["definition"])
                                elif phase == "groups":
                                    self.group_step(matter_id, selected)
                                elif phase == "group_check":
                                    self.group_check_step(matter_id, selected)
                                elif phase == "families":
                                    self.family_step(matter_id, selected)
                                elif phase == "publish":
                                    self.publish_step(matter_id, selected)
                                else:
                                    raise ProductionUnavailable("The saved production continuation is unsupported.")
                        self.changed(matter_id)
                        selected = None
                except ProductionStale:
                    if selected is not None:
                        self.changed(matter_id)
                    selected = None
                finally:
                    try:
                        if discovered is not None:
                            source, target = discovered
                            self.read_source(matter_id, source, target, verify_digest=True)
                        elif selected is not None and selected['status'] == 'building' and selected['phase'] == 'parse':
                            source = source_from_attempt(selected)
                            self.read_source(matter_id, source, selected['target'])
                    finally:
                        self.step_lock.release()
                pending = (matter_id not in self.discovery_done or bool(binding['dirty'])
                           or self.repository.pending_work(matter_id, binding))
            if not pending:
                break
        if self.last_budget is None:
            return {'state': 'pending', 'steps': 0, 'counts': {}, 'mark': self.revision(matter_id)}
        self.last_budget.in_control = True
        with self.repository.counted(self.last_budget):
            summary = self.repository.attempt_counts(matter_id)
            statuses = self.repository.status_counts(matter_id)
        state = 'pending' if pending else 'error' if statuses.get('failed', 0) else 'complete'
        return {"state": state, "steps": steps, "counts": summary, "mark": self.revision(matter_id)}

    def read_source(self, matter_id, source, target, *, verify_digest=False):
        binding = self.binding(matter_id)
        prior = self.repository.current_attempt(matter_id, source.document_id)
        if prior is not None and self.dependency_state(prior).get('paused'):
            return
        try:
            path = binding["store"].source_path(source.document_id, verify_digest=verify_digest)
            if source.source_format in {"csv", "tsv"}:
                self.import_legacy_stream(matter_id, source, path, target=target)
            else:
                self.import_source(matter_id, source, path, binding["definition"], target=target, max_records=4)
        except (OSError, UploadProblem):
            # Preserve an owned source read failure without promoting a partial
            # import or leaking its filesystem exception into a page response.
            with binding["store"].mutation_guard():
                revision = binding["store"].revision
                if not self.valid(matter_id, source, target, revision, check_path=False):
                    return
                attempt_id = self.begin_attempt(matter_id, source, parser_revision=PARSER_REVISION,
                    definition_digest=binding["digest"], target=target, input_revision=self.input_revision(matter_id) + ":pending")
                with self.repository.step(matter_id, attempt_id, target=target,
                        validate=lambda: self.valid(matter_id, source, target, revision, check_path=False)):
                    self.repository.update_attempt(matter_id, attempt_id, status="failed", phase="error",
                        failure="The admitted load file could not be read completely; saved records remain incomplete.")
            self.changed(matter_id)
        except ProductionStale:
            prior = self.repository.current_attempt(matter_id, source.document_id)
            if prior is not None:
                self.changed(matter_id)
            self.discovery[matter_id] = ""
            self.discovery_done.discard(matter_id)

    def reference(self, attempt, category):
        return {"attempt_id": attempt["attempt_id"], "generation": attempt["generation"], "category": category,
            "source_id": attempt["document_id"], "source_version_id": attempt["version_id"],
            "source_name": attempt["name"], "source_format": attempt["source_format"],
            "status": attempt["status"], "counts": attempt["counts"], "failure": attempt["file_failure"]}

    def document_projection(self, matter_id, document_id, version_id):
        binding = self.binding(matter_id)
        current = self.repository.catalog(matter_id, document_id)
        if current is None or current["version_id"] != version_id:
            raise KeyError(document_id)
        result = {"state": "complete", "values": {}, "raw_fields": (), "diagnostics": (),
                  "family": {}, "references": (), "references_next": None}
        own = self.repository.current_attempt(matter_id, document_id)
        if own is not None:
            category = load_files.record_category(own["source_format"])
            result["references"] = (self.reference(own, category),)
            result["state"] = "pending" if own["status"] == "building" else "error" if own["status"] != "complete" else "complete"
            if own["file_failure"]:
                result["diagnostics"] = (own["file_failure"],)
            if not self.attempt_current(matter_id, own):
                result["state"] = "pending"
            state = self.dependency_state(own)
            result['restarts'] = state.get('restarts', 0)
            if state.get('paused'):
                result.update(state='paused', reload=state['reload'], attempt_id=own['attempt_id'])
            return result
        if matter_id not in self.discovery_done:
            result["state"] = "pending"
        if load_files.source_format(current["display_name"]):
            result["state"] = "pending"
            return result
        status = self.repository.document_status(matter_id, document_id)
        rows = self.repository.document_records(matter_id, document_id, limit=1,
            preferred_formats=load_files.document_format_order())
        if not rows:
            result["state"] = "pending" if status == "pending" or matter_id not in self.discovery_done else "error" if status == "conflicted" else "complete"
            problem = self.repository.document_problem(matter_id, document_id)
            result["diagnostics"] = (problem,) if problem else ()
            return result
        row = rows[0]
        attempt = self.repository.attempt(matter_id, row["attempt_id"])
        result["references"] = (self.reference(attempt, "document"),)
        result["references_next"] = [row["attempt_id"], row["ordinal"]]
        if (matter_id not in self.discovery_done or not self.attempt_current(matter_id, attempt)
                or row["generation"] != attempt["generation"]):
            result["state"] = "pending"
            return result
        if status != "complete" or row["association_status"] != "matched":
            result["state"] = "pending" if status == "pending" else "error"
            result["diagnostics"] = (self.repository.document_problem(matter_id, document_id)
                or row["association_status"],)
            return result
        projection = project_record(row["headers"], row["cells"], binding["definition"])
        result.update(values=projection.values, raw_fields=projection.raw_fields,
                      diagnostics=tuple(row["diagnostics"]) + tuple(row.get("association_diagnostics", ())),
                      family=row["family"] if row["family"].get("root") else {})
        parent_id = row["family"].get("root")
        if parent_id:
            parent = self.repository.catalog(matter_id, parent_id)
            if parent:
                result["family"] = {**row["family"], "context": {"token": parent["action_token"],
                    "name": parent["display_name"], "parent": parent_id == document_id}}
        page = self.repository.associated_page(matter_id, document_id)
        if page is not None:
            result["references"] += (self.reference(self.repository.attempt(matter_id, page["attempt_id"]), "page"),)
        return result

    def page_image(self, matter_id, attempt, record):
        """Resolve only one admitted image; embedded names remain inert metadata."""
        page = load_files.record_semantics(record)
        if not page["valid"] or not path_key(page["path"]) or not attempt["root_key"]:
            return None
        candidates = self.repository.candidate_lookup(matter_id, attempt["root_key"],
            paths=(page["path"],), excluded_document_id=attempt["document_id"], limit=2)
        if len(candidates) != 1 or candidates[0]["media_type"] not in {"image/jpeg", "image/png"}:
            return None
        candidate = candidates[0]
        store = self.binding(matter_id)["store"]
        try:
            document = store.get(candidate["document_id"])
            store.source_path(document.document_id)
        except (KeyError, UploadProblem):
            return None
        if document.version_id != candidate["version_id"]:
            return None
        return {"document_id": document.document_id, "version_id": document.version_id,
                "media_type": candidate["media_type"]}

    def record_window(self, matter_id, attempt_id, *, category, ordinal=0,
                      direction="at", document_id="", limit=100):
        if category not in {"document", "page"} or direction not in {"at", "next", "previous"}:
            raise ValueError("The production record selector is invalid.")
        if (type(ordinal) is not int or not 0 <= ordinal <= MAX_ORDINAL
                or type(limit) is not int or not 1 <= limit <= 100 or (not ordinal and direction != "at")):
            raise ValueError("The production record position is invalid.")
        attempt = self.repository.attempt(matter_id, attempt_id)
        if attempt is None:
            raise KeyError(attempt_id)
        expected_category = load_files.record_category(attempt["source_format"])
        if category != expected_category:
            raise ValueError("The record kind does not match its production source.")
        own_source = document_id == attempt["document_id"]
        requested = None
        current_source = self.repository.catalog(matter_id, attempt["document_id"])
        if not own_source and not document_id:
            raise KeyError(attempt_id)
        if not own_source and ordinal:
            requested = self.repository.record(matter_id, attempt_id, ordinal, category=category)
            if requested is None or requested["document_id"] != document_id:
                raise KeyError(ordinal)
        if (current_source is None or attempt["status"] == "stale" or current_source["version_id"] != attempt["version_id"]
                or not self.attempt_current(matter_id, attempt)):
            raise ProductionStale("This production source changed. Reload to inspect its current records.")
        if not own_source and attempt["status"] != "complete":
            raise ProductionStale("This production association is not current. Reload to continue.")
        scope = "" if own_source else document_id
        selected = None
        if ordinal and direction == "at":
            selected = self.repository.record(matter_id, attempt_id, ordinal, category=category)
        else:
            rows = self.repository.records(matter_id, attempt_id, category=category, after=ordinal,
                reverse=direction == "previous", document_id=scope,
                group_ordinal=requested["group_ordinal"] if requested and category == "page" else None, limit=1)
            selected = rows[0] if rows else None
        if selected is None or (scope and selected["document_id"] != scope):
            raise KeyError(ordinal)
        if scope and selected["generation"] != attempt["generation"]:
            raise ProductionStale("This production record belongs to an earlier association generation.")
        group = selected["group_ordinal"] if scope and category == "page" else None
        previous = self.repository.records(matter_id, attempt_id, category=category, after=selected["ordinal"],
                                           reverse=True, document_id=scope, group_ordinal=group, limit=1)
        following = self.repository.records(matter_id, attempt_id, category=category, after=selected["ordinal"],
                                            document_id=scope, group_ordinal=group, limit=1)
        selected = {**selected, "image": self.page_image(matter_id, attempt, selected) if category == "page" else None}
        return {"records": (selected,), "current": selected,
            "previous": record_cursor(category, attempt_id, previous[0]["ordinal"]) if previous else "",
            "next": record_cursor(category, attempt_id, following[0]["ordinal"]) if following else "",
            "attempt": {**attempt, "source_name": attempt["name"]}, "category": category,
            "counts": attempt["counts"], "total": attempt["counts"]["accepted"] + attempt["counts"]["rejected"]}
