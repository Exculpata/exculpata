"""Unsupported shared indirection rejects without expanding the interpreter."""
from tests.fixture_probe import fixture_probe
import sqlite3
from types import SimpleNamespace

import pytest

from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize('source', [
    'holder = [bench.workspace.connection]\nhandle = holder[0]\nhandle.close()',
    'holder = [bench.workspace.connection]\nfor handle in [independent, holder[0]]:\n    handle.close()',
    "holder = {'shared': bench.workspace.connection}\nhandle = holder['shared']\nhandle.close()",
    'holder = [bench.workspace.connection]\nhandle = holder[index]',
    'holder = [[bench.workspace.connection]]\nhandle = holder[0][0]',
    'holder = [bench.workspace.connection.close]\noperation = holder[0]\noperation()',
    'def dispose(*handles):\n    for handle in handles:\n        handle.close()\ndispose(bench.workspace.connection)',
    "def dispose(**handles):\n    handles['shared'].close()\ndispose(shared=bench.workspace.connection)",
    'def dispose(label, *handles):\n    for handle in handles:\n        handle.close()\ndispose("synthetic", bench.workspace.connection)',
    'def dispose(*handles):\n    pass\ndispose(*[bench.workspace.connection])',
    "def dispose(**handles):\n    pass\ndispose(**{'shared': bench.workspace.connection})",
    'shared = bench.workspace.connection\ndef dispose(*handles):\n    shared.close()\ndispose(shared)',
    'match [bench.workspace.connection]:\n    case [handle]:\n        handle.close()',
    "match {'shared': bench.workspace.connection}:\n    case {'shared': handle}:\n        handle.close()",
    'match [bench.workspace.connection]:\n    case [*handles]:\n        pass',
    'match [[bench.workspace.connection]]:\n    case [[handle]]:\n        handle.close()',
    'match [bench.workspace.connection]:\n    case [handle] as handles:\n        handle.close()',
])
def test_known_shared_unsupported_indirection_requires_explicit_fixture_setup(source):
    prefix = "import sqlite3\nindependent = sqlite3.connect(':memory:')\n"
    assert any('unsupported' in v.rule for v in check_fixture_writes(prefix + source))


@pytest.mark.parametrize('source', [
    'holder = [independent]\nhandle = holder[0]\nhandle.close()',
    'holder = [resource]\nhandle = holder[index]',
    'def dispose(*handles):\n    for handle in handles:\n        handle.close()\ndispose(independent, resource)',
    "def dispose(**handles):\n    handles['independent'].close()\ndispose(independent=independent)",
    'match [independent]:\n    case [handle]:\n        handle.close()',
    'match resource:\n    case [handle]:\n        handle.close()',
    'match bench.workspace.connection:\n    case handle:\n        handle.execute("SELECT 1")',
    'bench.workspace.records[key] = synthetic',
    'bench.workspace.records[key]',
    'def workflow(store, **options):\n    store.append_audit_event(**options)\nworkflow(bench.workspace, label="synthetic")',
    'def workflow(store, *options):\n    store.append_audit_event()\nworkflow(bench.workspace, "synthetic")',
])
def test_unsupported_rules_preserve_independent_generic_and_simple_capture_controls(source):
    prefix = "import sqlite3\nindependent = sqlite3.connect(':memory:')\n"
    assert check_fixture_writes(prefix + source) == []


@pytest.mark.parametrize('form', ['subscript', 'attribute', 'loop', 'nested'])
@pytest.mark.parametrize('to_shared', [False, True])
def test_assignment_target_expression_binds_before_later_setter(form, to_shared):
    before = 'independent' if to_shared else 'bench.workspace.connection'
    after = 'bench.workspace.connection' if to_shared else 'independent'
    target = f'(sink[(handle := {after}) and 0], handle.isolation_level)'
    if form == 'attribute':
        target = f'(((handle := {after}) and sink).marker, handle.isolation_level)'
    prefix = f"import sqlite3\nindependent = sqlite3.connect(':memory:')\nhandle = {before}\n"
    if form == 'loop':
        source = f'for {target} in [(1, None)]:\n    pass'
    elif form == 'nested':
        source = f'(marker, {target}) = (0, (1, None))'
    else:
        source = f'{target} = (1, None)'
    assert bool(check_fixture_writes(prefix + source)) is to_shared


@pytest.mark.parametrize('source', [
    'holder = [bench.workspace.connection]\nfor handle in [independent, holder[0]]:\n    handle.close()',
    'def dispose(*handles):\n    for handle in handles:\n        handle.close()\ndispose(bench.workspace.connection)',
    'match [bench.workspace.connection]:\n    case [handle]:\n        handle.close()',
])
def test_unsupported_indirection_actually_closes_shared_connection_before_rejection(source):
    raw, independent = sqlite3.connect(':memory:'), sqlite3.connect(':memory:')
    bench = SimpleNamespace(workspace=SimpleNamespace(connection=raw))
    try:
        error, _ = fixture_probe(source, bench=bench, independent=independent)
        if error is not None:
            raise error
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            raw.execute('SELECT 1')
        assert check_fixture_writes(source)
    finally:
        raw.close()
        independent.close()


def test_target_walrus_rebinds_real_setter_and_commits_pending_work(tmp_path):
    path = tmp_path / 'synthetic-target-expression.sqlite'
    raw, independent = sqlite3.connect(path), sqlite3.connect(':memory:')
    bench = SimpleNamespace(workspace=SimpleNamespace(connection=raw))
    action = '(sink[(handle := bench.workspace.connection) and 0], handle.isolation_level) = (1, None)'
    source = "import sqlite3\nhandle = sqlite3.connect(':memory:')\nsink = [0]\n" + action
    try:
        raw.execute('CREATE TABLE synthetic_fixture(value TEXT)')
        # fixture-write: Deliberately retain pending synthetic data for the target expression transaction termination probe.
        raw.execute("INSERT INTO synthetic_fixture VALUES ('pending')")
        error, changed_sink = fixture_probe(action, 'sink', bench=bench, handle=independent, sink=[0])
        if error is not None:
            raise error
        assert changed_sink == [1]
        with sqlite3.connect(path) as observer:
            assert observer.execute('SELECT value FROM synthetic_fixture').fetchall() == [('pending',)]
        assert check_fixture_writes(source)
    finally:
        raw.close()
        independent.close()


@pytest.mark.parametrize('source', [
    "def dispose(marker, /, **handles):\n    handles['marker'].close()\ndispose(independent, marker=bench.workspace.connection)",
    "def dispose(*handles):\n    for handle in handles:\n        handle.close()\ndispose(*resources, bench.workspace.connection)",
    "def dispose(*handles):\n    for handle in handles:\n        handle.close()\ndispose(*resources, *[bench.workspace.connection])",
])
def test_ambiguous_or_positional_only_variadic_shared_inputs_reject(source):
    assert any('unsupported' in v.rule for v in check_fixture_writes(source))


def test_positional_only_shared_binding_is_not_overwritten_by_keyword_options():
    source = "import sqlite3\nindependent = sqlite3.connect(':memory:')\ndef dispose(handle, /, **options):\n    handle.close()\ndispose(bench.workspace.connection, handle=independent)"
    assert any('close' in v.rule for v in check_fixture_writes(source))
