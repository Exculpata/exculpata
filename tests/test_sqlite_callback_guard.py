"""Python SQLite callbacks must not deadlock the GIL against parameter binding."""
from __future__ import annotations

import faulthandler
import sqlite3
import threading
import time

import case_intelligence.sqlite_callback_guard as guard


def test_connections_opened_after_import_are_callback_safe():
    connection = sqlite3.connect(":memory:", check_same_thread=False, isolation_level="IMMEDIATE")
    try:
        assert isinstance(connection, guard.CallbackSafeConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("create table item(value text)")
        with connection:
            connection.execute("insert into item(value) values (?)", ("synthetic",))
            assert connection.in_transaction
        assert connection.total_changes == 1
        assert [row["value"] for row in connection.execute("select value from item")] == ["synthetic"]
        target = sqlite3.connect(":memory:")
        try:
            connection.backup(target)
            assert target.execute("select value from item").fetchone()[0] == "synthetic"
        finally:
            target.close()
        assert "CREATE TABLE" in "\n".join(connection.iterdump())
    finally:
        connection.close()


def test_trace_callback_may_reenter_sqlite_on_the_same_thread():
    writer = sqlite3.connect(":memory:", check_same_thread=False)
    reader = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        writer.execute("create table item(value text)")
        seen: list[int] = []

        def trace(statement: str) -> None:
            if statement.lstrip().upper().startswith("INSERT"):
                seen.append(reader.execute("select 1").fetchone()[0])

        writer.set_trace_callback(trace)
        writer.execute("insert into item(value) values (?)", ("synthetic",))
        writer.set_trace_callback(None)
        assert seen == [1]
    finally:
        writer.close()
        reader.close()


def test_waiting_callback_does_not_block_another_connection():
    left = sqlite3.connect(":memory:", check_same_thread=False)
    right = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        left.execute("create table item(value text)")
        right.execute("create table item(value text)")
        entered = threading.Event()
        finished = threading.Event()

        def trace(statement: str) -> None:
            if "INSERT" in statement.upper() and not entered.is_set():
                entered.set()
                assert finished.wait(5), "the other connection waited for this callback"

        left.set_trace_callback(trace)
        errors: list[BaseException] = []

        def insert(connection: sqlite3.Connection, value: str, done: threading.Event | None = None) -> None:
            try:
                connection.execute("insert into item(value) values (?)", (value,))
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                if done is not None:
                    done.set()

        paused = threading.Thread(target=insert, args=(left, "synthetic-left"))
        other = threading.Thread(target=insert, args=(right, "synthetic-right", finished))
        paused.start()
        assert entered.wait(5)
        other.start()
        paused.join(5)
        other.join(5)
        assert errors == []
        assert not paused.is_alive()
        assert not other.is_alive()
        assert left.execute("select value from item").fetchone()[0] == "synthetic-left"
        assert right.execute("select value from item").fetchone()[0] == "synthetic-right"
    finally:
        left.set_trace_callback(None)
        left.close()
        right.close()


def test_callback_and_parameter_bind_on_one_connection_cannot_deadlock():
    # Without the entry lock this freezes the process: the callback holds the
    # connection mutex and wants the GIL, while the binder holds the GIL and
    # wants that mutex. Exit so a regression cannot wait out the suite.
    faulthandler.dump_traceback_later(5, exit=True)
    try:
        connection = sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None
        )
        try:
            connection.execute("create table item(value text)")
            started = threading.Event()

            def hold(_value: str) -> int:
                started.set()
                time.sleep(0.2)
                return 1

            connection.create_function("hold", 1, hold)

            def reader() -> None:
                connection.execute("select hold(?)", ("synthetic",))

            worker = threading.Thread(target=reader)
            worker.start()
            assert started.wait(5)
            connection.execute("insert into item(value) values (?)", ("synthetic",))
            worker.join(5)
            assert not worker.is_alive()
            assert connection.execute("select value from item").fetchone()[0] == "synthetic"
        finally:
            connection.close()
    finally:
        faulthandler.cancel_dump_traceback_later()
