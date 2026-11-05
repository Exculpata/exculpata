"""Reviewer source designations; never retrieval policy or model input."""
import json
from importlib import resources

from .workspace_store import WorkspaceProblem


class SourceRoleConflict(WorkspaceProblem):
    pass


def migrate_source_roles(connection):
    # Serialize detection and all additive columns across concurrent openers.
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        columns = {row[1] for row in connection.execute('PRAGMA table_info(workbench_source_organization)')}
        if 'source_role' not in columns:
            sql = resources.files('case_intelligence').joinpath('migrations/sqlite/0039_source_roles.sql').read_text()
            for statement in sql.split(';'):
                if statement.strip():
                    connection.execute(statement)


class SourceRoleService:
    def __init__(self, workspace, *, administrator_override=False):
        self.workspace = workspace
        self.unit = workspace.entity_repository(export_read=administrator_override,
                                                administrator_override=administrator_override)
        self.connection = workspace.connection

    def _active(self, matter_id, actor_id):
        self.workspace._active_matter_locked(matter_id)
        principal = self.workspace.get_principal(actor_id)
        if not principal.active or not self.workspace.principal_enabled(principal.provider, principal.provider_subject):
            raise KeyError(matter_id)

    def _current(self, matter_id, token):
        row = self.connection.execute(
            'SELECT c.document_id,c.version_id,o.source_role,o.role_revision AS revision,'
            'o.role_updated_by AS updated_by,o.role_updated_at AS updated_at '
            'FROM workbench_source_organization o JOIN workbench_source_catalog c '
            'ON c.matter_id=o.matter_id AND c.document_id=o.document_id '
            'WHERE o.matter_id=? AND c.action_token=?', (matter_id, token)).fetchone()
        if row is None:
            raise KeyError(token)
        return dict(row)

    def read(self, matter_id, actor_id, token, *, check_authority=lambda: None):
        with self.unit.reading(matter_id, actor_id):
            check_authority()
            self._active(matter_id, actor_id)
            current = self._current(matter_id, token)
            self.unit.read_authorize(matter_id, actor_id)
            check_authority()
            return current

    def update(self, matter_id, actor_id, token, *, expected_revision, source_role,
               request_id, session_id=None, check_authority=lambda: None):
        with self.unit.transaction(matter_id, actor_id):
            self.workspace.membership(matter_id, actor_id)
            check_authority()
            self._active(matter_id, actor_id)
            if type(expected_revision) is not int or expected_revision < 0:
                raise WorkspaceProblem('A displayed source role revision is required.')
            if source_role not in ('unclassified', 'background'):
                raise WorkspaceProblem('Choose Unclassified or Background document.')
            current = self._current(matter_id, token)
            if current['revision'] != expected_revision:
                raise SourceRoleConflict('The source role changed in another tab. Review the current role before saving again.')
            if current['source_role'] != source_role:
                event = self.workspace._append_audit_event_locked(
                    actor_principal_id=actor_id, session_id=session_id, matter_id=matter_id,
                    request_id=request_id, action='source.role_update', outcome='success',
                    object_type='source', object_id=current['document_id'])
                self.connection.execute(
                    'UPDATE workbench_source_organization SET source_role=?,role_revision=role_revision+1,'
                    'role_updated_by=?,role_updated_at=? WHERE matter_id=? AND document_id=?',
                    (source_role, actor_id, event.occurred_at, matter_id, current['document_id']))
            self.workspace.membership(matter_id, actor_id)
            self.unit.authorize(matter_id, actor_id)
            self._active(matter_id, actor_id)
            check_authority()
            return self._current(matter_id, token)


def add_source_role_export(workspace, matter_id, actor_id, administrator_override, add_work_product):
    from .work_product_exports import ExportArtifact
    unit = workspace.entity_repository(export_read=True, administrator_override=administrator_override)
    with unit.reading(matter_id, actor_id):
        rows = [dict(row) for row in workspace.connection.execute(
            'SELECT o.document_id,c.version_id,o.source_role,o.role_revision AS revision,'
            'o.role_updated_by AS updated_by,o.role_updated_at AS updated_at '
            'FROM workbench_source_organization o LEFT JOIN workbench_source_catalog c '
            'ON c.matter_id=o.matter_id AND c.document_id=o.document_id '
            'WHERE o.matter_id=? ORDER BY o.document_id LIMIT 10001', (matter_id,))]
        events = [dict(row) for row in workspace.connection.execute(
            "SELECT * FROM workbench_audit_event WHERE matter_id=? AND action='source.role_update' "
            'ORDER BY occurred_at,event_id LIMIT 10001', (matter_id,))]
        if len(rows) > 10000 or len(events) > 10000:
            raise WorkspaceProblem('Source role metadata exceeds the complete export limit.')
        if any(row['version_id'] is None for row in rows):
            raise WorkspaceProblem('Source role metadata has no current source inventory. Retry after intake completes.')
        body = json.dumps(dict(format='exculpata-source-roles-v1', sources=rows, audit_events=events),
                          ensure_ascii=False, indent=2).encode('utf-8')
        unit.read_authorize(matter_id, actor_id)
        add_work_product(kind='source_roles', path='context/source-roles.json',
            artifact=ExportArtifact(body=body, media_type='application/json', filename='source-roles.json'))


def export_source_inventory(catalog):
    return tuple(dict(name=source.display_name, kind=source.kind, state=source.state_label,
                      count_label=source.count_label) for source in catalog)
