"""Shared matter-scoped review decisions, composed with entity authorization.

The control SQLite store is authoritative, including when retrieval uses PostgreSQL.
The PostgreSQL migration defines the equivalent schema for portable recovery tools.
"""
from dataclasses import replace
from importlib import resources

from .workspace_store import WorkspaceProblem

ACTIONS = {'dismiss': 'dismissed', 'reviewed': 'reviewed', 'reopen': 'open'}


class ReviewConflict(WorkspaceProblem):
    pass


def migrate_candidate_review(workspace):
    sql = resources.files('case_intelligence').joinpath('migrations/sqlite/0044_candidate_review.sql').read_text()
    with workspace._lock, workspace.connection:
        workspace.connection.executescript('BEGIN IMMEDIATE;\n' + sql + '\nCOMMIT;')


class CandidateReviewRepository:
    """Operations require the caller's source guard and authorized transaction."""
    def __init__(self, entity_repository):
        self.entities = entity_repository
        self.connection = entity_repository.connection

    def apply(self, matter_id, candidate, namespace='inconsistency'):
        row = self.connection.execute('SELECT * FROM workbench_candidate_review WHERE matter_id=? AND namespace=? AND signature=?',
                                      (matter_id, namespace, candidate.signature)).fetchone()
        if not row:
            return candidate
        return replace(candidate, status=row['status'], reviewed_by=row['reviewed_by'],
                       reviewed_at=row['reviewed_at'], revision=row['revision'])

    def save(self, matter_id, actor_id, candidate, action, expected_revision, namespace='inconsistency'):
        if action not in ACTIONS:
            raise WorkspaceProblem('Choose dismiss, reviewed or reopen.')
        current = self.apply(matter_id, candidate, namespace)
        if current.revision != expected_revision:
            raise ReviewConflict('This review decision changed. Reload and compare before saving again.')
        self.connection.execute('INSERT INTO workbench_candidate_review '
            '(matter_id,namespace,signature,status,reviewed_by,reviewed_at,revision) VALUES (?,?,?,?,?,?,?) '
            'ON CONFLICT(matter_id,namespace,signature) DO UPDATE SET status=excluded.status,'
            'reviewed_by=excluded.reviewed_by,reviewed_at=excluded.reviewed_at,revision=excluded.revision',
            (matter_id, namespace, candidate.signature, ACTIONS[action], actor_id, self.entities.now(), current.revision + 1))
        return self.apply(matter_id, candidate, namespace)
