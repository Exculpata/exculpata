"""A generated legacy database, atomic upgrade, and an isolated restore drill."""

from tests.case_lifecycle_helpers import lifecycle_session
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
import sqlite3

import pytest

from case_intelligence.case_lifecycle_migration import migrate_case_lifecycle
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
OWNER = 'principal-synthetic-owner'


def legacy_database(path):
    """Create only synthetic pre-upgrade tables from checked-in migration SQL."""
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    migrations = resources.files('case_intelligence').joinpath('migrations/sqlite')
    for source in sorted(migrations.iterdir(), key=lambda entry: entry.name):
        if '0002_' <= source.name < '0040_':
            connection.executescript(source.read_text(encoding='utf-8'))
    with connection:
        connection.execute(
            'INSERT INTO workbench_principal VALUES (?,?,?,?,?,1,?,?)',
            (OWNER, 'test', 'generated-owner', 'Generated Owner', 'generated', '2026-01-01T00:00:00Z',
             '2026-01-01T00:00:00Z'))
        for suffix, state in [('scheduled', 'active'), ('unscheduled', 'active'),
                              ('failed', 'purge_failed'), ('purging', 'purging'), ('deleted', 'deleted')]:
            matter_id = 'ci-matter-synthetic-' + suffix
            connection.execute('INSERT INTO workbench_matter VALUES (?,?,?,?,?,?,?)',
                (matter_id, 'm-' + {'scheduled':'a','unscheduled':'b','failed':'c','purging':'d','deleted':'e'}[suffix] * 12,
                 'Generated ' + suffix, 'Generated fixture', OWNER,
                 '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'))
            connection.execute(
                'INSERT INTO workbench_matter_lifecycle(matter_id,state,purge_id,updated_at) VALUES (?,?,?,?)',
                (matter_id, state, 'matter-purge-' + suffix if state in ('purging','purge_failed') else None,
                 '2026-01-01T00:00:00Z'))
            connection.execute('INSERT INTO workbench_matter_membership VALUES (?,?,?, ?,?,?,?,NULL)',
                (matter_id, OWNER, 'owner', 'active', OWNER, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'))
        connection.execute('INSERT INTO workbench_matter_retention VALUES (?,?,?,?,?,?)',
            ('ci-matter-synthetic-scheduled', '2026-01-31T00:00:00Z', '2026-02-07T00:00:00Z', OWNER,
             '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'))
    return connection


def snapshot(connection, table):
    return tuple(tuple(row) for row in connection.execute(f'SELECT * FROM {table} ORDER BY 1'))


def test_upgrade_keeps_legacy_dates_states_and_requires_explicit_selection(tmp_path):
    path = tmp_path / 'generated.sqlite'
    old = legacy_database(path)
    retained = snapshot(old, 'workbench_matter_retention')
    lifecycle = snapshot(old, 'workbench_matter_lifecycle')
    old.close()
    store = WorkspaceStore(path, clock=lambda: NOW)
    assert snapshot(store.connection, 'workbench_matter_retention') == retained
    assert snapshot(store.connection, 'workbench_matter_lifecycle') == lifecycle
    scheduled = store.case_lifecycle.policy('ci-matter-synthetic-scheduled')
    assert scheduled.mode == 'legacy_review'
    assert scheduled.legacy_expires_at == '2026-01-31T00:00:00Z'
    assert scheduled.legacy_purge_after == '2026-02-07T00:00:00Z'
    unscheduled = store.case_lifecycle.policy('ci-matter-synthetic-unscheduled')
    assert unscheduled.mode == 'unscheduled' and unscheduled.next_check_in_at is None
    assert store.due_matter_retentions() == ()
    assert store.begin_due_matter_purge(scheduled.matter_id, source_count=0) is None
    selected = store.case_lifecycle.check_in(scheduled.matter_id, OWNER, session_id=lifecycle_session(store, OWNER, administrator_override=False).session_id)
    assert selected.mode == 'ongoing' and selected.next_check_in_at == '2026-12-30T00:00:00Z'
    assert snapshot(store.connection, 'workbench_matter_retention') == retained
    for suffix in ('failed', 'purging'):
        with pytest.raises(WorkspaceProblem, match='recovery'):
            slug = 'm-' + ('c' if suffix == 'failed' else 'd') * 12
            store.case_lifecycle.begin_exit(slug, OWNER, session_id=lifecycle_session(store, OWNER, administrator_override=False).session_id)
        with pytest.raises(WorkspaceProblem, match='confirmed current exit'):
            store.case_lifecycle.validate_purge('ci-matter-synthetic-' + suffix, 'matter-purge-' + suffix)
    policies = snapshot(store.connection, 'workbench_case_policy')
    store.close()
    reopened = WorkspaceStore(path, clock=lambda: NOW)
    assert snapshot(reopened.connection, 'workbench_case_policy') == policies
    assert reopened.connection.execute('PRAGMA foreign_key_check').fetchall() == []
    reopened.close()


def test_atomic_failure_restores_old_schema_and_rename_setting(tmp_path):
    connection = legacy_database(tmp_path / 'generated.sqlite')
    connection.create_function('recordbench_case_lifecycle_writer', 0, lambda: 1)
    old_lifecycle = snapshot(connection, 'workbench_matter_lifecycle')
    old_schema = connection.execute("SELECT sql FROM sqlite_master WHERE name='workbench_matter_lifecycle'").fetchone()[0]

    def deny_policy_insert(action, arg1, arg2, database, trigger):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and arg1 == 'workbench_case_policy' else sqlite3.SQLITE_OK

    connection.set_authorizer(deny_policy_insert)
    with pytest.raises(sqlite3.DatabaseError, match='authorized'):
        migrate_case_lifecycle(connection)
    connection.set_authorizer(None)
    assert not connection.in_transaction
    assert connection.execute('PRAGMA legacy_alter_table').fetchone()[0] == 0
    assert snapshot(connection, 'workbench_matter_lifecycle') == old_lifecycle
    assert connection.execute("SELECT sql FROM sqlite_master WHERE name='workbench_matter_lifecycle'").fetchone()[0] == old_schema
    assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='workbench_case_policy'").fetchone() is None
    migrate_case_lifecycle(connection)
    assert snapshot(connection, 'workbench_matter_lifecycle') == old_lifecycle
    connection.close()


def test_synthetic_backup_clean_restore_and_old_writer_refusal(tmp_path):
    path = tmp_path / 'generated.sqlite'
    backup_path = tmp_path / 'generated-pre-upgrade-backup.sqlite'
    old = legacy_database(path)
    before = snapshot(old, 'workbench_matter_lifecycle')
    before_retention = snapshot(old, 'workbench_matter_retention')
    backup = sqlite3.connect(backup_path)
    old.backup(backup)
    backup.close()
    upgraded = WorkspaceStore(path, clock=lambda: NOW)
    # An already-open old writer sees the committed SQL guard too.
    with pytest.raises(sqlite3.OperationalError, match='recordbench_case_lifecycle_writer'):
        old.execute("UPDATE workbench_matter_lifecycle SET state='purging' WHERE state='active'")
    old.rollback()
    # The original 0007 startup backfill must also fail, even on ignored rows.
    with pytest.raises(sqlite3.OperationalError, match='recordbench_case_lifecycle_writer'):
        old.executescript(resources.files('case_intelligence').joinpath(
            'migrations/sqlite/0007_matter_lifecycle.sql').read_text())
    old.close()
    upgraded.close()
    restored_path = tmp_path / 'generated-restored.sqlite'
    with sqlite3.connect(backup_path) as source, sqlite3.connect(restored_path) as restored:
        source.backup(restored)
        assert restored.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert snapshot(restored, 'workbench_matter_lifecycle') == before
        assert snapshot(restored, 'workbench_matter_retention') == before_retention
        assert restored.execute("SELECT 1 FROM sqlite_master WHERE name='workbench_case_policy'").fetchone() is None
    restored_upgrade = WorkspaceStore(restored_path, clock=lambda: NOW)
    assert snapshot(restored_upgrade.connection, 'workbench_matter_lifecycle') == before
    assert snapshot(restored_upgrade.connection, 'workbench_matter_retention') == before_retention
    assert restored_upgrade.due_matter_retentions() == ()
    restored_upgrade.close()


def test_packaged_migration_matches_repository_source():
    root = Path(__file__).resolve().parents[1]
    assert (root / 'migrations/sqlite/0040_case_lifecycle.sql').read_bytes() == (
        root / 'src/case_intelligence/migrations/sqlite/0040_case_lifecycle.sql').read_bytes()


def test_v40_backup_upgrade_fences_old_writer_and_never_invents_authority(tmp_path):
    """Exercise the actual 0040 schema, including its version-one writer fences."""
    path = tmp_path / 'generated-v40.sqlite'
    old = legacy_database(path)
    old.create_function('recordbench_case_lifecycle_writer', 0, lambda: 1)
    old.create_function('recordbench_case_content_cleanup', 1, lambda matter_id: 0)
    old.executescript(resources.files('case_intelligence').joinpath(
        'migrations/sqlite/0040_case_lifecycle.sql').read_text())
    before = snapshot(old, 'workbench_matter_lifecycle')
    retained = snapshot(old, 'workbench_matter_retention')
    backup_path = tmp_path / 'generated-v40-backup.sqlite'
    with sqlite3.connect(backup_path) as backup:
        old.backup(backup)
    upgraded = WorkspaceStore(path, clock=lambda: NOW)
    assert snapshot(upgraded.connection, 'workbench_matter_lifecycle') == before
    assert snapshot(upgraded.connection, 'workbench_matter_retention') == retained
    assert {row[0] for row in upgraded.connection.execute(
        'SELECT policy_version FROM workbench_case_policy')} == {2}
    for table in ('workbench_case_download_attempt', 'workbench_case_download_coverage',
                  'workbench_case_purge_authority', 'workbench_case_session_binding'):
        assert snapshot(upgraded.connection, table) == ()
    with pytest.raises(sqlite3.IntegrityError, match='writer version'):
        old.execute("UPDATE workbench_matter_lifecycle SET state='purging' WHERE state='active'")
    old.rollback()
    assert upgraded.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert upgraded.connection.execute('PRAGMA foreign_key_check').fetchall() == []
    upgraded.close()
    old.close()
    # A stopped backup remains unchanged and upgrades independently after restore.
    restored_path = tmp_path / 'generated-v40-clean-restore.sqlite'
    with sqlite3.connect(backup_path) as source, sqlite3.connect(restored_path) as restored:
        assert {row[0] for row in source.execute('SELECT policy_version FROM workbench_case_policy')} == {1}
        source.backup(restored)
    restored = WorkspaceStore(restored_path, clock=lambda: NOW)
    assert snapshot(restored.connection, 'workbench_matter_lifecycle') == before
    assert snapshot(restored.connection, 'workbench_matter_retention') == retained
    restored.close()


def test_packaged_authority_migration_matches_repository_source():
    root = Path(__file__).resolve().parents[1]
    assert (root / 'migrations/sqlite/0041_case_exit_authority.sql').read_bytes() == (
        root / 'src/case_intelligence/migrations/sqlite/0041_case_exit_authority.sql').read_bytes()
