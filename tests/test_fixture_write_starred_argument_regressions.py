"""Starred operands retain unresolved known shared ownership before calls."""
import sqlite3
from types import SimpleNamespace

import pytest

from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize('source', [
    'def dispose(handle):\n    handle.close()\ndispose(*(resources or [bench.workspace.connection]))',
    'def dispose(handle):\n    handle.close()\ncleanup = dispose\ncleanup(*(resources or [bench.workspace.connection]))',
    'def dispose(marker, handle):\n    handle.close()\ndispose(0, *(resources or [bench.workspace.connection]))',
    'def dispose(handle):\n    handle.close()\ndispose(*(resources or (bench.workspace.connection,)))',
    'consume(*(resources or [bench.workspace.connection]))',
])
def test_starred_boolean_shared_operands_reject(source):
    assert any('heterogeneous' in v.rule for v in check_fixture_writes(source))


@pytest.mark.parametrize('source', [
    'def dispose(handle):\n    handle.close()\ndispose(*(resources or [independent]))',
    'def dispose(handle):\n    handle.close()\ndispose(*[independent])',
    'consume(*(resources or [resource]))',
])
def test_starred_independent_and_generic_controls_remain_allowed(source):
    assert check_fixture_writes("import sqlite3\nindependent = sqlite3.connect(':memory:')\n" + source) == []


def test_keyword_boolean_shared_operand_keeps_existing_rejection():
    assert check_fixture_writes("def dispose(handle):\n    handle.close()\ndispose(**(options or {'handle': bench.workspace.connection}))")


@pytest.mark.parametrize('source', [
    'def dispose(handle):\n    handle.close()\ndispose(*(*resources, bench.workspace.connection))',
    'def dispose(handle):\n    handle.close()\ndispose(*[bench.workspace.connection, *resources])',
    "def dispose(handle):\n    handle.close()\ndispose(**{**options, 'handle': bench.workspace.connection})",
    "holder = {key: bench.workspace.connection}\nholder[key].close()",
])
def test_opaque_literal_expansions_do_not_erase_known_shared_members(source):
    assert check_fixture_writes(source)


def test_starred_boolean_helper_closes_real_shared_connection_before_rejection():
    raw = sqlite3.connect(':memory:')
    bench = SimpleNamespace(workspace=SimpleNamespace(connection=raw))
    source = '''def dispose(handle):
    handle.close()
dispose(*(resources or [bench.workspace.connection]))
'''
    try:
        exec(source, {'bench': bench, 'resources': []})
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            raw.execute('SELECT 1')
        assert check_fixture_writes(source)
    finally:
        raw.close()
