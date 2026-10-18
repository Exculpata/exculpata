"""Evaluation order and explicit unsupported outer-scope/container mutations."""
import sqlite3
from types import SimpleNamespace

import pytest

from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize('pattern', ['None', 'True', 'False', '0', 'None as handle', '_ as handle'])
def test_non_destructuring_shared_match_patterns_are_allowed(pattern):
    assert check_fixture_writes(f'match bench.workspace.connection:\n    case {pattern}:\n        pass') == []


def test_whole_subject_alias_still_protects_shared_cleanup():
    assert any('close' in v.rule for v in check_fixture_writes('match bench.workspace.connection:\n    case _ as handle:\n        handle.close()'))


@pytest.mark.parametrize('action', [
    'handle = holder[(holder := ordinary) and 0]\nhandle.close()',
    'holder[(holder := ordinary) and 0].close()',
    'for handle in [independent, holder[(holder := ordinary) and 0]]:\n    handle.close()',
])
def test_subscript_uses_the_container_captured_before_its_index(action):
    prefix = "import sqlite3\nindependent = sqlite3.connect(':memory:')\nholder = [bench.workspace.connection]\nordinary = [independent]\n"
    assert check_fixture_writes(prefix + action)


def test_subscript_rebinding_does_not_retroactively_change_an_independent_base():
    source = "import sqlite3\nindependent = sqlite3.connect(':memory:')\nholder = [independent]\nshared = [bench.workspace.connection]\nhandle = holder[(holder := shared) and 0]\nhandle.close()"
    violations = check_fixture_writes(source)
    assert violations and {v.line for v in violations} == {4}
    assert not any('close' in v.rule for v in violations)
    assert check_fixture_writes(source.replace('shared = [bench.workspace.connection]', 'shared = (bench.workspace.connection,)')) == []


@pytest.mark.parametrize('action', [
    'connection.execute("INSERT INTO synthetic_fixture VALUES (?)", ((connection := independent) and "pending",))',
    'sqlite3.Connection.execute(connection, "INSERT INTO synthetic_fixture VALUES (?)", ((connection := independent) and "pending",))',
    'def write(handle, value):\n    handle.execute("INSERT INTO synthetic_fixture VALUES (?)", (value,))\nwrite(connection, (connection := independent) and "pending")',
    'operation = connection.execute\noperation("INSERT INTO synthetic_fixture VALUES (?)", ((operation := independent.execute) and "pending",))',
])
def test_argument_rebinding_cannot_reclassify_a_shared_call(action):
    prefix = "import sqlite3\nindependent = sqlite3.connect(':memory:')\nconnection = bench.workspace.connection\n"
    assert check_fixture_writes(prefix + action)


@pytest.mark.parametrize('expression', [
    '[bench.workspace.connection] if condition else resources',
    'resources if condition else [bench.workspace.connection]',
    '[[bench.workspace.connection]] if condition else resources',
    "{'shared': bench.workspace.connection} if condition else resources",
])
def test_heterogeneous_shared_container_ownership_cannot_disappear(expression):
    assert check_fixture_writes(f'holder = {expression}\nfor handle in holder:\n    handle.close()')


@pytest.mark.parametrize('source', [
    'if condition:\n    holder = [bench.workspace.connection]\nelse:\n    holder = resources\nfor handle in holder:\n    handle.close()',
    'holder = resources or [bench.workspace.connection]\nfor handle in holder:\n    handle.close()',
])
def test_branch_and_boolean_merges_keep_nested_shared_ownership(source):
    assert check_fixture_writes(source)


@pytest.mark.parametrize('source', [
    'handle = independent\ndef redirect():\n    global handle\n    handle = bench.workspace.connection\nredirect()\nhandle.close()',
    'def outer():\n    handle = independent\n    def redirect():\n        nonlocal handle\n        handle = bench.workspace.connection\n    redirect()\n    handle.close()\nouter()',
    'handle = independent\ndef redirect(value):\n    global handle\n    handle = value\nredirect(bench.workspace.connection)\nhandle.close()',
])
def test_helpers_cannot_hide_shared_global_or_nonlocal_writes(source):
    assert check_fixture_writes("import sqlite3\nindependent = sqlite3.connect(':memory:')\n" + source)


@pytest.mark.parametrize('source', [
    'connection = independent\nconnection.execute("INSERT INTO synthetic_fixture VALUES (?)", ((connection := independent) and "pending",))',
    'holder = [independent] if condition else resources\nfor handle in holder:\n    handle.close()',
    'def redirect():\n    global marker\n    marker = "synthetic"\n    bench.workspace.connection.execute("SELECT 1")\nredirect()',
    'def redirect():\n    global handle\n    handle = independent\nredirect()\nhandle.close()',
    'def read():\n    global handle\n    return bench.workspace.connection\nread().execute("SELECT 1")',
])
def test_independent_containers_calls_and_nonshared_outer_writes_remain_supported(source):
    assert check_fixture_writes("import sqlite3\nindependent = sqlite3.connect(':memory:')\n" + source) == []


@pytest.mark.parametrize('action', [
    'holder = [bench.workspace.connection]\nordinary = [independent]\nhandle = holder[(holder := ordinary) and 0]\nhandle.close()',
    'holder = [bench.workspace.connection] if condition else resources\nfor handle in holder:\n    handle.close()',
    'handle = independent\ndef redirect():\n    global handle\n    handle = bench.workspace.connection\nredirect()\nhandle.close()',
])
def test_reported_indirections_close_a_real_shared_connection_before_rejection(action):
    raw, independent = sqlite3.connect(':memory:'), sqlite3.connect(':memory:')
    bench = SimpleNamespace(workspace=SimpleNamespace(connection=raw))
    source = "import sqlite3\nindependent = sqlite3.connect(':memory:')\n" + action
    try:
        exec(action, {'bench': bench, 'independent': independent, 'condition': True, 'resources': []})
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            raw.execute('SELECT 1')
        assert check_fixture_writes(source)
    finally:
        raw.close()
        independent.close()


def test_argument_walrus_executes_through_original_shared_connection_before_rejection():
    raw, independent = sqlite3.connect(':memory:'), sqlite3.connect(':memory:')
    bench = SimpleNamespace(workspace=SimpleNamespace(connection=raw))
    action = 'connection.execute("INSERT INTO synthetic_fixture VALUES (?)", ((connection := independent) and "pending",))'
    source = "import sqlite3\nindependent = sqlite3.connect(':memory:')\nconnection = bench.workspace.connection\n" + action
    try:
        for connection in [raw, independent]:
            connection.execute('CREATE TABLE synthetic_fixture(value TEXT)')
        namespace = {'connection': raw, 'independent': independent}
        exec(action, namespace)
        assert raw.in_transaction
        assert raw.execute('SELECT value FROM synthetic_fixture').fetchall() == [('pending',)]
        assert independent.execute('SELECT value FROM synthetic_fixture').fetchall() == []
        assert namespace['connection'] is independent
        assert check_fixture_writes(source)
    finally:
        raw.close()
        independent.close()


@pytest.mark.parametrize('source', [
    'def inspect():\n    global handle\n    return [handle for handle in [bench.workspace.connection]]\ninspect()',
    'def inspect():\n    global handle\n    local = lambda: (handle := bench.workspace.connection)\n    local()\ninspect()',
    'def inspect():\n    global handle\n    class Local:\n        handle = bench.workspace.connection\ninspect()',
])
def test_nested_local_bindings_do_not_inherit_outer_global_declaration(source):
    if 'return [handle' in source:
        violations = check_fixture_writes(source)
        assert violations and {v.line for v in violations} == {3}
        assert not any('global/nonlocal' in v.rule for v in violations)
    else:
        assert check_fixture_writes(source) == []
