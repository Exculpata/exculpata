"""Finite dictionary-method semantics preserve snapshots and non-owning controls."""
import sqlite3
from types import SimpleNamespace

import pytest

from tests.fixture_write_policy import check_fixture_writes


REASON = '# fixture-write: Isolated synthetic operation deliberately exercises raw container extraction.'
PREFIX = "holder = {'shared': bench.workspace.connection}\nsafe = {'shared': independent}\n"


@pytest.mark.parametrize('source', [
    'labels = dict.fromkeys(holder)\nassert labels == {"shared": None}',
    'labels = holder.fromkeys(holder)\nassert labels == {"shared": None}',
    'from builtins import dict as mapping\nlabels = mapping.fromkeys(holder)',
    'result = holder.clear() ' + REASON + '\nassert result is None',
    'result = holder.update({}) ' + REASON + '\nassert result is None',
    'result = dict.clear(holder) ' + REASON + '\nassert result is None',
    'result = dict.update(holder, {}) ' + REASON + '\nassert result is None',
])
def test_key_only_and_none_returning_mapping_methods_remain_nonowning(source):
    violations = check_fixture_writes(PREFIX + source)
    assert any(v.line == 1 and 'mutable shared fixture containers' in v.rule for v in violations)
    independent = "import sqlite3\nindependent = sqlite3.connect(':memory:')\n"
    assert check_fixture_writes(independent + PREFIX.replace('bench.workspace.connection', 'independent') + source) == []


@pytest.mark.parametrize('source', [
    "handle = dict.get(holder, (holder := safe) and 'shared') " + REASON + '\nhandle.close()',
    "accessor = dict.get\nhandle = accessor(holder, (holder := safe) and 'shared') " + REASON + '\nhandle.close()',
    "from builtins import dict as mapping\nhandle = mapping.__getitem__(holder, (holder := safe) and 'shared') " + REASON + '\nhandle.close()',
    "method = 'get' if condition else '__getitem__'\naccessor = getattr(dict, method)\naccessor(holder, 'shared').close()",
    "import builtins\nmethod = 'get' if condition else '__getitem__'\naccessor = builtins.getattr(builtins.dict, method)\naccessor(holder, 'shared').close()",
    "accessor = dict.get if condition else dict.__getitem__\naccessor(holder, 'shared').close()",
    "holder = dict.fromkeys(['shared'], bench.workspace.connection)\nfor handle in holder.values():\n    handle.close()",
])
def test_value_returning_accessor_paths_retain_known_shared_ownership(source):
    assert check_fixture_writes(PREFIX + source)


@pytest.mark.parametrize('expression', [
    '[paths.values()] if condition else []',
    '([paths.values()],) if condition else ()',
    'resources or [paths.values()]',
])
def test_nested_active_paths_survive_heterogeneous_merges_after_exact_exception(expression):
    source = "import sqlite3\npaths = {'db': bench.workspace.path}\ngroups = " + expression + ' ' + REASON + '\nfor group in groups:\n    for path in group:\n        sqlite3.connect(path)'
    assert any(v.line > 3 for v in check_fixture_writes(source))


@pytest.mark.parametrize('source', [
    "handle = dict.get(safe, (holder := safe) and 'shared')\nhandle.close()",
    "method = 'get' if condition else '__getitem__'\naccessor = getattr(dict, method)\naccessor(safe, 'shared').close()",
    "holder = dict.fromkeys(['item'], independent)\nfor handle in holder.values():\n    handle.close()",
])
def test_independent_receiver_and_default_controls(source):
    prefix = "import sqlite3\nindependent = sqlite3.connect(':memory:')\n"
    assert any(v.line == 3 and 'mutable shared fixture containers' in v.rule
               for v in check_fixture_writes(prefix + PREFIX + source))
    assert check_fixture_writes(prefix + PREFIX.replace('bench.workspace.connection', 'independent') + source) == []


@pytest.mark.parametrize('expression', [
    'bench.workspace.path if condition else other_path',
    'bench.workspace.path or other_path',
])
def test_plain_merged_path_keys_remain_nonowning(expression):
    assert check_fixture_writes('path = ' + expression + '\nlabels = {path: 0}') == []


@pytest.mark.parametrize('source', [
    "handle = dict.get(holder, (holder := safe) and 'shared') " + REASON + '\nhandle.close()',
    "method = 'get' if condition else '__getitem__'\naccessor = getattr(dict, method)\naccessor(holder, 'shared').close()",
])
def test_real_shared_closure_precedes_inventory_rejection(source):
    raw = sqlite3.connect(':memory:')
    independent = sqlite3.connect(':memory:')
    bench = SimpleNamespace(workspace=SimpleNamespace(connection=raw))
    source = PREFIX + source
    try:
        exec(source, {'bench': bench, 'independent': independent, 'condition': True})
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            raw.execute('SELECT 1')
        assert check_fixture_writes(source)
    finally:
        raw.close()
        independent.close()
