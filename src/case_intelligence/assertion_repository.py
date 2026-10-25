"""Evidence assertion persistence composed with the authorized entity unit of work."""

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
from contextlib import contextmanager
import json
import uuid

from .entity_service import REFERENCE_FIELDS
from .workspace_store import WorkspaceProblem

RECORD_FIELDS = ('record_type', 'title', 'statement', 'raw_date', 'date_uncertainty', 'sort_date', 'status')
TABLES = ('workbench_assertion', 'workbench_assertion_role',
          'workbench_assertion_account', 'workbench_assertion_history')
MAX_RECORDS = 5000
MAX_ROLES = 100
MAX_ACCOUNTS = 200
MAX_HISTORY = 1000
MAX_STORAGE_BYTES = 64 * 1024 * 1024


class AssertionEditConflict(WorkspaceProblem):
    pass


class AssertionRepository:
    def __init__(self, entity_repository):
        self.entities = entity_repository
        self.connection = entity_repository.connection
        self.now = entity_repository.now

    @contextmanager
    def transaction(self, matter_id, actor_id):
        with self.entities.transaction(matter_id, actor_id):
            yield self

    @contextmanager
    def reading(self, matter_id, actor_id):
        with self.entities.reading(matter_id, actor_id):
            yield self

    def get(self, matter_id, assertion_id):
        row = self.connection.execute('SELECT * FROM workbench_assertion WHERE matter_id=? AND assertion_id=?',
                                      (matter_id, assertion_id)).fetchone()
        if row is None:
            raise KeyError(assertion_id)
        return dict(row)

    def check_revision(self, matter_id, assertion_id, expected_revision):
        current = self.get(matter_id, assertion_id)
        if type(expected_revision) is not int or expected_revision < 1 or current['revision'] != expected_revision:
            raise AssertionEditConflict('This event or assertion changed since you opened it. Compare the saved version with your unsaved work before saving again.')
        return current

    @staticmethod
    def _filter(matter_id, entity_id, date_group):
        clauses, parameters = ['a.matter_id=?'], [matter_id]
        if entity_id:
            clauses.append('EXISTS (SELECT 1 FROM workbench_assertion_role r WHERE r.matter_id=a.matter_id '
                           'AND r.assertion_id=a.assertion_id AND r.entity_id=?)')
            parameters.append(entity_id)
        if date_group == 'dated':
            clauses.append("a.sort_date!=''")
        elif date_group == 'undated':
            clauses.append("a.sort_date=''")
        return ' AND '.join(clauses), parameters

    def list(self, matter_id, entity_id='', page=1, date_group='all'):
        where, parameters = self._filter(matter_id, entity_id, date_group)
        total = self.connection.execute('SELECT COUNT(*) FROM workbench_assertion a WHERE ' + where, parameters).fetchone()[0]
        rows = self.connection.execute('SELECT a.* FROM workbench_assertion a WHERE ' + where +
            " ORDER BY (a.sort_date=''),a.sort_date,a.created_at,a.assertion_id LIMIT 50 OFFSET ?",
            (*parameters, (page - 1) * 50))
        return [dict(row) for row in rows], total

    def roles(self, matter_id, assertion_id, *, current=True):
        if not current:
            return [dict(row) for row in self.connection.execute(
                'SELECT * FROM workbench_assertion_role WHERE matter_id=? AND assertion_id=? ORDER BY created_at,role_id',
                (matter_id, assertion_id))]
        rows = self.connection.execute('SELECT r.*,e.revision AS current_revision,e.display_name AS current_display_name,'
            'e.entity_type AS current_entity_type FROM workbench_assertion_role r LEFT JOIN workbench_entity e '
            'ON e.matter_id=r.matter_id AND e.entity_id=r.entity_id WHERE r.matter_id=? AND r.assertion_id=? '
            'ORDER BY r.created_at,r.role_id', (matter_id, assertion_id))
        result = []
        for row in rows:
            value = dict(row)
            value['identity_state'] = ('missing' if value['current_revision'] is None else
                'current' if value['current_revision'] == value['entity_revision'] else 'changed')
            result.append(value)
        return result

    def accounts(self, matter_id, assertion_id):
        return [dict(row) for row in self.connection.execute('SELECT * FROM workbench_assertion_account '
            'WHERE matter_id=? AND assertion_id=? ORDER BY stance DESC,created_at,account_id', (matter_id, assertion_id))]

    def history(self, matter_id, assertion_id):
        return [dict(row) for row in self.connection.execute('SELECT * FROM workbench_assertion_history '
            'WHERE matter_id=? AND assertion_id=? ORDER BY revision DESC', (matter_id, assertion_id))]

    def create(self, matter_id, actor_id, fields):
        assertion_id, now = 'assertion-' + uuid.uuid4().hex, self.now()
        self.connection.execute('INSERT INTO workbench_assertion(assertion_id,matter_id,' + ','.join(RECORD_FIELDS) +
            ',created_by,created_at,updated_by,updated_at) VALUES (' + ','.join('?' for _ in range(13)) + ')',
            (assertion_id, matter_id, *(fields[key] for key in RECORD_FIELDS), actor_id, now, actor_id, now))
        return self.get(matter_id, assertion_id)

    def save(self, matter_id, actor_id, assertion_id, fields):
        self.connection.execute('UPDATE workbench_assertion SET ' + ','.join(key + '=?' for key in RECORD_FIELDS) +
            ',revision=revision+1,updated_by=?,updated_at=? WHERE matter_id=? AND assertion_id=?',
            (*(fields[key] for key in RECORD_FIELDS), actor_id, self.now(), matter_id, assertion_id))

    def add_role(self, matter_id, actor_id, assertion_id, entity, role):
        if self.connection.execute('SELECT 1 FROM workbench_assertion_role '
                'WHERE matter_id=? AND assertion_id=? AND entity_id=? AND role=?',
                (matter_id, assertion_id, entity['entity_id'], role)).fetchone():
            raise WorkspaceProblem('This identity already has that role. Remove the existing role before attaching a revised identity snapshot.')
        role_id = 'assertion-role-' + uuid.uuid4().hex
        self.connection.execute('INSERT INTO workbench_assertion_role VALUES (?,?,?,?,?,?,?,?,?,?)',
            (role_id, matter_id, assertion_id, role, entity['entity_id'], entity['revision'],
             entity['display_name'], entity['entity_type'], actor_id, self.now()))
        return dict(self.connection.execute('SELECT * FROM workbench_assertion_role WHERE role_id=?', (role_id,)).fetchone())

    def role(self, matter_id, assertion_id, role_id):
        row = self.connection.execute('SELECT * FROM workbench_assertion_role WHERE matter_id=? AND assertion_id=? AND role_id=?',
            (matter_id, assertion_id, role_id)).fetchone()
        if row is None:
            raise KeyError(role_id)
        return dict(row)

    def revise_role(self, matter_id, assertion_id, role_id, entity, role):
        if self.connection.execute('SELECT 1 FROM workbench_assertion_role WHERE matter_id=? AND assertion_id=? '
                'AND entity_id=? AND role=? AND role_id!=?',
                (matter_id, assertion_id, entity['entity_id'], role, role_id)).fetchone():
            raise WorkspaceProblem('This identity already has that role on another attachment. Correct or remove the duplicate role instead.')
        self.connection.execute('UPDATE workbench_assertion_role SET entity_id=?,entity_revision=?,display_name=?,entity_type=?,role=? '
            'WHERE matter_id=? AND assertion_id=? AND role_id=?',
            (entity['entity_id'], entity['revision'], entity['display_name'], entity['entity_type'], role, matter_id, assertion_id, role_id))
        return self.role(matter_id, assertion_id, role_id)

    def add_account(self, matter_id, actor_id, assertion_id, reference, stance, attributed_to):
        account_id = 'assertion-account-' + uuid.uuid4().hex
        self.connection.execute('INSERT INTO workbench_assertion_account(account_id,matter_id,assertion_id,stance,'
            'attributed_to,' + ','.join(REFERENCE_FIELDS) + ',created_by,created_at) VALUES (' +
            ','.join('?' for _ in range(16)) + ')',
            (account_id, matter_id, assertion_id, stance, attributed_to,
             *(reference[key] for key in REFERENCE_FIELDS), actor_id, self.now()))
        return self.account(matter_id, assertion_id, account_id)

    def account(self, matter_id, assertion_id, account_id):
        row = self.connection.execute('SELECT * FROM workbench_assertion_account '
            'WHERE matter_id=? AND assertion_id=? AND account_id=?', (matter_id, assertion_id, account_id)).fetchone()
        if row is None:
            raise KeyError(account_id)
        return dict(row)

    def duplicate_account(self, matter_id, assertion_id, support_token, attributed_to, *, excluding=''):
        row = self.connection.execute('SELECT * FROM workbench_assertion_account WHERE matter_id=? '
            'AND assertion_id=? AND support_token=? AND attributed_to=? AND account_id!=?',
            (matter_id, assertion_id, support_token, attributed_to, excluding)).fetchone()
        return dict(row) if row else None

    def revise_account(self, matter_id, assertion_id, account_id, stance, attributed_to):
        self.connection.execute('UPDATE workbench_assertion_account SET stance=?,attributed_to=? '
            'WHERE matter_id=? AND assertion_id=? AND account_id=?', (stance, attributed_to, matter_id, assertion_id, account_id))

    def remove_account(self, matter_id, assertion_id, account_id):
        removed = self.account(matter_id, assertion_id, account_id)
        self.connection.execute('DELETE FROM workbench_assertion_account WHERE matter_id=? AND assertion_id=? AND account_id=?',
            (matter_id, assertion_id, account_id))
        return removed

    def remove_role(self, matter_id, assertion_id, role_id):
        row = self.connection.execute('SELECT * FROM workbench_assertion_role WHERE matter_id=? AND assertion_id=? AND role_id=?',
            (matter_id, assertion_id, role_id)).fetchone()
        if row is None:
            raise KeyError(role_id)
        self.connection.execute('DELETE FROM workbench_assertion_role WHERE matter_id=? AND assertion_id=? AND role_id=?',
            (matter_id, assertion_id, role_id))
        return dict(row)

    def record_history(self, matter_id, actor_id, assertion_id, action, **deltas):
        current = self.get(matter_id, assertion_id)
        # Only additions/removals carry original excerpts and frozen roles;
        # metadata corrections carry the small before/after fields, never copies
        # of all previously attached sources on every revision.
        snapshot = dict(current, history_format=1, **deltas)
        self.connection.execute('INSERT INTO workbench_assertion_history VALUES (?,?,?,?,?,?,?)',
            (assertion_id, matter_id, current['revision'], action, json.dumps(snapshot), actor_id, self.now()))
        self.connection.execute('UPDATE workbench_matter SET updated_at=? WHERE matter_id=?', (self.now(), matter_id))

    def check_limits(self, matter_id, assertion_id):
        counts = ((TABLES[0], MAX_RECORDS, False), (TABLES[1], MAX_ROLES, True),
                  (TABLES[2], MAX_ACCOUNTS, True), (TABLES[3], MAX_HISTORY, True))
        for table, maximum, scoped in counts:
            count = self.connection.execute('SELECT COUNT(*) FROM ' + table + ' WHERE matter_id=?' +
                (' AND assertion_id=?' if scoped else ''),
                (matter_id, assertion_id) if scoped else (matter_id,)).fetchone()[0]
            if count > maximum:
                raise WorkspaceProblem('This event or assertion exceeds the retained record, role, account, or history limit. The change was not saved.')
        if self.storage_bytes(matter_id) > MAX_STORAGE_BYTES:
            raise WorkspaceProblem('The matter has reached its retained assertion storage limit. The change was not saved.')

    def storage_bytes(self, matter_id):
        total = 0
        for table in TABLES:
            columns = [row[1] for row in self.connection.execute('PRAGMA table_info(' + table + ')')]
            sizes = '+'.join('COALESCE(length(CAST("' + column + '" AS BLOB)),0)' for column in columns)
            total += self.connection.execute('SELECT COALESCE(SUM(256+' + sizes + '),0) FROM ' + table +
                ' WHERE matter_id=?', (matter_id,)).fetchone()[0]
        return total

    def delete(self, matter_id, assertion_id):
        self.connection.execute('DELETE FROM workbench_assertion WHERE matter_id=? AND assertion_id=?', (matter_id, assertion_id))
        self.connection.execute('UPDATE workbench_matter SET updated_at=? WHERE matter_id=?', (self.now(), matter_id))

    def export_records(self, matter_id, *, entity_id=''):
        """Caller holds the authorized UOW, including administrator recovery reads."""
        where, parameters = self._filter(matter_id, entity_id, 'all')
        for row in self.connection.execute('SELECT a.assertion_id FROM workbench_assertion a WHERE ' + where +
                " ORDER BY (a.sort_date=''),a.sort_date,a.created_at,a.assertion_id", parameters):
            assertion_id = row[0]
            yield dict(format=f"{LEGACY_STEM}-assertion-v1", record=self.get(matter_id, assertion_id),
                roles=self.roles(matter_id, assertion_id), accounts=self.accounts(matter_id, assertion_id),
                history=self.history(matter_id, assertion_id),
                source_support='Retained original-source support; availability is not revalidated in this bundle.')
