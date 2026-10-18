"""Bound and unbound accessors must retain unsupported fixture provenance."""
import sqlite3
from types import SimpleNamespace

import pytest

from tests.fixture_write_policy import check_fixture_writes


REASON = '# fixture-write: Isolated synthetic extraction deliberately retains a raw shared handle.'
SHARED = "holder = {'shared': bench.workspace.connection}\n"


@pytest.mark.parametrize('binding', [
    'convert = str if condition else text',
    'if condition:\n    convert = str\nelse:\n    convert = text',
    'convert = str or text',
])
def test_all_proven_scalar_converter_branches_remain_nonowning(binding):
    assert check_fixture_writes('from builtins import str as text\n' + binding + '\nlabels = {convert(bench.workspace.connection): 0}') == []


@pytest.mark.parametrize('source', [
    "handle = holder.get('shared') " + REASON + '\nhandle.close()',
    "getter = holder.get " + REASON + "\nhandle = getter('shared') " + REASON + '\nhandle.close()',
    'handles = holder.values() ' + REASON + '\nfor handle in handles:\n    handle.close()',
    'handles = holder.items() ' + REASON + '\nfor key, handle in handles:\n    handle.close()',
    'for handle in dict.values(holder):\n    handle.close()',
    'for key, handle in dict.items(holder):\n    handle.close()',
    "dict.__getitem__(holder, 'shared').close()",
    'accessor = dict.values\nfor handle in accessor(holder):\n    handle.close()',
    'from builtins import dict as mapping\nfor handle in mapping.values(holder):\n    handle.close()',
    "handle = dict.get(holder, 'shared') " + REASON + '\nhandle.close()',
])
def test_accessor_results_and_explicit_receivers_retain_shared_ownership(source):
    assert check_fixture_writes(SHARED + source)


@pytest.mark.parametrize('iteration, target', [
    ('paths.values()', 'path'),
    ('paths.items()', 'key, path'),
    ('dict.values(paths)', 'path'),
])
def test_active_paths_in_mapping_views_cannot_hide_indirect_opens(iteration, target):
    source = f"import sqlite3\ndef open_db(paths):\n    for {target} in {iteration}:\n        return sqlite3.connect(path)\nconnection = open_db({{'db': bench.workspace.path}})\nconnection.execute('SELECT 1')"
    assert check_fixture_writes(source)


@pytest.mark.parametrize('source', [
    'for key in holder.keys():\n    assert isinstance(key, str)',
    'for key in dict.keys(holder):\n    assert isinstance(key, str)',
    "paths = {'db': bench.workspace.path}\nfor key in paths.keys():\n    assert isinstance(key, str)",
    "paths = {'db': bench.workspace.path}\nfor key in dict.keys(paths):\n    assert isinstance(key, str)",
    "resources = {'item': independent}\nfor handle in dict.values(resources):\n    handle.close()",
    "handle = holder.get('shared') " + REASON,
])
def test_safe_keys_independent_resources_and_exact_extraction_exception(source):
    assert check_fixture_writes("import sqlite3\nindependent = sqlite3.connect(':memory:')\n" + SHARED + source) == []


@pytest.mark.parametrize('source', [
    "handle = holder.get('shared') " + REASON + '\nhandle.close()',
    'for handle in dict.values(holder):\n    handle.close()',
    "dict.__getitem__(holder, 'shared').close()",
])
def test_accessor_closes_real_shared_connection_before_rejection(source):
    raw = sqlite3.connect(':memory:')
    bench = SimpleNamespace(workspace=SimpleNamespace(connection=raw))
    source = SHARED + source
    try:
        exec(source, {'bench': bench})
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            raw.execute('SELECT 1')
        assert check_fixture_writes(source)
    finally:
        raw.close()
