"""The write boundary for synthetic fixtures sharing a live workspace.

Use public workflow methods for normal transitions. When a fixture must seed or
corrupt internal state, keep the complete mutation inside ``fixture_write``.
"""
from contextlib import contextmanager


@contextmanager
def fixture_write(workspace):
    """Serialize fixture writes through commit/rollback on the current lock.

    The yielded connection is scoped to this boundary. Do not retain it for
    later writes or wait for a worker/HTTP request here. Media API methods may
    commit individually; the outer boundary serializes the complete sequence.
    A nested SQL fixture uses a savepoint and never commits its caller's work.
    """
    with workspace._lock:
        connection = workspace.connection
        if connection.in_transaction:
            with _nested_write(connection):
                yield connection
        else:
            with connection:
                yield connection


@contextmanager
def _nested_write(connection):
    connection.execute('SAVEPOINT fixture_write_nested')
    try:
        yield
    except BaseException:
        # OR ROLLBACK can end the entire transaction and remove the savepoint.
        # Preserve that database error instead of masking it during cleanup.
        if connection.in_transaction:
            connection.execute('ROLLBACK TO fixture_write_nested')
            connection.execute('RELEASE fixture_write_nested')
        raise
    else:
        if connection.in_transaction:
            connection.execute('RELEASE fixture_write_nested')
