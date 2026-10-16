"""Frozen optional orientation and exact direct support for focused answers only.

All repository operations use the caller's source guard and control transaction.
No selection is consulted again when executing an accepted job.
"""

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import json

from .matter_context_repository import serialized
from .workspace_store import WorkspaceProblem

FORMAT = f"{LEGACY_STEM}-answer-context-v1"
MAX_SNAPSHOT_BYTES = 512 * 1024
MAX_ATTEMPTS = 16


def freeze(service, matter_id, actor_id, revision, scope_ids, source_set_id):
    selection = service.repository.selection(matter_id, actor_id)
    if type(revision) is not int or revision != selection['revision']:
        raise WorkspaceProblem('Saved context changed. Review Selected context and submit again.')
    if not selection['entries']:
        raise WorkspaceProblem('Saved context is empty. Select records or turn saved context off.')
    rows = []
    budget = service.budget()
    for entry in selection['entries']:
        row = service._row(matter_id, actor_id, entry, budget)
        if row['state'] != 'Unchanged':
            raise WorkspaceProblem('Saved context changed or is missing. Review, reconcile or remove it in Selected context.')
        current = row['current']
        if any(not ref['available'] for ref in current['references']):
            raise WorkspaceProblem('A selected source is stale or unavailable. Repair or remove the affected context before submitting.')
        if scope_ids is not None and any(ref['document_id'] not in scope_ids for ref in current['references']):
            raise WorkspaceProblem('Selected context has out-of-scope sources. Remove that context or deliberately change the question source scope.')
        rows.append(dict(entry=entry, **current))
    result = dict(format=FORMAT, actor_id=actor_id, matter_id=matter_id,
                  selection_revision=revision, submitted_at=service.repository.now(),
                  source_set_id=source_set_id, source_ids=sorted(scope_ids) if scope_ids is not None else None,
                  records=rows)
    if len(serialized(result).encode()) > MAX_SNAPSHOT_BYTES:
        raise WorkspaceProblem('Complete selected context is too large. Reduce the selection and submit again.')
    # This is an upper bound, independent of the later whole-request token budget.
    orientation(result)
    return result


def orientation(snapshot, evidence=None):
    from .generation import MAX_WORKING_CONTEXT_CHARS
    # Reference text appears ONLY in the separately verified evidence packet.
    def account(ref):
        result = {key: ref[key] for key in ('stance', 'attributed_to') if key in ref}
        if evidence is not None:
            matches = [f'S{i}' for i, citation in enumerate(evidence, 1)
                       if all(getattr(citation, key) == ref[key] for key in
                              ('document_id', 'source_version_id', 'chunk_id', 'location',
                               'excerpt_digest', 'support_token'))]
            if len(matches) != 1:
                raise WorkspaceProblem('A selected account has no unique admitted original. Repair the selected context before submitting.')
            result['evidence_id'] = matches[0]
        return result
    records = [dict(kind=row['entry']['kind'], object_id=row['entry']['object_id'],
                    record=row['record'], roles=row['roles'],
                    accounts=[account(ref) for ref in row['references']]) for row in snapshot['records']]
    value = serialized(dict(format=FORMAT, records=records))
    if len(value) > MAX_WORKING_CONTEXT_CHARS:
        raise WorkspaceProblem('Whole selected records exceed the context ceiling. Remove records; no partial record was supplied.')
    return value


def references(snapshot):
    return [ref for row in snapshot['records'] for ref in row['references']]


def admit(snapshot, retrieved, resolve, excluded_kinds):
    """Required originals together, then complete retrieved passages within ceilings."""
    from .generation import MAX_EVIDENCE_ITEMS, MAX_EVIDENCE_CHARS, MAX_EVIDENCE_ITEM_CHARS
    def identity(citation):
        return tuple(getattr(citation, key) for key in (
            'matter_id', 'document_id', 'source_version_id', 'chunk_id', 'location',
            'excerpt_digest', 'evidence_kind', 'support_token'))
    # Reject an impossible whole group from frozen metadata before resolving
    # potentially large originals. Duplicate exact references can share a slot.
    unique_refs = {}
    for ref in references(snapshot):
        key = tuple(ref[key] for key in ('document_id', 'source_version_id', 'chunk_id',
                                         'location', 'unit_number', 'excerpt_digest', 'support_token'))
        unique_refs.setdefault(key, ref)
        if len(unique_refs) > MAX_EVIDENCE_ITEMS:
            raise WorkspaceProblem('A complete selected support group exceeds the evidence budget. Reduce selected context; no one-sided group was supplied.')
    required = []
    seen = set()
    for ref in unique_refs.values():
        citation = resolve(ref)
        if citation is None or citation.evidence_kind in excluded_kinds:
            raise WorkspaceProblem('A complete selected support group is unavailable for this question. Repair/remove the affected context or change the question scope.')
        key = identity(citation)
        if key not in seen:
            required.append(citation)
            seen.add(key)
    if (len(required) > MAX_EVIDENCE_ITEMS or
        any(len(c.excerpt) > MAX_EVIDENCE_ITEM_CHARS for c in required) or
        sum(len(c.excerpt) for c in required) > MAX_EVIDENCE_CHARS):
        raise WorkspaceProblem('A complete selected support group exceeds the evidence budget. Reduce selected context; no one-sided group was supplied.')
    admitted = list(required)
    routes = {identity(c): ['selected_support'] for c in required}
    omitted = []
    for citation in retrieved:
        key = identity(citation)
        if key in seen:
            if 'retrieval' not in routes[key]:
                routes[key].append('retrieval')
            continue
        reason = ('evidence_kind' if citation.evidence_kind in excluded_kinds else
                  'whole_passage_character_limit' if len(citation.excerpt) > MAX_EVIDENCE_ITEM_CHARS else
                  'evidence_budget' if len(admitted) >= MAX_EVIDENCE_ITEMS or
                  sum(len(c.excerpt) for c in admitted) + len(citation.excerpt) > MAX_EVIDENCE_CHARS else '')
        if reason:
            omitted.append(dict(document_id=citation.document_id, source_version_id=citation.source_version_id,
                                location=citation.location, reason=reason))
        else:
            admitted.append(citation)
            routes[key] = ['retrieval']
            seen.add(key)
    return admitted, [dict(evidence_id=f'S{i}', routes=routes[identity(c)])
                      for i, c in enumerate(admitted, 1)], omitted


class AnswerContextRepository:
    def __init__(self, workspace):
        self.workspace = workspace
        self.connection = workspace.connection

    def snapshot(self, matter_id, job_id):
        row = self.connection.execute('SELECT snapshot_json FROM workbench_answer_context WHERE matter_id=? AND job_id=?',
                                      (matter_id, job_id)).fetchone()
        return json.loads(row[0]) if row else None

    def receipt(self, matter_id, job_id):
        snapshot = self.snapshot(matter_id, job_id)
        if snapshot is None:
            return None
        attempts = [dict(row) for row in self.connection.execute(
            'SELECT ordinal,worker_attempt,state,manifest_json,response_json,prepared_at,attempted_at,completed_at '
            'FROM workbench_answer_context_attempt WHERE job_id=? ORDER BY ordinal', (job_id,))]
        for attempt in attempts:
            attempt['manifest'] = json.loads(attempt.pop('manifest_json'))
            attempt['response_metadata'] = json.loads(attempt.pop('response_json') or 'null')
        return dict(snapshot=snapshot, attempts=attempts,
                    notice='Context supplied records application dispatch, not proof of model attention or immutable per-response model attestation.')

    def fence(self, job):
        row = self.connection.execute('SELECT * FROM workbench_answer_job WHERE job_id=? AND matter_id=?',
                                      (job.job_id, job.matter_id)).fetchone()
        if (row is None or row['state'] != 'running' or row['cancellation_requested'] or
            row['attempts'] != job.attempts or row['worker_id'] != job.worker_id):
            raise WorkspaceProblem('Answer cancelled or worker superseded. No further dispatch or save is allowed.')
        self.workspace.membership(job.matter_id, job.actor_id)
        active = self.connection.execute('SELECT 1 FROM workbench_conversation c JOIN workbench_conversation_organization o '
            "ON o.conversation_id=c.conversation_id WHERE c.matter_id=? AND c.conversation_id=? AND o.state='active'",
            (job.matter_id, job.conversation_id)).fetchone()
        if active is None:
            raise WorkspaceProblem('The answer conversation is no longer active.')

    def prepare(self, job, manifest):
        encoded = serialized(manifest)
        if len(encoded.encode()) > 128 * 1024:
            raise WorkspaceProblem('The complete dispatch record exceeds its bound. Reduce the question context.')
        with self.workspace._lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            self.fence(job)
            ordinal = self.connection.execute('SELECT count(*)+1 FROM workbench_answer_context_attempt WHERE job_id=?',
                                              (job.job_id,)).fetchone()[0]
            if ordinal > MAX_ATTEMPTS:
                raise WorkspaceProblem('This request reached its recorded attempt limit. Submit a new reviewed request.')
            self.connection.execute('INSERT INTO workbench_answer_context_attempt '
                '(job_id,ordinal,worker_attempt,state,manifest_json,prepared_at) VALUES (?,?,?,\'prepared\',?,?)',
                (job.job_id, ordinal, job.attempts, encoded, self.workspace._now()))
        return ordinal

    def transition(self, job, ordinal, state, response=None):
        previous = 'prepared' if state == 'dispatch_attempted' else 'dispatch_attempted'
        with self.workspace._lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            self.fence(job)
            column = 'attempted_at' if state == 'dispatch_attempted' else 'completed_at'
            if state not in ('dispatch_attempted', 'completed', 'transport_failed'):
                raise ValueError('Invalid dispatch state')
            changed = self.connection.execute(f'UPDATE workbench_answer_context_attempt SET state=?,{column}=?,response_json=? '
                'WHERE job_id=? AND ordinal=? AND worker_attempt=? AND state=?',
                (state, self.workspace._now(), serialized(response) if response is not None else None, job.job_id, ordinal, job.attempts, previous)).rowcount
            if changed != 1:
                raise WorkspaceProblem('The dispatch attempt changed. No additional request was sent.')
