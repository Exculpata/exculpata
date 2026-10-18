from tests.fixture_probe import fixture_probe
"""Inventory regressions backed by actual shared-connection side effects."""
import sqlite3

import pytest

from case_intelligence.workspace_store import WorkspaceStore
from tests.fixture_write import fixture_write
from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize('assignment', [
    'workspace.connection.row_factory = factory',
    "setattr(workspace.connection, 'row_factory', factory)",
])
def test_owner_lock_alone_cannot_scope_a_connection_factory(tmp_path, assignment):
    workspace = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    source = (
        'with workspace._lock:\n'
        f'    {assignment}\n'
        "    leaked = workspace.connection.execute('SELECT 1').fetchone()\n"
    )
    try:
        error, leaked = fixture_probe(source, 'leaked', workspace=workspace, factory=lambda cursor, row: cursor.connection)
        if error is not None:
            raise error
        # fixture-write: Inspect the isolated raw escape probe namespace to prove the callback exposed its connection.
        assert leaked is workspace.connection
        assert [v.line for v in check_fixture_writes(source)] == [2]
    finally:
        workspace.close()


@pytest.mark.parametrize('operation, expected_rows', [('queue', 0), ('finish', 1)])
def test_nested_media_calls_that_destroy_caller_ownership_are_inventoried(tmp_path, operation, expected_rows):
    workspace = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    actor = 'synthetic-fixture-owner'
    workspace.upsert_principal('test', actor, 'Synthetic Owner', actor, preferred_principal_id=actor)
    matter = workspace.create_matter('Synthetic nested media', '', actor)
    calls = {
        'queue': "workspace.queue_media_job(matter.matter_id, 'a' * 32, 'b' * 32, actor, source_sha256='c' * 64, byte_size=1, media_type='audio/wav', duration_ms=1)",
        'finish': "workspace.finish_media_job('synthetic-missing-job', degraded=False, message='Synthetic completion')",
    }
    source = (
        'with fixture_write(workspace) as outer:\n'
        "    outer.execute(\"INSERT INTO synthetic_pending VALUES ('caller')\")\n"
        '    try:\n'
        '        with fixture_write(workspace):\n'
        f'            {calls[operation]}\n'
        '    except (sqlite3.OperationalError, KeyError):\n'
        '        pass\n'
        '    transaction_survived = outer.in_transaction\n'
        "    raise ValueError('synthetic caller rollback')\n"
    )
    try:
        with fixture_write(workspace) as connection:
            connection.execute('CREATE TABLE synthetic_pending (value TEXT)')
        error, transaction_survived = fixture_probe(source, 'transaction_survived', workspace=workspace,
            fixture_write=fixture_write, sqlite3=sqlite3, matter=matter, actor=actor)
        with pytest.raises(ValueError, match='synthetic caller rollback'):
            raise error
        # The raw media APIs own transactions: queue rolled back the caller;
        # finish committed it. The inventory must reject either nested seed.
        # fixture-write: Inspect the isolated negative probe namespace to verify the recorded caller transaction was terminated.
        assert not transaction_survived
        assert workspace.connection.execute('SELECT count(*) FROM synthetic_pending').fetchone()[0] == expected_rows
        violations = check_fixture_writes(source)
        assert any(v.line == 5 and 'nested' in v.rule for v in violations)
    finally:
        workspace.close()
