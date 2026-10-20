"""Bounded container flows cannot discard known fixture provenance."""
import sqlite3
from types import SimpleNamespace

import pytest

from tests.fixture_write_policy import check_fixture_writes


REASON = '# fixture-write: Isolated synthetic construction deliberately retains raw shared ownership.'
PREFIX = "import sqlite3\nindependent = sqlite3.connect(':memory:')\nholder = {'shared': bench.workspace.connection}\nsafe = {}\n"


@pytest.mark.parametrize('source', [
    "dict.get(safe, 'missing', bench.workspace.connection).close()",
    "dict.pop(safe, 'missing', bench.workspace.connection).close()",
    "dict.setdefault(safe, 'missing', bench.workspace.connection).close()",
    "safe.get('missing', bench.workspace.connection).close()",
    "safe.pop('missing', bench.workspace.connection).close()",
    "safe.setdefault('missing', bench.workspace.connection).close()",
    "dict.get(*[holder, 'shared']).close()",
    "dict.get(*(holder, 'shared')).close()",
    "accessor = dict.get\naccessor(*[holder, 'shared']).close()",
    'paths = {"db": bench.workspace.path}\ngroups = [*paths.values()] ' + REASON + '\nfor path in groups:\n    sqlite3.connect(path)',
    'paths = {"db": bench.workspace.path}\ngroups = (*paths.values(),) ' + REASON + '\nfor path in groups:\n    sqlite3.connect(path)',
    "holder = dict(shared=bench.workspace.connection)\nholder.get('shared').close()",
    "holder = dict({'shared': bench.workspace.connection})\nholder.get('shared').close()",
    "holder = dict([('shared', bench.workspace.connection)])\nholder.get('shared').close()",
    "accessor = [dict.get][0]\naccessor(holder, 'shared').close()",
    "accessor = {'get': dict.get}['get']\naccessor(holder, 'shared').close()",
    'holder = {bench.workspace.connection}\nfor handle in holder:\n    handle.close()',
    'holder = [handle for handle in [bench.workspace.connection]]\nfor handle in holder:\n    handle.close()',
    'holder = {handle for handle in [bench.workspace.connection]}\nfor handle in holder:\n    handle.close()',
    'holder = [handle for handle in [bench.workspace.connection]] ' + REASON + '\nfor handle in holder:\n    handle.close()',
    "holder = {}\nholder.update({'shared': bench.workspace.connection})\nholder['shared'].close()",
    "holder = {}\ndict.update(holder, {'shared': bench.workspace.connection})\nholder['shared'].close()",
    "holder = {}\nalias = holder\nholder.update({'shared': bench.workspace.connection}) " + REASON + "\nalias['shared'].close()",
    "holder = {}\nholder['shared'] = bench.workspace.connection\nholder['shared'].close()",
    "holder = {}\nalias = holder\nholder['shared'] = bench.workspace.connection " + REASON + "\nalias['shared'].close()",
    'holder = [independent]\nholder[0] = bench.workspace.connection\nholder[0].close()',
])
def test_known_container_flows_retain_shared_or_active_path_provenance(source):
    assert check_fixture_writes(PREFIX + source)


@pytest.mark.parametrize('source', [
    "dict.get(safe, 'missing', independent).close()",
    "safe.get('missing', independent).close()",
    "accessor = [dict.get][0]\naccessor({'item': independent}, 'item').close()",
    "labels = dict({'shared': bench.workspace.connection})\nfor key in labels.keys():\n    assert isinstance(key, str)",
    "holder = dict(item=independent)\nholder.get('item').close()",
    'holder = {independent}\nfor handle in holder:\n    handle.close()',
    'holder = [handle for handle in [independent]]\nfor handle in holder:\n    handle.close()',
    "holder = {}\nresult = holder.update({'item': independent})\nassert result is None",
    "holder = {}\nholder['item'] = independent\nholder['item'].close()",
])
def test_independent_resources_safe_keys_and_none_results_remain_controls(source):
    assert any(v.line == 3 and 'mutable shared fixture containers' in v.rule
               for v in check_fixture_writes(PREFIX + source))
    independent_source = (PREFIX + source).replace('bench.workspace.connection', 'independent')
    assert check_fixture_writes(independent_source) == []


def test_shared_set_unpacking_does_not_assume_iteration_order():
    assert check_fixture_writes(PREFIX + 'first, second = {independent, bench.workspace.connection}\nfirst.close()')


@pytest.mark.parametrize('source', [
    "safe = {}\ndict.get(safe, 'missing', bench.workspace.connection).close()",
    "holder = {'shared': bench.workspace.connection}\ndict.get(*[holder, 'shared']).close()",
    "holder = dict(shared=bench.workspace.connection)\nholder.get('shared').close()",
    "holder = {'shared': bench.workspace.connection}\naccessor = [dict.get][0]\naccessor(holder, 'shared').close()",
    'holder = {bench.workspace.connection}\nfor handle in holder:\n    handle.close()',
    'holder = [handle for handle in [bench.workspace.connection]]\nfor handle in holder:\n    handle.close()',
    "holder = {}\nholder.update({'shared': bench.workspace.connection})\nholder['shared'].close()",
    "holder = {}\nholder['shared'] = bench.workspace.connection\nholder['shared'].close()",
])
def test_actual_container_flows_close_shared_connection_before_rejection(source):
    raw = sqlite3.connect(':memory:')
    bench = SimpleNamespace(workspace=SimpleNamespace(connection=raw))
    try:
        exec(source, {'bench': bench})
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            raw.execute('SELECT 1')
        assert check_fixture_writes(source)
    finally:
        raw.close()
