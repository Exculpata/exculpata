"""Opaque dictionary keys must retain known shared ownership."""
import sqlite3
from types import SimpleNamespace

import pytest

from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize('source', [
    'holder = {bench.workspace.connection: 0}\nfor handle in holder:\n    handle.close()',
    'handle = bench.workspace.connection\nholder = {handle: 0}\nfor item in holder:\n    item.close()',
    'for handle in {bench.workspace.connection: 0}:\n    handle.close()',
    '[handle.close() for handle in {bench.workspace.connection: 0}]',
    'holder = {(bench.workspace.connection,): 0}\nfor handles in holder:\n    for handle in handles:\n        handle.close()',
    'holder = {bench.workspace.connection.close: 0}\nfor cleanup in holder:\n    cleanup()',
    'holder = {**options, bench.workspace.connection: 0}\nfor handle in holder:\n    handle.close()',
])
def test_shared_dictionary_keys_are_explicitly_unsupported(source):
    assert any('heterogeneous' in v.rule for v in check_fixture_writes(source))


@pytest.mark.parametrize('source', [
    'holder = {independent: 0}\nfor handle in holder:\n    handle.close()',
    'for handle in {independent: 0}:\n    handle.close()',
    'holder = {(independent,): 0}',
    'holder = {key: 0}',
    "holder = {'handle': independent}",
])
def test_independent_and_ordinary_dictionary_keys_remain_allowed(source):
    assert check_fixture_writes("import sqlite3\nindependent = sqlite3.connect(':memory:')\n" + source) == []


def test_dictionary_key_iteration_closes_real_shared_connection_before_rejection():
    raw = sqlite3.connect(':memory:')
    bench = SimpleNamespace(workspace=SimpleNamespace(connection=raw))
    source = '''holder = {bench.workspace.connection: 0}
for handle in holder:
    handle.close()
'''
    try:
        exec(source, {'bench': bench})
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            raw.execute('SELECT 1')
        assert check_fixture_writes(source)
    finally:
        raw.close()
