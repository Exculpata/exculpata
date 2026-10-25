"""Class declarations, direct Boolean results and precise outer-write diagnostics."""
from tests.fixture_probe import fixture_probe
import sqlite3
from types import SimpleNamespace

import pytest

from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize('source', [
    'handle = independent\ndef redirect():\n    class Local:\n        global handle\n        handle = bench.workspace.connection\nredirect()\nhandle.close()',
    'def outer():\n    handle = independent\n    def redirect():\n        class Local:\n            nonlocal handle\n            handle = bench.workspace.connection\n    redirect()\n    handle.close()\nouter()',
])
def test_class_scope_owns_its_global_and_nonlocal_declarations(source):
    assert check_fixture_writes("import sqlite3\nindependent = sqlite3.connect(':memory:')\n" + source)


@pytest.mark.parametrize('expression', [
    'resources or [bench.workspace.connection]',
    '[bench.workspace.connection] and resources',
    'resources or [[bench.workspace.connection]]',
])
@pytest.mark.parametrize('consumer', ['loop', 'comprehension', 'call'])
def test_direct_boolean_shared_container_results_reject(expression, consumer):
    source = {
        'loop': f'for handle in {expression}:\n    handle.close()',
        'comprehension': f'[handle.close() for handle in ({expression})]',
        'call': f'consume({expression})',
    }[consumer]
    assert any('heterogeneous' in v.rule for v in check_fixture_writes(source))


@pytest.mark.parametrize('source, expected_line', [
    ('def redirect():\n    global handle\n    match bench.workspace.connection:\n        case _ as handle:\n            pass\nredirect()', 4),
    ('def redirect():\n    global handle\n    [(handle := bench.workspace.connection) for item in [1]]\nredirect()', 3),
    ('def redirect():\n    global handle\n    [\n        (handle := bench.workspace.connection)\n        for item in [1]\n    ]\nredirect()', 4),
])
def test_synthesized_outer_write_targets_report_real_source_locations(source, expected_line):
    violations = check_fixture_writes(source, 'synthetic_scope.py')
    assert any(v.line == expected_line and 'global/nonlocal' in v.rule for v in violations), violations
    assert all(v.filename == 'synthetic_scope.py' and 1 <= v.line <= len(source.splitlines()) for v in violations)


@pytest.mark.parametrize('source', [
    'def inspect():\n    global handle\n    class Local:\n        handle = bench.workspace.connection\ninspect()',
    'def redirect():\n    class Local:\n        global handle\n        handle = independent\nredirect()',
    'for handle in resources or [independent]:\n    handle.close()',
])
def test_own_declarations_and_independent_boolean_controls(source):
    assert check_fixture_writes("import sqlite3\nindependent = sqlite3.connect(':memory:')\n" + source) == []


def test_independent_comprehension_write_does_not_revalidate_prior_shared_binding():
    source = "import sqlite3\nindependent = sqlite3.connect(':memory:')\nhandle = bench.workspace.connection\ndef redirect():\n    global handle\n    [(handle := independent) for item in [1]]\nredirect()"
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('action', [
    'handle = independent\ndef redirect():\n    class Local:\n        global handle\n        handle = bench.workspace.connection\nredirect()\nhandle.close()',
    'for handle in resources or [bench.workspace.connection]:\n    handle.close()',
])
def test_reported_class_and_boolean_paths_actually_close_shared_connection(action):
    raw, independent = sqlite3.connect(':memory:'), sqlite3.connect(':memory:')
    bench = SimpleNamespace(workspace=SimpleNamespace(connection=raw))
    source = "import sqlite3\nindependent = sqlite3.connect(':memory:')\n" + action
    try:
        error, _ = fixture_probe(action, bench=bench, independent=independent, resources=[], global_handle=True)
        if error is not None:
            raise error
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            raw.execute('SELECT 1')
        assert check_fixture_writes(source)
    finally:
        raw.close()
        independent.close()


def test_direct_boolean_shared_container_used_as_subscript_base_rejects():
    assert check_fixture_writes('(resources or [bench.workspace.connection])[0].close()')
