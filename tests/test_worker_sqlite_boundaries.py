"""Reject the unsafe SQLite boundaries identified in the two hosted timeout dumps."""
import threading

import pytest

from tests import test_automatic_discovery as discovery
from tests import test_intake_receipt_http as intake
from tests.test_automatic_discovery import workbench  # noqa: F401
from tests.test_intake_receipt_http import workspace  # noqa: F401


@pytest.mark.parametrize('scenario', ['status', 'readiness', 'intake'])
def test_live_worker_observations_are_serialized(request, monkeypatch, scenario):
    fixture = request.getfixturevalue('workspace' if scenario == 'intake' else 'workbench')
    client, bench, *_ = fixture
    store = bench.workspace
    original = store.connection
    caller = threading.get_ident()
    observations = []

    class CheckedConnection:
        def __getattr__(self, name):
            return getattr(original, name)

        def __enter__(self):
            return original.__enter__()

        def __exit__(self, *args):
            return original.__exit__(*args)

        def set_trace_callback(self, callback):
            assert store._lock._is_owned(), 'callback mutation must serialize with live report claims'
            observations.append('callback')
            return original.set_trace_callback(callback)

        def execute(self, sql, *args, **kwargs):
            if threading.get_ident() == caller and (
                'count(*) from workbench_upload_session' in sql.lower()
                or 'count(*) from workbench_principal where principal_id=' in sql.lower()
                or sql.startswith('EXPLAIN QUERY PLAN ')
            ):
                assert store._lock._is_owned(), 'read must serialize with live ingestion/report SQL'
                observations.append('plan' if sql.startswith('EXPLAIN QUERY PLAN ') else 'read')
                cursor = original.execute(sql, *args, **kwargs)

                class CheckedCursor:
                    def __iter__(self):
                        assert store._lock._is_owned(), 'query-plan iteration must remain locked'
                        for row in cursor:
                            assert store._lock._is_owned(), 'query-plan consumption must remain locked'
                            yield row

                    def fetchone(self):
                        assert store._lock._is_owned(), 'cursor consumption must remain locked'
                        return cursor.fetchone()

                return CheckedCursor()
            return original.execute(sql, *args, **kwargs)

    get, post = client.get, client.post

    def unlocked(method):
        def call(*args, **kwargs):
            assert not store._lock._is_owned(), 'HTTP cannot retain a lock needed by its request thread'
            return method(*args, **kwargs)
        return call

    monkeypatch.setattr(client, 'get', unlocked(get))
    monkeypatch.setattr(client, 'post', unlocked(post))
    with store._lock:
        store.connection = CheckedConnection()
    try:
        if scenario == 'intake':
            intake.test_legacy_http_resume_preserves_received_offsets_and_selection(fixture)
            assert observations == ['read']
        elif scenario == 'status':
            discovery.test_a_status_poll_never_creates_the_system_principal(fixture)
            assert observations == ['read', 'callback', 'callback', 'read']
        else:
            discovery.test_readiness_progress_never_scans_the_coverage_ledger(fixture)
            assert observations[:2] == ['callback', 'callback']
            assert observations[2:] and set(observations[2:]) == {'plan'}
    finally:
        with store._lock:
            original.set_trace_callback(None)
            store.connection = original
