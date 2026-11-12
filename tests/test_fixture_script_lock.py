"""Assert fixture scripts hold the shared workspace connection's lock."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

import pytest

from case_intelligence.workspace_store import WorkspaceStore
from tests import test_discovery_review_list as target
from tests.fixture_write import fixture_script
from tests.test_discovery_review_list import production  # noqa: F401


class RecordingConnection:
    def __init__(self, workspace, observed, competing=None):
        object.__setattr__(self, '_workspace', workspace)
        object.__setattr__(self, '_connection', workspace.connection)
        object.__setattr__(self, '_observed', observed)
        object.__setattr__(self, '_competing', competing)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __setattr__(self, name, value):
        setattr(self._connection, name, value)

    def __enter__(self):
        # fixture-write: Forward the real connection context without changing caller transaction behavior.
        return self._connection.__enter__()

    def __exit__(self, *args):
        # fixture-write: Forward the real connection context without changing caller transaction behavior.
        return self._connection.__exit__(*args)

    def executescript(self, sql_script):
        self._observed.append(self._workspace._lock._is_owned())
        if self._competing is not None:
            def try_lock():
                acquired = self._workspace._lock.acquire(blocking=False)
                if acquired:
                    self._workspace._lock.release()
                return acquired

            with ThreadPoolExecutor(1) as pool:
                self._competing.append(pool.submit(try_lock).result(timeout=10))
        # fixture-write: Delegate synthetic scripts unchanged so real transaction behavior remains observable.
        return self._connection.executescript(sql_script)


def install_recording_connection(workspace, observed, competing=None):
    with workspace._lock:
        original = workspace.connection
        workspace.connection = RecordingConnection(workspace, observed, competing)
    return original


def test_fixture_script_holds_lock_and_returns_result(tmp_path):
    workspace = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    observed = []
    original = install_recording_connection(workspace, observed)
    try:
        result = fixture_script(workspace, 'SELECT 1;')
        assert result.connection is original
        assert result.fetchone() is None
        assert observed == [True]
    finally:
        with workspace._lock:
            workspace.connection = original
        workspace.close()


@pytest.mark.parametrize('installed,expected_calls', [(True, 2), (False, 1)])
@pytest.mark.parametrize('operation', ['insert', 'update'])
def test_domain_rejection_body_holds_script_lock(production, installed, expected_calls, operation):
    workspace = production[1].workspace
    observed = []
    original = install_recording_connection(workspace, observed)
    try:
        target.test_identifier_domain_rejection_preserves_caller_transaction(production, installed, operation)
        assert observed == [True] * expected_calls
    finally:
        with workspace._lock:
            workspace.connection = original


@pytest.mark.parametrize('legacy', ['def001', 'DÉF002', 'bad'])
def test_migration_body_holds_script_lock(tmp_path, monkeypatch, legacy):
    observed = []
    original_init = WorkspaceStore.__init__
    cleanup = ExitStack()

    def restore_connection(workspace, original):
        with workspace._lock:
            workspace.connection = original

    def checked_init(workspace, *args, **kwargs):
        original_init(workspace, *args, **kwargs)
        original = install_recording_connection(workspace, observed)
        cleanup.callback(restore_connection, workspace, original)

    monkeypatch.setattr(WorkspaceStore, '__init__', checked_init)
    try:
        target.test_identifier_migration_preserves_spelling_and_rejects_existing_collisions(tmp_path, legacy)
        assert observed == [True]
    finally:
        cleanup.close()


def test_fixture_script_excludes_second_thread(tmp_path):
    workspace = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    observed, competing = [], []
    original = install_recording_connection(workspace, observed, competing)
    try:
        fixture_script(workspace, 'BEGIN IMMEDIATE; SELECT 1; COMMIT;')
        assert competing == [False]
        assert observed == [True]
    finally:
        with workspace._lock:
            workspace.connection = original
        workspace.close()
