"""Dictionary ownership boundaries preserve evaluation order and scalar controls."""
import sqlite3
from types import SimpleNamespace

import pytest

from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize('source', [
    'handle = bench.workspace.connection\nholder = {handle: (handle := independent)}\nfor item in holder:\n    item.close()',
    'handle = bench.workspace.connection\nholder = {(handle,): (handle := independent)}',
    'handle = bench.workspace.connection\nholder = {handle: 0, (handle := independent): 1}',
    'for handle in {bench.workspace.connection: 0}.keys():\n    handle.close()',
    'for handle, value in {bench.workspace.connection: 0}.items():\n    handle.close()',
    'keys = {bench.workspace.connection: 0}.keys\nfor handle in keys():\n    handle.close()',
    "for handle in {key: bench.workspace.connection}.values():\n    handle.close()",
    'holder = {handle: 0 for handle in [bench.workspace.connection]}\nfor item in holder:\n    item.close()',
    'holder = {(handle,): 0 for handle in [bench.workspace.connection]}',
    'holder = {handle: (handle := independent) for item in [0]}',
    'holder = {0: handle for handle in [bench.workspace.connection]}',
])
def test_unsupported_dictionary_ownership_surfaces_reject(source):
    prefix = "import sqlite3\nindependent = sqlite3.connect(':memory:')\nhandle = bench.workspace.connection\n"
    assert check_fixture_writes(prefix + source)


@pytest.mark.parametrize('key', [
    'str(bench.workspace.connection)',
    'os.fspath(bench.workspace.path)',
    'str(bench.workspace.path)',
    'pathlib.Path(bench.workspace.path)',
    'bench.workspace.path',
    'bench.workspace.path.as_posix()',
])
def test_scalar_or_path_keys_do_not_carry_reachable_connection_ownership(key):
    assert check_fixture_writes('import os\nimport pathlib\nlabels = {' + key + ": 'expected'}") == []


@pytest.mark.parametrize('source', [
    'holder = {independent: 0}\nfor handle in holder.keys():\n    handle.close()',
    'holder = {handle: 0 for handle in [independent]}',
    'holder = {str(handle): 0 for handle in [bench.workspace.connection]}',
    'holder = {independent: (handle := bench.workspace.connection)}',
])
def test_dictionary_controls_preserve_the_supported_distinction(source):
    prefix = "import sqlite3\nindependent = sqlite3.connect(':memory:')\n"
    # A shared value remains explicitly unsupported even when its key is independent.
    if 'handle :=' in source or '[bench.workspace.connection]' in source:
        assert check_fixture_writes(prefix + source)
        if '[bench.workspace.connection]' in source:
            assert check_fixture_writes(prefix + source.replace('[bench.workspace.connection]', '(bench.workspace.connection,)')) == []
    else:
        assert check_fixture_writes(prefix + source) == []


@pytest.mark.parametrize('source', [
    'handle = bench.workspace.connection\nholder = {handle: (handle := independent)}\nfor item in holder:\n    item.close()',
    'for handle in {bench.workspace.connection: 0}.keys():\n    handle.close()',
    'for handle, value in {bench.workspace.connection: 0}.items():\n    handle.close()',
    'holder = {handle: 0 for handle in [bench.workspace.connection]}\nfor item in holder:\n    item.close()',
])
def test_dictionary_paths_close_real_shared_connection_before_rejection(source):
    raw = sqlite3.connect(':memory:')
    independent = sqlite3.connect(':memory:')
    bench = SimpleNamespace(workspace=SimpleNamespace(connection=raw))
    try:
        exec(source, {'bench': bench, 'independent': independent})
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            raw.execute('SELECT 1')
        prefix = "import sqlite3\nindependent = sqlite3.connect(':memory:')\n"
        assert check_fixture_writes(prefix + source)
    finally:
        raw.close()
        independent.close()
