"""The same ordinary-table migration, service and seek queries on both engines."""
from dataclasses import replace
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4

import pytest

from case_intelligence.modules import ModuleDatabase
from case_intelligence.modules.discovery_review import fields, presentation
from case_intelligence.modules.discovery_review.codes import CodeRepository
from case_intelligence.modules.discovery_review.identifiers import IdentifierRepository
from case_intelligence.modules.discovery_review.stored_index import query
from case_intelligence.modules.discovery_review.stored_index.models import Budget
from case_intelligence.modules.discovery_review.stored_index.rebuild import Service, Job, work_bound
from case_intelligence.modules.discovery_review.stored_index.repository import digest
from tests.test_discovery_review_list import production as production
from tests.test_discovery_review_index import service_for, manifest

pytestmark = pytest.mark.postgres


class BorrowedPostgres:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, parameters=()):
        # fixture-write: Forward real SQL only on this test's isolated PostgreSQL schema connection.
        return self.connection.execute(sql.replace('?', '%s'), parameters)

    def transaction(self):
        return self.connection.transaction()


@pytest.fixture
def isolated_connection(postgres_base_dsn):
    import psycopg
    from psycopg import sql
    schema = 'synthetic_discovery_' + uuid4().hex
    with psycopg.connect(postgres_base_dsn, autocommit=True) as connection:
        # fixture-write: Create only this test's uniquely named synthetic schema.
        connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        try:
            # fixture-write: Scope the independent connection to the owned synthetic schema.
            connection.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
            yield connection
        finally:
            # fixture-write: Drop only the uniquely named schema owned by this test.
            connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_portable_migration_and_queries_match_sqlite(production, isolated_connection):
    from dataclasses import asdict
    client, bench, matter = production
    root = Path(__file__).resolve().parents[1]
    definition = fields.load_definition()
    migration = (root / 'migrations/sqlite/0049_discovery_review_index.sql').read_text()
    minimal = '''CREATE TABLE workbench_matter(matter_id TEXT PRIMARY KEY);
    CREATE TABLE workbench_discovery_identifier(matter_id TEXT, document_id TEXT, identifier TEXT, PRIMARY KEY(matter_id,document_id));
    CREATE TABLE workbench_discovery_code(matter_id TEXT,code_id TEXT,label TEXT,active INTEGER,position INTEGER);
    CREATE TABLE workbench_discovery_code_event(matter_id TEXT,event_id INTEGER,document_id TEXT,code_id TEXT,action TEXT,created_by TEXT,created_at TEXT);'''
    sqlite = sqlite3.connect(':memory:')
    results = []
    try:
        for connection in (sqlite, BorrowedPostgres(isolated_connection)):
            for statement in (minimal + migration).split(';'):
                if statement.strip():
                    # fixture-write: Apply reviewed DDL only to standalone synthetic SQLite or isolated PostgreSQL.
                    connection.execute(statement)
            # fixture-write: Seed a minimal parent only in the isolated portability database.
            connection.execute('INSERT INTO workbench_matter VALUES (?)', (matter.matter_id,))
            document_id = next(iter(bench.source_store(matter).documents))
            # fixture-write: Synthetic human-work rows belong only to the independent portability database.
            connection.execute('INSERT INTO workbench_discovery_identifier VALUES (?,?,?)', (matter.matter_id, document_id, 'TEAM007'))
            # fixture-write: Minimal Coding definition in the owned synthetic database.
            connection.execute('INSERT INTO workbench_discovery_code VALUES (?,?,?,?,?)', (matter.matter_id, 'synthetic', 'Synthetic code', 1, 1))
            # fixture-write: Minimal effective event in the owned synthetic database.
            connection.execute('INSERT INTO workbench_discovery_code_event VALUES (?,?,?,?,?,?,?)',
                               (matter.matter_id, 1, document_id, 'synthetic', 'apply', 'synthetic-actor', '2025-01-01T00:00:00Z'))
            if connection is sqlite:
                sqlite.commit()
            db = ModuleDatabase(connection, threading.RLock())
            original = service_for(bench)
            service = Service(replace(original.context, db=db), IdentifierRepository(db, original.context.membership), CodeRepository(db))
            job = Job(matter.matter_id, bench.source_store(matter), definition, digest(asdict(definition)))
            service.jobs[matter.matter_id] = job
            checkpoints = []
            for _ in range(work_bound(**{**manifest(4), 'I': 1, 'C': 1})[2]):
                service.advance(job, 1)
                assert not job.error, getattr(service, 'failure', '')
                assert job.last_budget.touches <= 256
                state = service.repository.state(matter.matter_id)
                checkpoints.append((state['phase'], state['indexed'], state['pending'], state['errors']))
                if state['phase'] == 'complete':
                    break
                job = Job(matter.matter_id, job.store, definition, job.definition_digest)
                service.jobs[matter.matter_id] = job
            assert state['phase'] == 'complete'
            before = service.repository.state(matter.matter_id)
            with pytest.raises(RuntimeError, match='synthetic rollback'):
                with service.repository.transaction(Budget(256, 224), 2 * 1024 * 1024) as tx:
                    tx.write('DELETE FROM workbench_discovery_index_state WHERE matter_id=?', (matter.matter_id,))
                    raise RuntimeError('synthetic rollback')
            assert service.repository.state(matter.matter_id) == before
            ordered = []
            for descending in (False, True):
                boundary = b''
                walk = []
                while page := service.repository.page(matter.matter_id, 'name', descending, boundary=boundary, limit=1):
                    walk.append(page[0])
                    boundary = page[0][0]
                ordered.append(walk)
            values = service.repository.rows('SELECT document_id,payload FROM workbench_discovery_index_item WHERE matter_id=? ORDER BY document_id', (matter.matter_id,))
            view = service.current(matter.matter_id, job.store, definition)
            choice = presentation.choose(definition, {}, view)
            windows = []
            for needle in ('synthetic', 'SYN000', '%_', 'no matching synthetic value'):
                listing = query.list_page(definition, view, choice, base='/synthetic', q=needle, sort='name',
                                          descending=False, page=1, doc='', edge='', show_all=False)
                windows.append(([row['item'].document_id for row in listing['rows']],
                                listing['coverage'], listing['matches_so_far'], listing['filter_complete']))
            retained = service.repository.state(matter.matter_id)
            service.cancel(matter.matter_id)
            service.advance(job, 1)
            assert service.repository.state(matter.matter_id) == retained and job.cancelled
            results.append((checkpoints, ordered, values, windows))
        assert results[0] == results[1]
    finally:
        sqlite.close()
