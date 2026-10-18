"""Ownership regressions for helpers, SQLite setters, and nested media seeds."""
from textwrap import dedent

import pytest

from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize('mutation', [
    'bench.workspace.connection.row_factory = callback',
    "setattr(bench.workspace.connection, 'row_factory', callback)",
])
def test_raw_connection_factory_requires_scoped_handle_even_with_owner_lock(mutation):
    assert check_fixture_writes('with bench.workspace._lock:\n    ' + mutation)


@pytest.mark.parametrize('kind, attribute', [
    ('Connection', 'row_factory'), ('Cursor', 'row_factory'),
    ('Connection', 'isolation_level'), ('Connection', 'autocommit'),
])
@pytest.mark.parametrize('form', ['direct', 'descriptor', 'setter', 'getattr'])
def test_sqlite_data_descriptor_setters_retain_shared_receiver(kind, attribute, form):
    descriptor = f'sqlite3.{kind}.{attribute}'
    receiver = 'bench.workspace.connection' + ('.cursor()' if kind == 'Cursor' else '')
    operation = {
        'direct': f'{descriptor}.__set__({receiver}, None)',
        'descriptor': f'descriptor = {descriptor}\ndescriptor.__set__({receiver}, None)',
        'setter': f'assign = {descriptor}.__set__\nassign({receiver}, None)',
        'getattr': f"assign = getattr({descriptor}, '__set__')\nassign({receiver}, None)",
    }[form]
    assert check_fixture_writes('import sqlite3\n' + operation)


@pytest.mark.parametrize('source', [
    'def dispose(handle):\n    handle.close()\ndispose(bench.workspace.connection)',
    'def dispose(handle):\n    close = handle.close\n    close()\ndispose(handle=bench.workspace.connection)',
    'dispose = lambda handle: handle.close()\ndispose(bench.workspace.connection)',
    '(lambda handle: handle.close())(bench.workspace.connection)',
    'def dispose(handle):\n    handle.close()\ncleanup = dispose\ncleanup(bench.workspace.connection)',
    'def dispose(action):\n    action()\ndispose(bench.workspace.connection.close)',
    'def dispose(handle):\n    handle.close()\ndef outer(item):\n    dispose(item)\nouter(bench.workspace.connection)',
    'def test_cleanup():\n    def dispose(handle):\n        handle.close()\n    dispose(bench.workspace.connection)',
    'def dispose(handle):\n    handle.close()\nwith fixture_write(bench.workspace) as connection:\n    dispose(connection)',
])
def test_local_helper_calls_propagate_shared_close_provenance(source):
    assert check_fixture_writes(source)


@pytest.mark.parametrize('receiver', [
    'resource', 'bench.workspace', "sqlite3.connect(':memory:')",
    'bench.workspace.connection.cursor()',
])
@pytest.mark.parametrize('helper', [
    'def dispose(handle):\n    handle.close()',
    'dispose = lambda handle: handle.close()',
    'def dispose(handle):\n    close = handle.close\n    close()',
])
def test_helper_close_preserves_resource_product_independent_and_cursor_controls(receiver, helper):
    assert check_fixture_writes(f'import sqlite3\n{helper}\ndispose({receiver})') == []


@pytest.mark.parametrize('kind, attribute', [
    ('Connection', 'row_factory'), ('Cursor', 'row_factory'),
    ('Connection', 'isolation_level'), ('Connection', 'autocommit'),
])
def test_sqlite_data_descriptor_independent_controls(kind, attribute):
    receiver = 'independent' + ('.cursor()' if kind == 'Cursor' else '')
    source = f"import sqlite3\nindependent = sqlite3.connect(':memory:')\nassign = sqlite3.{kind}.{attribute}.__set__\nassign({receiver}, None)"
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('method', ['queue_media_job', 'import_media_transcript', 'finish_media_job'])
@pytest.mark.parametrize('saved', [False, True])
def test_commit_capable_media_seed_rejected_in_nested_same_owner_scope(method, saved):
    operation = f'bench.workspace.{method}'
    before = f'seed = {operation}\n' if saved else ''
    source = before + f'with fixture_write(bench.workspace):\n    with fixture_write(bench.workspace):\n        {"seed" if saved else operation}(synthetic)'
    assert check_fixture_writes(source)


@pytest.mark.parametrize('source', [
    'with fixture_write(bench.workspace, media=True):\n    bench.workspace.queue_media_job(synthetic)',
    'with fixture_write(other.workspace):\n    with fixture_write(bench.workspace, media=True):\n        bench.workspace.finish_media_job(synthetic)',
    'with fixture_write(bench.workspace) as connection:\n    connection.row_factory = callback',
    'with bench.workspace._lock:\n    bench.workspace.connection.set_trace_callback(callback)',
    'descriptor = non_sql.row_factory\ndescriptor.__set__(resource, callback)',
])
def test_binding_controls_keep_valid_scopes_and_non_sql_operations(source):
    assert check_fixture_writes(source) == []


def test_nested_owner_aliases_and_plain_workspace_names_are_recognized():
    source = dedent('''\
        store = workspace
        with fixture_write(store):
            with fixture_write(workspace):
                seed = store.finish_media_job
                seed(synthetic)
    ''')
    assert [(item.line, item.rule) for item in check_fixture_writes(source)] == [
        (5, 'commit-capable media fixture seed inside nested fixture_write')]


def test_local_call_specialization_does_not_leak_argument_ownership_between_calls():
    source = dedent('''\
        def dispose(handle):
            handle.close()
        dispose(bench.workspace.connection.cursor())
        dispose(resource)
        dispose(bench.workspace)
    ''')
    assert check_fixture_writes(source) == []


def test_local_call_specialization_preserves_rebinding_and_shadowing():
    source = dedent('''\
        import sqlite3
        def dispose(handle):
            handle = sqlite3.connect(':memory:')
            handle.close()
        def retain(handle):
            def deferred(handle):
                handle.close()
            return deferred
        dispose(bench.workspace.connection)
        retain(bench.workspace.connection)
    ''')
    assert check_fixture_writes(source) == []


def test_recursive_helper_specialization_is_bounded_and_still_checks_close():
    source = dedent('''\
        def dispose(handle):
            if retry:
                dispose(handle)
            handle.close()
        dispose(bench.workspace.connection)
    ''')
    assert [(item.line, item.rule) for item in check_fixture_writes(source)] == [
        (4, 'shared connection close bypasses workspace lifetime')]


def test_generic_and_specialized_findings_are_not_duplicated():
    source = "def seed(connection):\n    connection.execute('DELETE FROM synthetic')\nseed(bench.workspace.connection)"
    assert [(item.line, item.rule) for item in check_fixture_writes(source)] == [
        (2, 'SQL fixture write outside fixture_write')]


@pytest.mark.parametrize('argument', ['', ', media=False', ', media=mode', ', media=1'])
def test_media_seeds_require_explicit_literal_opt_in(argument):
    source = f'with fixture_write(bench.workspace{argument}):\n    bench.workspace.queue_media_job(synthetic)'
    assert check_fixture_writes(source)


def test_media_mode_rejects_same_owner_nesting_even_without_seed_calls():
    source = 'with fixture_write(workspace):\n    with fixture_write(workspace, media=True):\n        pass'
    assert check_fixture_writes(source)


def test_media_mode_does_not_leak_to_deferred_seed_body():
    source = 'with fixture_write(bench.workspace, media=True):\n    deferred = lambda: bench.workspace.queue_media_job(synthetic)'
    assert check_fixture_writes(source)


@pytest.mark.parametrize('source', [
    'with fixture_write(bench.workspace, media=True):\n    bench.workspace.queue_media_job(synthetic)',
    'with fixture_write(other.workspace):\n    with fixture_write(bench.workspace, media=True):\n        bench.workspace.finish_media_job(synthetic)',
    'with fixture_write(bench.workspace, media=True):\n    with fixture_write(bench.workspace) as connection:\n        connection.execute("DELETE FROM workbench_synthetic")',
    'def seed(store):\n    with fixture_write(store, media=True):\n        store.queue_media_job(synthetic)\nwith fixture_write(bench.workspace):\n    seed(bench.workspace)',
])
def test_explicit_media_mode_preserves_safe_sql_and_runtime_entry_checks(source):
    # The last helper cannot infer its caller statically. Runtime media entry
    # rejects the active same-connection boundary before any product API runs.
    assert check_fixture_writes(source) == []
