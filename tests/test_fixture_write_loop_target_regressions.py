"""Literal iteration and assignment leaves retain known SQLite ownership."""
from tests.fixture_probe import fixture_probe
import sqlite3
from threading import Event, RLock, Thread
from types import SimpleNamespace

import pytest

from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize('container', [
    '[bench.workspace.connection]', '(bench.workspace.connection,)',
    '[independent, bench.workspace.connection]',
    '[bench.workspace.connection, independent]',
])
@pytest.mark.parametrize('style', ['direct', 'alias', 'helper', 'returned'])
def test_literal_loop_connection_close_keeps_shared_ownership(container, style):
    prefix = "import sqlite3\nindependent = sqlite3.connect(':memory:')\n"
    if style == 'direct':
        source = f'for handle in {container}:\n    handle.close()'
    elif style == 'alias':
        source = f'handles = {container}\nfor handle in handles:\n    handle.close()'
    elif style == 'helper':
        source = f'def dispose(handles):\n    for handle in handles:\n        handle.close()\ndispose({container})'
    else:
        source = f'def handles():\n    return {container}\nfor handle in handles():\n    handle.close()'
    assert any('close' in v.rule for v in check_fixture_writes(prefix + source))


@pytest.mark.parametrize('source', [
    'for handle, label in [(bench.workspace.connection, "synthetic")]:\n    handle.close()',
    'for label, *handles in [("synthetic", bench.workspace.connection)]:\n    for handle in handles:\n        handle.close()',
    'for operation in [bench.workspace.connection.close]:\n    operation()',
    'for store in [bench.workspace]:\n    with fixture_write(store):\n        store.append_audit_event()',
    'for group in [[bench.workspace.connection]]:\n    for handle in group:\n        handle.close()',
])
def test_nested_literal_loop_targets_and_bound_methods_keep_ownership(source):
    assert check_fixture_writes(source)


@pytest.mark.parametrize('attribute,value', [
    ('isolation_level', 'None'), ('autocommit', 'True'),
    ('row_factory', 'None'), ('text_factory', 'str'),
])
@pytest.mark.parametrize('target,rhs', [
    ('(TARGET, marker)', '(VALUE, 1)'),
    ('[marker, TARGET]', '[1, VALUE]'),
    ('(marker, (TARGET, other))', '(1, (VALUE, 2))'),
    ('(*TARGET, marker)', '(VALUE, 1)'),
])
def test_unpacking_checks_every_sqlite_descriptor_leaf(attribute, value, target, rhs):
    source = f'{target.replace("TARGET", "bench.workspace.connection." + attribute)} = {rhs.replace("VALUE", value)}'
    assert check_fixture_writes(source)


@pytest.mark.parametrize('source', [
    'for bench.workspace.connection.isolation_level in [None]:\n    pass',
    'for (bench.workspace.connection.row_factory, marker) in [(None, 1)]:\n    pass',
    '[marker for (bench.workspace.connection.text_factory, marker) in [(str, 1)]]',
    '(marker for (bench.workspace.connection.isolation_level, marker) in [(None, 1)])',
])
def test_iteration_attribute_targets_apply_the_same_setter_checks(source):
    assert check_fixture_writes(source)


@pytest.mark.parametrize('source', [
    'for handle in [independent]:\n    handle.close()',
    'for handle in (independent, independent):\n    handle.close()',
    'for handle in resources:\n    handle.close()',
    '(independent.isolation_level, marker) = (None, 1)',
    '[independent.row_factory, marker] = [None, 1]',
    'for independent.text_factory in [str]:\n    pass',
    'with fixture_write(bench.workspace) as handle:\n    (handle.row_factory, marker) = (None, 1)',
    'with bench.workspace._lock:\n    (bench.workspace.connection.text_factory, marker) = (str, 1)',
])
def test_iteration_and_unpacking_preserve_independent_and_owned_controls(source):
    assert check_fixture_writes("import sqlite3\nindependent = sqlite3.connect(':memory:')\n" + source) == []


def test_literal_cleanup_helper_closes_actual_shared_connection_before_inventory_rejects():
    raw = sqlite3.connect(':memory:')
    bench = SimpleNamespace(workspace=SimpleNamespace(connection=raw))
    source = '''def dispose(handles):
    for handle in handles:
        handle.close()
dispose([bench.workspace.connection])
'''
    try:
        error, _ = fixture_probe(source, bench=bench)
        if error is not None:
            raise error
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            raw.execute('SELECT 1')
        assert check_fixture_writes(source)
    finally:
        raw.close()


def test_unpacking_descriptor_commits_actual_worker_transaction_without_lock(tmp_path):
    path = tmp_path / 'synthetic-unpacked-setter.sqlite'
    raw = sqlite3.connect(path, check_same_thread=False)
    raw.execute('CREATE TABLE synthetic_fixture(value TEXT)')
    bench = SimpleNamespace(workspace=SimpleNamespace(connection=raw, _lock=RLock()))
    ready, resume = Event(), Event()
    observations, errors = [], []

    def worker():
        try:
            with bench.workspace._lock:
                # fixture-write: Deliberately seed worker-owned pending data for the unpacked setter negative regression.
                raw.execute("INSERT INTO synthetic_fixture VALUES ('worker pending')")
                ready.set()
                assert resume.wait(5)
                observations.append(raw.in_transaction)
        except BaseException as error:
            errors.append(error)
            ready.set()

    worker_thread = Thread(target=worker)
    worker_thread.start()
    source = '(bench.workspace.connection.isolation_level, marker) = (None, 1)'
    try:
        assert ready.wait(5)
        assert not errors
        error, _ = fixture_probe(source, bench=bench)
        if error is not None:
            raise error
        with sqlite3.connect(path) as independent:
            assert independent.execute('SELECT value FROM synthetic_fixture').fetchall() == [('worker pending',)]
        resume.set()
        worker_thread.join(5)
        assert not worker_thread.is_alive()
        assert not errors and observations == [False]
        assert check_fixture_writes(source)
    finally:
        resume.set()
        worker_thread.join(5)
        raw.close()
