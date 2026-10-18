"""The write boundary for synthetic fixtures sharing a live workspace.

Use public workflow methods for normal transitions. When a fixture must seed or
corrupt internal state, keep the complete mutation inside ``fixture_write``.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import re
import sqlite3
from threading import get_ident, local


_active_scopes = local()


def _current_scopes():
    return getattr(_active_scopes, "stack", ())


@dataclass
class _FixtureScope:
    connection: object
    nested: bool
    thread: int
    active: bool = True


@contextmanager
def fixture_write(workspace, *, media=False):
    """Serialize fixture writes through commit/rollback on the current lock.

    The yielded connection is scoped to this boundary. Do not retain it for
    later writes or wait for a worker/HTTP request here. Manual media setup must
    use ``media=True``: those public APIs commit individually, so this mode
    requires an outer boundary without a caller transaction and serializes the
    complete sequence. It does not make multiple media API calls atomic.
    A nested SQL fixture uses a savepoint and never commits its caller's work.
    """
    if not isinstance(media, bool):
        raise TypeError("fixture_write media must be a bool")
    with workspace._lock:
        connection = workspace.connection
        if getattr(connection, "autocommit", None) is True:
            raise RuntimeError("fixture_write does not support autocommit=True")
        active = _current_scopes()
        nested = connection.in_transaction or any(scope.active and scope.connection is connection for scope in active)
        if media and nested:
            raise RuntimeError("fixture_write(media=True) requires an outer boundary without a caller transaction")
        scope = _FixtureScope(connection, nested, get_ident())
        _active_scopes.stack = (*active, scope)
        handle = _FixtureConnection(scope)
        original_factory = connection.row_factory
        connection.row_factory = _scoped_factory(handle, original_factory)
        try:
            if nested:
                with _nested_write(connection):
                    yield handle
            else:
                with connection:
                    yield handle
        finally:
            # Restore scoped callbacks before workers regain the workspace lock.
            connection.row_factory = original_factory
            scope.active = False
            _active_scopes.stack = active


def _reject_transaction_control():
    raise RuntimeError("nested fixture_write cannot end its caller's transaction; use execute/executemany for fixture SQL")


def _sql_commands(sql):
    # SQLite permits leading comments, empty statements and a UTF-8 BOM.
    return re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|--[^\n]*|/\*[\s\S]*?\*/", " ", sql).lstrip(";\ufeff \t\r\n\v\f")


def _check_nested_sql(sql):
    commands = _sql_commands(sql)
    if (re.match(r"(?:BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b", commands, re.I)
            or re.search(r"\b(?:OR|ON\s+CONFLICT)\s+ROLLBACK\b|\bRAISE\s*\(\s*ROLLBACK\b", commands, re.I)):
        _reject_transaction_control()


def _check_attachment_sql(sql):
    if re.match(r"(?:ATTACH|DETACH|VACUUM)\b", _sql_commands(sql), re.I):
        raise RuntimeError("fixture_write does not support attachment changes or VACUUM")


_SQL_IDENTIFIER = r'''(?:\w+|"(?:""|[^"])*"|'(?:''|[^'])*'|`(?:``|[^`])*`|\[[^\]]*\])'''
_PRAGMA = re.compile(
    rf"^(?:EXPLAIN\s+(?:QUERY\s+PLAN\s+)?)?PRAGMA\s+"
    rf"(?:{_SQL_IDENTIFIER}\s*\.\s*)?({_SQL_IDENTIFIER})(.*)$", re.I | re.S,
)
_SQL_COMMENT_OR_QUOTED = re.compile(
    r''''(?:''|[^'])*'|"(?:""|[^"])*"|`(?:``|[^`])*`|\[[^\]]*\]|--[^\n]*|/\*[\s\S]*?\*/'''
)
_READ_ARGUMENT_PRAGMAS = {
    "table_info", "table_xinfo", "table_list", "index_info", "index_xinfo",
    "index_list", "foreign_key_list", "foreign_key_check", "integrity_check", "quick_check",
}
_ACTION_PRAGMAS = {"optimize", "wal_checkpoint", "incremental_vacuum", "shrink_memory"}
_TRANSACTIONAL_PRAGMAS = {"user_version", "application_id"}


def _check_pragma_sql(sql):
    """Return whether this is a PRAGMA query; reject nontransactional changes.

    Connection settings can ignore a surrounding BEGIN or survive ROLLBACK.
    Only the two supported transactional metadata setters belong in a fixture
    write. EXPLAIN does not prevent prepare-time PRAGMA configuration changes.
    """
    commands = _SQL_COMMENT_OR_QUOTED.sub(
        lambda match: " " if match[0].startswith(("--", "/*")) else match[0], sql,
    ).lstrip(";\ufeff \t\r\n\v\f")
    pragma = _PRAGMA.match(commands)
    if pragma is None:
        return False
    name, argument = pragma.groups()
    name = name.strip("\"'`[]").lower()
    argument = argument.lstrip()
    setting = argument.startswith("=") or (argument.startswith("(") and name not in _READ_ARGUMENT_PRAGMAS)
    if name in _ACTION_PRAGMAS or (setting and name not in _TRANSACTIONAL_PRAGMAS):
        raise RuntimeError(f"fixture_write does not support nontransactional PRAGMA {name}")
    return not setting


def _check_nested_schema(connection):
    # A plain INSERT can invoke a rollback policy stored in a constraint or
    # trigger. Reject that schema before starting a nested fixture rather than
    # discover transaction loss after the caller's pending work is gone.
    cursor = connection.cursor()
    cursor.row_factory = None
    try:
        names = cursor.execute('SELECT CAST(name AS BLOB) FROM pragma_database_list').fetchall()
        for database in names:
            name = database[0].decode('utf-8').replace('"', '""')
            for row in cursor.execute(f'SELECT CAST(sql AS BLOB) FROM "{name}".sqlite_schema WHERE sql IS NOT NULL'):
                _check_nested_sql(row[0].decode('utf-8'))
    finally:
        cursor.close()


class _FixtureConnection:
    """A scoped SQL handle that leaves transaction ownership with the caller.

    Do not install an SQLite authorizer: callers may already have one, and the
    sqlite3 API cannot retrieve it for restoration. The inventory guard requires
    this yielded handle for scripts and explicit transaction control.
    """

    def __init__(self, scope):
        self._scope = scope
        self._connection = scope.connection
        self._nested = scope.nested

    def _check_active(self):
        if not self._scope.active or self._scope.thread != get_ident():
            raise RuntimeError("fixture_write handle is only usable inside its owning boundary")

    def _owns_caller_transaction(self):
        self._check_active()
        # An outer handle retained inside a nested boundary must not bypass it.
        return self._nested or any(scope.active and scope.connection is self._connection and scope.nested for scope in _current_scopes())

    def _check_transaction_control(self):
        if self._owns_caller_transaction():
            _reject_transaction_control()

    def _check_sql(self, sql):
        self._check_active()
        _check_attachment_sql(sql)
        pragma_query = _check_pragma_sql(sql)
        if self._owns_caller_transaction():
            _check_nested_sql(sql)
            # Caller APIs may change schemas after nested entry.
            _check_nested_schema(self._connection)
        elif (not self._connection.in_transaction and not pragma_query and
              not re.match(r"(?:SELECT|EXPLAIN|BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b", _sql_commands(sql), re.I)):
            # SQLite does not implicitly begin for DDL, CTE writes or prefixed
            # DML. Begin at the SQL handle, leaving media API entry lazy.
            self._connection.execute('BEGIN')

    def __getattr__(self, name):
        self._check_active()
        value = getattr(self._connection, name)
        if name == "row_factory" and isinstance(value, _ScopedRowFactory):
            return value.factory
        if name in {"row_factory", "text_factory"} or not callable(value):
            return value
        if name not in {"set_authorizer", "set_trace_callback", "set_progress_handler",
                        "create_function", "create_aggregate", "create_collation", "create_window_function",
                        "getlimit", "setlimit", "interrupt"}:
            raise RuntimeError(f"fixture_write does not support connection operation {name}")
        # A bound method saved while active must recheck at invocation time.
        def scoped_call(*args, **kwargs):
            self._check_active()
            return value(*args, **kwargs)
        return scoped_call

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        self._check_active()
        if name in {"autocommit", "isolation_level"}:
            self._check_transaction_control()
        if name == "autocommit" and value is True:
            raise RuntimeError("fixture_write does not support autocommit=True")
        if name == "row_factory":
            value = _scoped_factory(self, value)
        setattr(self._connection, name, value)

    def execute(self, sql, *args, **kwargs):
        self._check_sql(sql)
        return _FixtureCursor(self, self._connection.execute(sql, *args, **kwargs))

    def executemany(self, sql, *args, **kwargs):
        self._check_sql(sql)
        return _FixtureCursor(self, self._connection.executemany(sql, *args, **kwargs))

    def cursor(self, *args, **kwargs):
        self._check_active()
        factory = args[0] if args else kwargs.get('factory', sqlite3.Cursor)
        if factory is not sqlite3.Cursor:
            raise RuntimeError('fixture_write does not support custom cursor factories')
        return _FixtureCursor(self, self._connection.cursor(*args, **kwargs))

    def executescript(self, sql_script):
        cursor = self.cursor()
        return cursor.executescript(sql_script)

    def _execute_script(self, cursor, sql_script):
        self._check_transaction_control()
        # sqlite3.executescript commits before execution. Execute complete SQLite
        # statements individually instead, preserving triggers and quoted ';'.
        # Explicit transaction control/rollback policies cannot escape this unit.
        _check_nested_schema(self._connection)
        statements, start = [], 0
        for index, character in enumerate(sql_script):
            if character == ';' and sqlite3.complete_statement(sql_script[start:index + 1]):
                statements.append(sql_script[start:index + 1])
                start = index + 1
        statements.append(sql_script[start:])
        for statement in statements:
            _check_attachment_sql(statement)
            _check_pragma_sql(statement)
            _check_nested_sql(statement)
        if not self._connection.in_transaction:
            self._connection.execute('BEGIN')
        for statement in statements:
            cursor.execute(statement)

    def commit(self):
        self._check_transaction_control()
        self._connection.commit()

    def rollback(self):
        self._check_transaction_control()
        self._connection.rollback()

    def close(self):
        self._check_active()
        raise RuntimeError("fixture_write does not own the shared connection lifetime; close the workspace after its workers stop")

    def __enter__(self):
        self._check_transaction_control()
        self._connection.__enter__()
        return self

    def __exit__(self, *args):
        self._check_transaction_control()
        return self._connection.__exit__(*args)


class _ScopedRowFactory:
    def __init__(self, connection, factory):
        self.connection = connection
        self.factory = factory

    def __call__(self, cursor, row):
        self.connection._check_active()
        return self.factory(_FixtureCursor(self.connection, cursor, wrap_factory=False), row)


def _scoped_factory(connection, factory):
    if isinstance(factory, _ScopedRowFactory):
        factory = factory.factory
    return factory if factory is None or factory is sqlite3.Row else _ScopedRowFactory(connection, factory)


class _FixtureCursor:
    def __init__(self, connection, cursor, *, wrap_factory=True):
        self._connection = connection
        self._cursor = cursor
        if wrap_factory:
            cursor.row_factory = _scoped_factory(connection, cursor.row_factory)

    @property
    def connection(self):
        self._connection._check_active()
        return self._connection

    def __getattr__(self, name):
        self.connection._check_active()
        value = getattr(self._cursor, name)
        if name == "row_factory" and isinstance(value, _ScopedRowFactory):
            return value.factory
        if name == "row_factory" or not callable(value):
            return value
        def scoped_call(*args, **kwargs):
            self.connection._check_active()
            return value(*args, **kwargs)
        return scoped_call

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        self._connection._check_active()
        if name == "connection":
            raise AttributeError("fixture_write cursor connection is read-only")
        if name == "row_factory":
            value = _scoped_factory(self._connection, value)
        setattr(self._cursor, name, value)

    def __iter__(self):
        self.connection._check_active()
        return self

    def __next__(self):
        self.connection._check_active()
        return next(self._cursor)

    def execute(self, sql, *args, **kwargs):
        self.connection._check_sql(sql)
        self._cursor.execute(sql, *args, **kwargs)
        return self

    def executemany(self, sql, *args, **kwargs):
        self.connection._check_sql(sql)
        self._cursor.executemany(sql, *args, **kwargs)
        return self

    def executescript(self, sql_script):
        self.connection._execute_script(self._cursor, sql_script)
        return self


@contextmanager
def _nested_write(connection):
    _check_nested_schema(connection)
    if not connection.in_transaction:
        # A read-first outer fixture is still the transaction owner. Without
        # BEGIN, releasing an outermost savepoint would commit the inner write.
        connection.execute('BEGIN')
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
