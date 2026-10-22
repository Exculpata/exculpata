"""fromkeys must retain evaluated key ownership as well as its default."""
import sqlite3
from types import SimpleNamespace

import pytest

from tests.fixture_write_policy import check_fixture_writes


REASON = '# fixture-write: Isolated synthetic construction deliberately retains a raw shared key.'


@pytest.mark.parametrize('source', [
    'holder = dict.fromkeys([bench.workspace.connection])\nfor handle in holder:\n    handle.close()',
    'holder = dict.fromkeys((bench.workspace.connection,))',
    "from builtins import dict as mapping\nholder = mapping.fromkeys([bench.workspace.connection])",
    "holder = {}.fromkeys([bench.workspace.connection])",
    'keys = [bench.workspace.connection]\nholder = dict.fromkeys(keys, (keys := [])) ' + REASON + '\nfor handle in holder:\n    handle.close()',
    'holder = dict.fromkeys([bench.workspace.connection]) ' + REASON + '\nfor handle in holder:\n    handle.close()',
    'paths = dict.fromkeys([bench.workspace.path])\nfor path in paths:\n    sqlite3.connect(path)',
])
def test_fromkeys_shared_keys_and_active_paths_retain_provenance(source):
    assert check_fixture_writes('import sqlite3\n' + source)


@pytest.mark.parametrize('source', [
    "holder = {'shared': bench.workspace.connection}\nlabels = dict.fromkeys(holder)",
    "holder = {'db': bench.workspace.path}\nlabels = dict.fromkeys(holder)",
    'holder = dict.fromkeys([independent])\nfor handle in holder:\n    handle.close()',
    'holder = dict.fromkeys([bench.workspace.connection]) ' + REASON,
])
def test_literal_mapping_keys_independent_keys_and_exact_construction_exception(source):
    prefix = "import sqlite3\nindependent = sqlite3.connect(':memory:')\n"
    if 'bench.workspace.connection' in source:
        assert any('unsupported' in v.rule for v in check_fixture_writes(prefix + source))
        assert check_fixture_writes(prefix + source.replace('bench.workspace.connection', 'independent')) == []
    else:
        assert check_fixture_writes(prefix + source) == []


def test_fromkeys_iteration_closes_real_shared_connection_before_rejection():
    raw = sqlite3.connect(':memory:')
    bench = SimpleNamespace(workspace=SimpleNamespace(connection=raw))
    source = 'holder = dict.fromkeys([bench.workspace.connection])\nfor handle in holder:\n    handle.close()'
    try:
        exec(source, {'bench': bench})
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            raw.execute('SELECT 1')
        assert check_fixture_writes(source)
    finally:
        raw.close()
