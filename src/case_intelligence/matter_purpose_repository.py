"""Reviewer orientation on the caller's existing authorized connection."""


class MatterPurposeRepository:
    def __init__(self, unit_of_work, audit):
        self.unit = unit_of_work
        self.connection = unit_of_work.connection
        self.audit = audit

    def require_active(self, matter_id):
        row = self.connection.execute(
            "SELECT 1 FROM workbench_matter_lifecycle WHERE matter_id=? AND state='active'",
            (matter_id,),
        ).fetchone()
        if row is None:
            raise KeyError(matter_id)

    def get(self, matter_id):
        row = self.connection.execute(
            'SELECT objective,background,revision,updated_by,updated_at '
            'FROM workbench_matter_purpose WHERE matter_id=?', (matter_id,),
        ).fetchone()
        return dict(row) if row else dict(objective='', background='', revision=0,
                                         updated_by=None, updated_at=None)

    def save(self, matter_id, actor_id, objective, background, revision, request_id, session_id):
        # The existing audit writer shares this transaction; it receives no text,
        # hashes, lengths or prior values from either reviewer-authored field.
        event = self.audit(actor_principal_id=actor_id, session_id=session_id,
            matter_id=matter_id, request_id=request_id, action='matter.purpose_update',
            outcome='success', object_type='matter', object_id=matter_id)
        self.connection.execute(
            'INSERT INTO workbench_matter_purpose VALUES (?,?,?,?,?,?) '
            'ON CONFLICT(matter_id) DO UPDATE SET objective=excluded.objective,'
            'background=excluded.background,revision=excluded.revision,'
            'updated_by=excluded.updated_by,updated_at=excluded.updated_at',
            (matter_id, objective, background, revision, actor_id, event.occurred_at),
        )
        return self.get(matter_id)
