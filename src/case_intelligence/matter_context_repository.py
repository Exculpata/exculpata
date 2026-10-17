"""Reference-only selections on the existing authorized connection.

The caller owns the source guard and authorized transaction. No implicit commit.
"""

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import json

from .workspace_store import WorkspaceProblem

MAX_ITEMS = 50
MAX_REFERENCES = 1000
MAX_BYTES = 512 * 1024


def serialized(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))


class ContextLimit(WorkspaceProblem):
    pass


class MatterContextRepository:
    def __init__(self, assertions):
        self.connection = assertions.connection
        self.now = assertions.now

    def selection(self, matter_id, owner_id):
        header = self.connection.execute('SELECT * FROM workbench_context_selection WHERE matter_id=? AND owner_id=?',
                                         (matter_id, owner_id)).fetchone()
        entries = [dict(row) for row in self.connection.execute('SELECT * FROM workbench_context_entry '
            'WHERE matter_id=? AND owner_id=? ORDER BY ordinal LIMIT 51', (matter_id, owner_id))]
        if len(entries) > MAX_ITEMS:
            raise ContextLimit('Saved selection exceeds the reference limit.')
        for row in entries:
            row['approval'] = json.loads(row.pop('approval_json'))
        return dict(header=dict(header) if header else None, revision=header['revision'] if header else 0, entries=entries)

    def preflight(self, table, matter_id, column, identifier, budget, *, limit=200):
        # All identifiers come from fixed service calls, never browser input.
        columns = [row['name'] for row in self.connection.execute(f'PRAGMA table_info({table})')]
        size = '+'.join(f'coalesce(length(CAST("{name}" AS BLOB)),0)' for name in columns)
        rows = self.connection.execute(f'SELECT {size} FROM {table} WHERE matter_id=? AND {column}=? LIMIT ?',
                                       (matter_id, identifier, limit + 1)).fetchall()
        budget['references'] += len(rows)
        budget['bytes'] += sum(row[0] for row in rows)
        if len(rows) > limit or budget['references'] > MAX_REFERENCES or budget['bytes'] > MAX_BYTES:
            raise ContextLimit('Record or selection exceeds inspection bounds (1,000 dependency rows / 512 KiB). Open the original record; remove entries or reduce support before selecting it.')

    def save(self, matter_id, owner_id, selection, entries):
        now = self.now()
        revision = selection['revision'] + 1
        self.connection.execute('INSERT INTO workbench_context_selection VALUES (?,?,?,?,?,?) '
            'ON CONFLICT(matter_id,owner_id) DO UPDATE SET revision=excluded.revision,updated_at=excluded.updated_at,updated_by=excluded.updated_by',
            (matter_id, owner_id, revision, now, now, owner_id))
        self.connection.execute('DELETE FROM workbench_context_entry WHERE matter_id=? AND owner_id=?', (matter_id, owner_id))
        for ordinal, entry in enumerate(entries, 1):
            self.connection.execute('INSERT INTO workbench_context_entry VALUES (?,?,?,?,?,?,?,?)',
                (matter_id, owner_id, entry['kind'], entry['object_id'], ordinal,
                 serialized(entry['approval']), entry['selected_by'], entry['selected_at']))
        return revision

    def export(self, matter_id):
        headers = [dict(row) for row in self.connection.execute('SELECT * FROM workbench_context_selection '
            'WHERE matter_id=? ORDER BY owner_id LIMIT 1001', (matter_id,))]
        if len(headers) > 1000:
            raise ContextLimit('Too many selections for a complete context manifest.')
        selections = []
        size = 0
        for header in headers:
            selection = self.selection(matter_id, header['owner_id'])
            size += len(serialized(selection).encode())
            if size > 8 * 1024 * 1024:
                raise ContextLimit('Context manifest exceeds the complete export limit.')
            selections.append(selection)
        return dict(format=f"{LEGACY_STEM}-context-selections-v1", consumption='explicit focused-answer opt-in only; actual consumption is recorded per answer', selections=selections)
