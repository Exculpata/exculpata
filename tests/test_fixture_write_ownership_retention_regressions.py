"""Ownership survives supported captures and remains local to exact exceptions."""
from tests.fixture_probe import fixture_probe
import sqlite3
from types import SimpleNamespace

import pytest

from tests.fixture_write_policy import check_fixture_writes


PREFIX = "import sqlite3\nindependent = sqlite3.connect(':memory:')\nhandle = bench.workspace.connection\n"
REASON = '# fixture-write: Isolated synthetic construction deliberately retains a raw shared handle.'


@pytest.mark.parametrize('source', [
    'holder = {(handle, (handle := independent)): 0}\nfor pair in holder:\n    pair[0].close()',
    'holder = {((handle,), (handle := independent)): 0}',
    'holder = {(handle, (handle := independent)): 0} ' + REASON + '\nfor pair in holder:\n    pair[0].close()',
    'holder = {item: 0 for item in [handle]} ' + REASON + '\nfor item in holder:\n    item.close()',
    'holder = {0: item for item in [handle]} ' + REASON + '\nconsume(holder)',
    'holder = {item: 0 for item in [handle]} ' + REASON + '\nalias = holder',
    "holder = {'shared': handle}\nfor item in holder.values():\n    item.close()",
    "holder = {'shared': handle}\nfor key, item in holder.items():\n    item.close()",
    "holder = {'shared': handle}\nholder.get('shared').close()",
    "holder = {'shared': handle}\ngetter = holder.get\ngetter('shared').close()",
    "holder = {'shared': handle}\nfor item in getattr(holder, 'values')():\n    item.close()",
])
def test_retained_container_ownership_rejects_later_unannotated_consumption(source):
    assert check_fixture_writes(PREFIX + source)


@pytest.mark.parametrize('binding', [
    'if condition:\n    str = lambda value: value',
    'convert = str\nif condition:\n    convert = lambda value: value',
    'from os import fspath\nif condition:\n    fspath = lambda value: value',
])
def test_ambiguous_scalar_converters_do_not_erase_shared_handles(binding):
    name = 'convert' if 'convert =' in binding else 'fspath' if 'import fspath' in binding else 'str'
    assert check_fixture_writes(PREFIX + binding + f'\nresult = {name}(handle)\nresult.close()')


@pytest.mark.parametrize('path', ['bench.workspace.path', 'str(bench.workspace.path)', 'os.fspath(bench.workspace.path)'])
def test_active_workspace_paths_specialize_fixed_parameter_helpers(path):
    source = f"import sqlite3\nimport os\ndef open_db(path):\n    return sqlite3.connect(path)\nconnection = open_db({path})\nconnection.execute('SELECT 1')"
    assert check_fixture_writes(source)


@pytest.mark.parametrize('source', [
    "holder = {'shared': handle}\nfor key in holder:\n    assert isinstance(key, str)",
    "holder = {'shared': handle}\nfor key in holder.keys():\n    assert isinstance(key, str)",
    'labels = {str(handle): 0}',
    'convert = str\nlabels = {convert(handle): 0}',
    'from builtins import str as text\nlabels = {text(handle): 0}',
    'import os\nlabels = {os.fspath(bench.workspace.path): 0}',
    'holder = {item: 0 for item in [independent]}\nfor item in holder:\n    item.close()',
    "def open_db(path):\n    return sqlite3.connect(path)\nconnection = open_db(':memory:')\nconnection.execute('SELECT 1')",
    'holder = {item: 0 for item in [handle]} ' + REASON,
])
def test_nonowning_keys_independent_handles_and_exact_construction_exception(source):
    if "holder = {'shared': handle}" in source or 'for item in [handle]' in source:
        assert any('mutable shared fixture containers' in v.rule for v in check_fixture_writes(PREFIX + source))
        assert check_fixture_writes(PREFIX.replace('handle = bench.workspace.connection', 'handle = independent') + source) == []
    elif 'os.fspath(bench.workspace.path)' in source:
        assert any('opaque dictionary paths' in v.rule for v in check_fixture_writes(PREFIX + source))
        assert check_fixture_writes(PREFIX + source.replace('bench.workspace.path', '"synthetic.sqlite"')) == []
    else:
        assert check_fixture_writes(PREFIX + source) == []


@pytest.mark.parametrize('source', [
    'handle = bench.workspace.connection\nholder = {(handle, (handle := independent)): 0}\nfor pair in holder:\n    pair[0].close()',
    'holder = {item: 0 for item in [bench.workspace.connection]} ' + REASON + '\nfor item in holder:\n    item.close()',
    "holder = {'shared': bench.workspace.connection}\nfor item in holder.values():\n    item.close()",
    'if condition:\n    str = lambda value: value\nhandle = str(bench.workspace.connection)\nhandle.close()',
])
def test_real_shared_closure_precedes_inventory_rejection(source):
    raw = sqlite3.connect(':memory:')
    independent = sqlite3.connect(':memory:')
    bench = SimpleNamespace(workspace=SimpleNamespace(connection=raw))
    try:
        error, _ = fixture_probe(source, bench=bench, independent=independent, condition=True)
        if error is not None:
            raise error
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            raw.execute('SELECT 1')
        assert check_fixture_writes(PREFIX + source)
    finally:
        raw.close()
        independent.close()
