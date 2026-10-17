"""The write boundary for synthetic fixtures sharing a live workspace.

Use public workflow methods for normal transitions. When a fixture must seed or
corrupt internal state, keep the complete mutation inside ``fixture_write``.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import re


_nested_connections = ContextVar("fixture_write_nested_connections", default=())


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
                token = _nested_connections.set((*_nested_connections.get(), connection))
                try:
                    yield _FixtureConnection(connection, nested=True)
                finally:
                    _nested_connections.reset(token)
        else:
            with connection:
                yield _FixtureConnection(connection)


def _reject_transaction_control():
    raise RuntimeError("nested fixture_write cannot end its caller's transaction; use execute/executemany for fixture SQL")


def _check_nested_sql(sql):
    # SQLite permits leading comments, empty statements and a UTF-8 BOM.
    commands = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|--[^\n]*|/\*[\s\S]*?\*/", " ", sql).lstrip(";\ufeff \t\r\n\v\f")
    if (re.match(r"(?:BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b", commands, re.I)
            or re.search(r"\b(?:OR|ON\s+CONFLICT)\s+ROLLBACK\b|\bRAISE\s*\(\s*ROLLBACK\b", commands, re.I)):
        _reject_transaction_control()


def _check_nested_schema(connection):
    # A plain INSERT can invoke a rollback policy stored in a constraint or
    # trigger. Reject that schema before starting a nested fixture rather than
    # discover transaction loss after the caller's pending work is gone.
    for database in connection.execute('PRAGMA database_list'):
        name = database[1].replace('"', '""')
        for row in connection.execute(f'SELECT sql FROM "{name}".sqlite_schema WHERE sql IS NOT NULL'):
            _check_nested_sql(row[0])


class _FixtureConnection:
    """A scoped SQL handle that leaves transaction ownership with the caller.

    Do not install an SQLite authorizer: callers may already have one, and the
    sqlite3 API cannot retrieve it for restoration. The inventory guard requires
    this yielded handle for scripts and explicit transaction control.
    """

    def __init__(self, connection, *, nested=False):
        self._connection = connection
        self._nested = nested

    def _owns_caller_transaction(self):
        # An outer handle retained inside a nested boundary must not bypass it.
        return self._nested or any(item is self._connection for item in _nested_connections.get())

    def _check_transaction_control(self):
        if self._owns_caller_transaction():
            _reject_transaction_control()

    def _check_sql(self, sql):
        if self._owns_caller_transaction():
            _check_nested_sql(sql)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __setattr__(self, name, value):
        if name in {"autocommit", "isolation_level"}:
            self._check_transaction_control()
            setattr(self._connection, name, value)
            return
        object.__setattr__(self, name, value)

    def execute(self, sql, *args, **kwargs):
        self._check_sql(sql)
        return _FixtureCursor(self, self._connection.execute(sql, *args, **kwargs))

    def executemany(self, sql, *args, **kwargs):
        self._check_sql(sql)
        return _FixtureCursor(self, self._connection.executemany(sql, *args, **kwargs))

    def cursor(self, *args, **kwargs):
        return _FixtureCursor(self, self._connection.cursor(*args, **kwargs))

    def executescript(self, *args, **kwargs):
        # Even a SELECT-only script implicitly commits a pending transaction.
        self._check_transaction_control()
        return _FixtureCursor(self, self._connection.executescript(*args, **kwargs))

    def commit(self):
        self._check_transaction_control()
        self._connection.commit()

    def rollback(self):
        self._check_transaction_control()
        self._connection.rollback()

    def close(self):
        self._check_transaction_control()
        self._connection.close()

    def __enter__(self):
        self._check_transaction_control()
        self._connection.__enter__()
        return self

    def __exit__(self, *args):
        self._check_transaction_control()
        return self._connection.__exit__(*args)


class _FixtureCursor:
    def __init__(self, connection, cursor):
        self.connection = connection
        self._cursor = cursor

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._cursor)

    def execute(self, sql, *args, **kwargs):
        self.connection._check_sql(sql)
        self._cursor.execute(sql, *args, **kwargs)
        return self

    def executemany(self, sql, *args, **kwargs):
        self.connection._check_sql(sql)
        self._cursor.executemany(sql, *args, **kwargs)
        return self

    def executescript(self, *args, **kwargs):
        self.connection._check_transaction_control()
        self._cursor.executescript(*args, **kwargs)
        return self


@contextmanager
def _nested_write(connection):
    _check_nested_schema(connection)
    connection.execute('SAVEPOINT fixture_write_nested')
    try:
        yield
    except BaseException:
        # External APIs or SQLite failures can remove the savepoint. Preserve
        # the original error instead of masking it during cleanup.
        if connection.in_transaction:
            connection.execute('ROLLBACK TO fixture_write_nested')
            connection.execute('RELEASE fixture_write_nested')
        raise
    else:
        if connection.in_transaction:
            connection.execute('RELEASE fixture_write_nested')
