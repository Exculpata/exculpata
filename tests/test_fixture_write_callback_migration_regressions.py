"""Exercise real migrated callbacks with live lock-ownership assertions."""
import pytest

from tests import test_briefing as briefing
from tests import test_evidence_graph as graph
from tests.test_briefing import workbench  # noqa: F401
from tests.test_evidence_graph import graph_case  # noqa: F401


@pytest.mark.parametrize('scenario', ['briefing', 'graph'])
def test_projection_callback_changes_hold_the_owning_lock(request, monkeypatch, scenario):
    fixture = request.getfixturevalue('workbench' if scenario == 'briefing' else 'graph_case')
    store = fixture[1].workspace if scenario == 'briefing' else fixture[0]
    original = store.connection
    observed = []

    class CheckedConnection:
        def __getattr__(self, name):
            return getattr(original, name)

        def __enter__(self):
            return original.__enter__()

        def __exit__(self, *args):
            return original.__exit__(*args)

        def set_trace_callback(self, callback):
            assert store._lock._is_owned(), 'callback replacement requires the owning workspace lock'
            observed.append(callback is not None)
            return original.set_trace_callback(callback)  # fixture-write: Forward only after asserting ownership; acquiring the lock here would conceal the regression.

    with store._lock:
        store.connection = CheckedConnection()
    try:
        if scenario == 'briefing':
            briefing.test_catalog_streams_beyond_one_page_and_counts_root_and_nested_folders(fixture, monkeypatch)
        else:
            graph.test_record_pagination_is_stable_bounded_and_does_not_load_histories(fixture, monkeypatch)
        assert observed == [True, False]
    finally:
        with store._lock:
            original.set_trace_callback(None)
            store.connection = original
