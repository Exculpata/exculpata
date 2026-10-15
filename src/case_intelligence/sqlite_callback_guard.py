"""Keep Python SQLite callbacks from deadlocking the interpreter.

CPython releases the GIL around ``sqlite3_step``. A user-defined function,
authorizer, trace, or progress callback then reacquires the GIL while that
connection's SQLite mutex is still held. ``sqlite3_bind_*`` does the opposite:
it keeps the GIL and takes the same connection mutex. One thread inside a
callback and another thread binding a parameter on that connection wait on
each other forever. ``busy_timeout`` does not apply. A second connection has
its own mutex and is not part of this deadlock.

Each callback-safe connection takes its own reentrant lock before every
pysqlite call. A waiter blocks on that lock and drops the GIL, so the callback
can finish and release the SQLite mutex. The lock is reentrant because a
callback may use SQLite on the same thread, including a different connection.
It is not a transaction lock: multi-statement work still needs the store lock
that already guards that connection.
"""
from __future__ import annotations

import sqlite3
import threading


_ORIGINAL_CONNECT = sqlite3.connect


def _guard(lock_name: str, method):
    def call(self, *args, **kwargs):
        with getattr(self, lock_name):
            return method(self, *args, **kwargs)

    call.__name__ = method.__name__
    call.__doc__ = method.__doc__
    return call


class CallbackSafeCursor(sqlite3.Cursor):
    """Cursor whose SQLite calls hold its connection's entry lock."""

    execute = _guard("_entry", sqlite3.Cursor.execute)
    executemany = _guard("_entry", sqlite3.Cursor.executemany)
    executescript = _guard("_entry", sqlite3.Cursor.executescript)
    fetchone = _guard("_entry", sqlite3.Cursor.fetchone)
    fetchmany = _guard("_entry", sqlite3.Cursor.fetchmany)
    fetchall = _guard("_entry", sqlite3.Cursor.fetchall)
    close = _guard("_entry", sqlite3.Cursor.close)

    @property
    def _entry(self):
        return self.connection._entry

    def __next__(self):
        with self._entry:
            return sqlite3.Cursor.__next__(self)


class _GuardedBlob:
    """Proxy for the unsubclassable ``sqlite3.Blob`` type."""

    __slots__ = ("_blob", "_entry")

    def __init__(self, blob: sqlite3.Blob, entry: threading.RLock) -> None:
        self._blob = blob
        self._entry = entry

    def __enter__(self) -> _GuardedBlob:
        return self

    def __exit__(self, exc_type, exc, traceback) -> object:
        with self._entry:
            return self._blob.__exit__(exc_type, exc, traceback)

    def close(self) -> None:
        with self._entry:
            self._blob.close()

    def read(self, *args, **kwargs):
        with self._entry:
            return self._blob.read(*args, **kwargs)

    def write(self, *args, **kwargs):
        with self._entry:
            return self._blob.write(*args, **kwargs)

    def seek(self, *args, **kwargs):
        with self._entry:
            return self._blob.seek(*args, **kwargs)

    def tell(self):
        with self._entry:
            return self._blob.tell()

    def __len__(self) -> int:
        with self._entry:
            return self._blob.__len__()

    def __getitem__(self, item):
        with self._entry:
            return self._blob.__getitem__(item)

    def __setitem__(self, item, value) -> None:
        with self._entry:
            self._blob.__setitem__(item, value)


class CallbackSafeConnection(sqlite3.Connection):
    """Connection whose SQLite calls hold that connection's entry lock.

    ``interrupt`` stays unlocked. It is the escape hatch another thread uses
    while this connection is already inside SQLite; taking the entry lock
    there would wait for the operation it is trying to cancel.
    """

    close = _guard("_entry", sqlite3.Connection.close)
    commit = _guard("_entry", sqlite3.Connection.commit)
    rollback = _guard("_entry", sqlite3.Connection.rollback)
    create_function = _guard("_entry", sqlite3.Connection.create_function)
    create_aggregate = _guard("_entry", sqlite3.Connection.create_aggregate)
    create_window_function = _guard("_entry", sqlite3.Connection.create_window_function)
    create_collation = _guard("_entry", sqlite3.Connection.create_collation)
    enable_load_extension = _guard("_entry", sqlite3.Connection.enable_load_extension)
    load_extension = _guard("_entry", sqlite3.Connection.load_extension)
    set_authorizer = _guard("_entry", sqlite3.Connection.set_authorizer)
    set_progress_handler = _guard("_entry", sqlite3.Connection.set_progress_handler)
    set_trace_callback = _guard("_entry", sqlite3.Connection.set_trace_callback)
    backup = _guard("_entry", sqlite3.Connection.backup)
    serialize = _guard("_entry", sqlite3.Connection.serialize)
    deserialize = _guard("_entry", sqlite3.Connection.deserialize)
    setlimit = _guard("_entry", sqlite3.Connection.setlimit)
    getlimit = _guard("_entry", sqlite3.Connection.getlimit)
    setconfig = _guard("_entry", sqlite3.Connection.setconfig)
    getconfig = _guard("_entry", sqlite3.Connection.getconfig)
    __enter__ = _guard("_entry", sqlite3.Connection.__enter__)
    __exit__ = _guard("_entry", sqlite3.Connection.__exit__)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._entry = threading.RLock()

    def cursor(self, factory=None):
        if factory is None:
            factory = CallbackSafeCursor
        with self._entry:
            return sqlite3.Connection.cursor(self, factory)

    def execute(self, *args, **kwargs):
        with self._entry:
            cursor = sqlite3.Connection.cursor(self, CallbackSafeCursor)
            return sqlite3.Cursor.execute(cursor, *args, **kwargs)

    def executemany(self, *args, **kwargs):
        with self._entry:
            cursor = sqlite3.Connection.cursor(self, CallbackSafeCursor)
            return sqlite3.Cursor.executemany(cursor, *args, **kwargs)

    def executescript(self, *args, **kwargs):
        with self._entry:
            cursor = sqlite3.Connection.cursor(self, CallbackSafeCursor)
            return sqlite3.Cursor.executescript(cursor, *args, **kwargs)

    def blobopen(self, *args, **kwargs) -> _GuardedBlob:
        with self._entry:
            return _GuardedBlob(
                sqlite3.Connection.blobopen(self, *args, **kwargs), self._entry
            )

    @property
    def in_transaction(self) -> bool:
        with self._entry:
            return sqlite3.Connection.in_transaction.__get__(self)

    @property
    def total_changes(self) -> int:
        with self._entry:
            return sqlite3.Connection.total_changes.__get__(self)

    @property
    def isolation_level(self):
        with self._entry:
            return sqlite3.Connection.isolation_level.__get__(self)

    @isolation_level.setter
    def isolation_level(self, value) -> None:
        with self._entry:
            sqlite3.Connection.isolation_level.__set__(self, value)

    @property
    def autocommit(self):
        with self._entry:
            return sqlite3.Connection.autocommit.__get__(self)

    @autocommit.setter
    def autocommit(self, value) -> None:
        with self._entry:
            sqlite3.Connection.autocommit.__set__(self, value)


def _guarded_connect(*args, **kwargs):
    # ``factory`` is the sixth positional parameter. Replace only the default
    # connection type; a caller-supplied factory is already a deliberate subclass.
    if "factory" in kwargs:
        if kwargs["factory"] is sqlite3.Connection:
            kwargs["factory"] = CallbackSafeConnection
        return _ORIGINAL_CONNECT(*args, **kwargs)
    if len(args) >= 6:
        if args[5] is sqlite3.Connection:
            args = (*args[:5], CallbackSafeConnection, *args[6:])
        return _ORIGINAL_CONNECT(*args, **kwargs)
    return _ORIGINAL_CONNECT(*args, factory=CallbackSafeConnection, **kwargs)


def install_sqlite_callback_guard() -> None:
    """Route later ``sqlite3.connect`` calls through callback-safe connections."""
    if sqlite3.connect is not _guarded_connect:
        sqlite3.connect = _guarded_connect
