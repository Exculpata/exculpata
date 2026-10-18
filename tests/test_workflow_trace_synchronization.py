"""Trace instrumentation must share worker serialization without locking HTTP."""
import pytest

from tests.test_evidence_graph_workflow import connections
from tests.test_matter_context_workflow import (
    test_http_selection_conflicts_sources_exports_no_model_and_reload as context_workflow,
)
from tests.test_matter_knowledge_workflow import (
    test_notebook_connects_same_name_identities_both_accounts_and_original_returns as knowledge_workflow,
)


@pytest.mark.parametrize('workflow, trace_pairs', [
    pytest.param(context_workflow, 3, id='context'),
    pytest.param(knowledge_workflow, 1, id='knowledge'),
])
def test_workflow_trace_changes_are_serialized_but_http_is_not_locked(
    connections, monkeypatch, workflow, trace_pairs,
):
    client = connections['client']
    workspace = client.app.state.workbench.workspace
    original = workspace.connection
    observed = []

    class CheckedConnection:
        def __getattr__(self, name):
            return getattr(original, name)

        def __enter__(self):
            # fixture-write: Forward the instrumented product connection context unchanged so caller transaction behavior remains observable.
            return original.__enter__()

        def __exit__(self, *args):
            # fixture-write: Forward the instrumented product connection context unchanged so caller transaction behavior remains observable.
            return original.__exit__(*args)

        def set_trace_callback(self, callback):
            assert workspace._lock._is_owned(), 'Trace callback change lacks workspace lock'
            observed.append(callback is not None)
            return original.set_trace_callback(callback)  # fixture-write: Forward after asserting caller lock ownership; acquiring a lock here would mask the regression.

    original_get = client.get

    def unlocked_get(*args, **kwargs):
        assert not workspace._lock._is_owned(), 'HTTP request retains workspace lock'
        return original_get(*args, **kwargs)

    monkeypatch.setattr(client, 'get', unlocked_get)
    with workspace._lock:
        workspace.connection = CheckedConnection()
    try:
        # Run the original workflow, including live workers and every assertion.
        workflow(connections, monkeypatch)
        assert observed == [True, False] * trace_pairs
    finally:
        with workspace._lock:
            original.set_trace_callback(None)
            workspace.connection = original
