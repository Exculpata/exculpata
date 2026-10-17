"""Synthetic v1 recovery with old cookies never presented against v2."""

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
from contextlib import contextmanager
import hashlib
import os
import sqlite3

import pytest

from case_intelligence import admin_cli, local_accounts
from case_intelligence.local_accounts import LocalAccountRepository
from tests.test_local_account_lifecycle import ACTOR, PASSWORD, identity
from tests.test_local_authentication import _accounts_file


@contextmanager
def connection(path):
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


@pytest.fixture
def migrated(tmp_path):
    repo = LocalAccountRepository(_accounts_file(tmp_path))
    original = repo.path.read_bytes()
    first = identity(tmp_path, repo)
    context, old_cookie = first.login_local('alice.reviewer', PASSWORD)
    matter = first.store.create_matter('Synthetic rollback retention', '', context.principal_id)
    foreign = first.store.create_session(context.principal_id,
        hashlib.sha256(b'synthetic-provider-session').hexdigest(), 'oidc',
        '2099-01-01T00:00:00Z', '2099-01-01T00:00:00Z')
    audit = first.store.append_audit_event(actor_principal_id=context.principal_id,
        session_id=context.session.session_id, matter_id=matter.matter_id,
        request_id='synthetic-rollback-request', action='synthetic.rollback', outcome='success')
    workspace = first.store.path
    first.store.close()
    backup = tmp_path / 'recovery/accounts-v1.json'
    repo.migrate(backup, actor=ACTOR)
    second = identity(tmp_path, repo)
    _, new_cookie = second.login_local('alice.reviewer', PASSWORD)
    second.store.close()
    key = workspace.parent / 'identity-session.key'
    with connection(workspace) as db:
        schema = db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall()
    return dict(repo=repo, original=original, backup=backup, workspace=workspace,
        old_cookie=old_cookie, new_cookie=new_cookie, key=key, key_bytes=key.read_bytes(),
        foreign=foreign, matter=matter, audit=audit, schema=schema)


def rows(state, query):
    with connection(state['workspace']) as db:
        # fixture-write: All rows callers inspect the stopped identity database with SELECT queries.
        return db.execute(query).fetchall()


def test_operator_rollback_invalidates_both_cookie_generations_without_intervening_requests(migrated, tmp_path):
    state = migrated
    assert rows(state, "SELECT count(*) FROM workbench_session WHERE auth_method='local'") == [(2,)]
    foreign_before = rows(state, "SELECT * FROM workbench_session WHERE auth_method != 'local'")
    audit_before = rows(state, "SELECT * FROM workbench_audit_event")
    result = state['repo'].rollback(state['backup'], state['workspace'], actor=ACTOR, writers_stopped=True)
    assert result.action == 'rollback'
    assert state['repo'].path.read_bytes() == state['backup'].read_bytes() == state['original']
    assert state['repo'].path.stat().st_mode & 0o777 == 0o600
    assert state['key'].read_bytes() == state['key_bytes']
    assert rows(state, 'SELECT type,name,sql FROM sqlite_master ORDER BY type,name') == state['schema']
    old_digests = {hashlib.sha256(state[name].encode()).hexdigest() for name in ('old_cookie','new_cookie')}
    assert not old_digests.intersection(row[0] for row in rows(state, "SELECT token_digest FROM workbench_session"))
    assert rows(state, "SELECT count(*) FROM workbench_session WHERE auth_method='local' AND revoked_at IS NULL") == [(0,)]
    assert rows(state, "SELECT session_id FROM workbench_session WHERE auth_method != 'local'") == [(state['foreign'].session_id,)]
    assert rows(state, "SELECT * FROM workbench_session WHERE auth_method != 'local'") == foreign_before
    assert rows(state, 'SELECT * FROM workbench_audit_event') == audit_before
    assert rows(state, 'SELECT matter_id FROM workbench_matter') == [(state['matter'].matter_id,)]
    # Append-only audit references and the historical session IDs are retained.
    assert rows(state, "SELECT event_id,session_id FROM workbench_audit_event WHERE action='synthetic.rollback'") == [(state['audit'].event_id, state['audit'].session_id)]
    restored = identity(tmp_path, state['repo'])
    try:
        assert restored.resolve(state['old_cookie']) is None
        assert restored.resolve(state['new_cookie']) is None
        context, fresh = restored.login_local('alice.reviewer', PASSWORD)
        assert context.is_administrator and restored.resolve(fresh)
    finally:
        restored.store.close()


def test_revocation_failure_prevents_account_replacement(migrated):
    state = migrated
    current = state['repo'].path.read_bytes()
    with connection(state['workspace']) as db:
        # fixture-write: Install rollback failure injection after both fixture identity stores are closed.
        db.execute("CREATE TRIGGER synthetic_purge_failure BEFORE UPDATE ON workbench_session BEGIN SELECT RAISE(ABORT,'synthetic purge failure'); END")
    with pytest.raises(RuntimeError, match='could not be invalidated'):
        state['repo'].rollback(state['backup'], state['workspace'], actor=ACTOR, writers_stopped=True)
    assert state['repo'].path.read_bytes() == current
    assert state['backup'].read_bytes() == state['original']
    assert rows(state, "SELECT count(*) FROM workbench_session WHERE auth_method='local'") == [(2,)]


@pytest.mark.parametrize('after_replacement', [False, True])
def test_account_write_failure_leaves_sessions_revoked_and_can_retry(migrated, monkeypatch, after_replacement):
    state = migrated
    current = state['repo'].path.read_bytes()
    original_write = local_accounts._write
    def failed_write(*args, **kwargs):
        assert rows(state, "SELECT count(*) FROM workbench_session WHERE auth_method='local' AND revoked_at IS NULL") == [(0,)]
        if after_replacement:
            original_write(*args, **kwargs)
        raise OSError('Synthetic account replacement failure')
    monkeypatch.setattr(local_accounts, '_write', failed_write)
    with pytest.raises(OSError, match='Synthetic account replacement'):
        state['repo'].rollback(state['backup'], state['workspace'], actor=ACTOR, writers_stopped=True)
    assert state['repo'].path.read_bytes() == (state['original'] if after_replacement else current)
    assert state['backup'].read_bytes() == state['original']
    assert state['key'].read_bytes() == state['key_bytes']
    assert rows(state, "SELECT session_id FROM workbench_session WHERE auth_method != 'local'") == [(state['foreign'].session_id,)]
    monkeypatch.setattr(local_accounts, '_write', original_write)
    state['repo'].rollback(state['backup'], state['workspace'], actor=ACTOR, writers_stopped=True)
    assert state['repo'].path.read_bytes() == state['original']


@pytest.mark.parametrize('fault', ['missing', 'wrong', 'incompatible', 'symlink', 'parent_symlink', 'journal_symlink', 'mode', 'hardlink'])
def test_invalid_workspace_never_restores_accounts(migrated, tmp_path, fault):
    state = migrated
    current = state['repo'].path.read_bytes()
    target = state['workspace']
    if fault in {'missing', 'wrong', 'incompatible'}:
        target = tmp_path / 'other.sqlite'
        if fault != 'missing':
            with connection(target) as db:
                if fault == 'wrong':
                    db.execute('CREATE TABLE unrelated(value TEXT)')
                else:
                    # fixture-write: Build an incompatible isolated database that no WorkspaceStore has opened.
                    db.execute('CREATE TABLE workbench_principal(principal_id TEXT)')
                    # fixture-write: Build an incompatible isolated session schema for operator rollback refusal.
                    db.execute('CREATE TABLE workbench_session(session_id TEXT, auth_method TEXT)')
    elif fault == 'symlink':
        target = tmp_path / 'linked.sqlite'
        target.symlink_to(state['workspace'])
    elif fault == 'parent_symlink':
        parent = tmp_path / 'linked-runtime'
        parent.symlink_to(state['workspace'].parent)
        target = parent / state['workspace'].name
    elif fault == 'journal_symlink':
        companion = target.with_name(target.name + '-wal')
        companion.symlink_to(tmp_path / 'untouched-journal')
    elif fault == 'mode':
        target.chmod(0o666)
    elif fault == 'hardlink':
        os.link(target, tmp_path / 'linked-backup.sqlite')
    with pytest.raises((OSError, RuntimeError)):
        state['repo'].rollback(state['backup'], target, actor=ACTOR, writers_stopped=True)
    assert state['repo'].path.read_bytes() == current
    assert state['backup'].read_bytes() == state['original']
    if fault == 'missing':
        assert not target.exists()
    if fault == 'journal_symlink':
        assert not (tmp_path / 'untouched-journal').exists()
        companion.unlink()
    assert rows(state, "SELECT count(*) FROM workbench_session WHERE auth_method='local'") == [(2,)]


@pytest.mark.parametrize('boundary', ['running', 'browser', 'v2_backup'])
def test_rollback_requires_operator_stop_and_v1_backup(migrated, boundary):
    state = migrated
    current = state['repo'].path.read_bytes()
    if boundary == 'browser':
        state['repo'].guard = lambda accounts: None
    if boundary == 'v2_backup':
        state['backup'].write_bytes(current)
    with pytest.raises(RuntimeError):
        state['repo'].rollback(state['backup'], state['workspace'], actor=ACTOR,
                               writers_stopped=boundary != 'running')
    assert state['repo'].path.read_bytes() == current
    assert rows(state, "SELECT count(*) FROM workbench_session WHERE auth_method='local'") == [(2,)]


def test_rollback_cli_requires_confirmation_and_emits_only_receipt(migrated, monkeypatch, capsys):
    state = migrated
    arguments = [f"{LEGACY_STEM}", 'accounts', 'rollback', '--file', str(state['repo'].path),
        '--backup-file', str(state['backup']), '--workspace-file', str(state['workspace'])]
    monkeypatch.setattr('sys.argv', arguments)
    with pytest.raises(SystemExit) as error:
        admin_cli.main()
    assert error.value.code == 1
    assert 'Stop the application' in capsys.readouterr().err
    monkeypatch.setattr('sys.argv', arguments + ['--confirm-stopped'])
    admin_cli.main()
    output = capsys.readouterr()
    assert 'rollback; account=*; actor=uid:' in output.out and not output.err
    assert PASSWORD not in output.out and '$argon2' not in output.out
    assert state['repo'].path.read_bytes() == state['original']


def test_existing_revocation_timestamp_and_history_survive_repeated_rollback(migrated):
    state = migrated
    timestamp = '2026-01-01T00:00:00Z'
    with connection(state['workspace']) as db:
        session_id = db.execute("SELECT session_id FROM workbench_session WHERE auth_method='local' LIMIT 1").fetchone()[0]
        # fixture-write: Seed prior revocation after both fixture identity stores are closed.
        db.execute('UPDATE workbench_session SET revoked_at=? WHERE session_id=?', (timestamp, session_id))
        session_ids = db.execute('SELECT session_id FROM workbench_session ORDER BY session_id').fetchall()
    for _ in range(2):
        state['repo'].rollback(state['backup'], state['workspace'], actor=ACTOR, writers_stopped=True)
        with connection(state['workspace']) as db:
            assert db.execute('SELECT revoked_at FROM workbench_session WHERE session_id=?', (session_id,)).fetchone() == (timestamp,)
            assert db.execute('SELECT session_id FROM workbench_session ORDER BY session_id').fetchall() == session_ids
        assert state['repo'].path.read_bytes() == state['backup'].read_bytes() == state['original']
