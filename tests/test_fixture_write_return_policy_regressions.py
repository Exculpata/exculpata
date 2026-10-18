"""Known local helper returns must retain shared SQLite and workspace ownership."""
import pytest

from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize('returned, arguments', [
    ('store.connection.close', ''), ('store.connection.deserialize', 'payload'),
    ('store.queue_media_job', 'synthetic'), ('store.import_media_transcript', 'synthetic'),
    ('store.finish_media_job', 'synthetic'),
])
@pytest.mark.parametrize('form', ['assigned', 'immediate', 'lambda'])
def test_returned_shared_mutator_retains_provenance(returned, arguments, form):
    definition = f'def operation(store):\n    return {returned}\n'
    if form == 'assigned':
        source = definition + f'operate = operation(bench.workspace)\noperate({arguments})'
    elif form == 'immediate':
        source = definition + f'operation(bench.workspace)({arguments})'
    else:
        source = f'operation = lambda store: {returned}\noperate = operation(bench.workspace)\noperate({arguments})'
    assert check_fixture_writes(source)


@pytest.mark.parametrize('source', [
    'def get(store):\n    return store\nget(bench.workspace).queue_media_job(synthetic)',
    'def get(store):\n    return store.connection\nwith get(bench.workspace):\n    pass',
    'def get(store):\n    operation = store.connection.close\n    return operation\nget(bench.workspace)()',
    'def get(store):\n    return store.connection.close if choice else resource.close\nget(bench.workspace)()',
    'def get(store):\n    if choice:\n        return store.connection.close\n    return resource.close\nget(bench.workspace)()',
    'def get(store):\n    return store.connection.close\ndef wrapper(store):\n    return get(store)\nwrapper(bench.workspace)()',
    'def get(store):\n    return (store.connection.close, resource)\noperation, item = get(bench.workspace)\noperation()',
    'def get(store):\n    return store.connection.cursor()\nget(bench.workspace).execute("DELETE FROM synthetic")',
])
def test_explicit_return_aliases_branches_and_chains_are_conservative(source):
    assert check_fixture_writes(source)


@pytest.mark.parametrize('source', [
    'def get(item):\n    return item.close\nget(resource)()',
    "import sqlite3\ndef get(item):\n    return item.close\nget(sqlite3.connect(':memory:'))()",
    'def get(item):\n    return item.close\nget(bench.workspace.connection.cursor())()',
    'def get(store):\n    return store.close\nget(bench.workspace)()',
    'def get(store):\n    return store.connection.execute\nget(bench.workspace)("SELECT 1")',
    'def get(store):\n    return store.queue_media_job\nwith fixture_write(bench.workspace, media=True):\n    get(bench.workspace)(synthetic)',
    'def get(handle):\n    return handle.execute\nwith fixture_write(bench.workspace) as connection:\n    get(connection)("DELETE FROM synthetic")',
    'def get(handle):\n    return handle.close\nget(resource)\nget(bench.workspace.connection.cursor())()',
])
def test_return_provenance_preserves_independent_resources_and_safe_scoped_operations(source):
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('source', [
    'def operation(store):\n    return operation(store) if retry else store.connection.close\noperation(bench.workspace)()',
    'def operation(store):\n    return operation(store)\noperation(bench.workspace)()',
    'async def operation(store):\n    return store.connection.close\noperate = operation(bench.workspace)',
    'def operation(store):\n    yield store.connection.close\noperations = operation(bench.workspace)',
])
def test_known_shared_returns_fail_closed_for_recursive_async_and_generator_shapes(source):
    assert any('unsupported' in item.rule for item in check_fixture_writes(source))


@pytest.mark.parametrize('source', [
    'async def operation(item):\n    return item.close\nresult = operation(resource)',
    "import sqlite3\nasync def operation(item):\n    return item.close\nresult = operation(sqlite3.connect(':memory:'))",
    'def operation(item):\n    yield item.close\nresult = operation(resource)',
])
def test_unsupported_shared_return_rule_preserves_independent_and_resource_controls(source):
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('body', [
    'cursor = bench.workspace.connection.execute("SELECT 1")',
    'cursor = bench.workspace.connection.cursor()',
    'consume(bench.workspace.connection.execute("SELECT 1"))',
    'return bench.workspace.connection.execute("SELECT 1")',
    'cursor = bench.workspace.connection.cursor().execute("SELECT 1")',
    'saved = bench.workspace.connection.execute("SELECT 1").fetchone',
    'saved = bench.workspace.connection.cursor().close',
])
def test_retained_raw_cursor_acquisition_requires_yielded_handle(body):
    source = 'def use():\n    with fixture_write(bench.workspace):\n        ' + body
    assert any('yielded' in item.rule for item in check_fixture_writes(source))


@pytest.mark.parametrize('body', [
    'bench.workspace.connection.execute("DELETE FROM synthetic")',
    'rows = bench.workspace.connection.execute("SELECT 1").fetchall()',
    'row = bench.workspace.connection.cursor().execute("SELECT 1").fetchone()',
    'count = bench.workspace.connection.execute("DELETE FROM synthetic").rowcount',
    'bench.workspace.connection.cursor().close()',
])
def test_immediate_in_scope_cursor_consumption_and_discarded_writes_remain_supported(body):
    assert check_fixture_writes('with fixture_write(bench.workspace):\n    ' + body) == []


def test_retained_yielded_and_independent_cursors_remain_supported():
    assert check_fixture_writes('with fixture_write(bench.workspace) as connection:\n    cursor = connection.execute("SELECT 1")') == []
    assert check_fixture_writes("import sqlite3\nindependent = sqlite3.connect(':memory:')\nwith fixture_write(bench.workspace):\n    cursor = independent.execute('SELECT 1')") == []


@pytest.mark.parametrize('source', [
    'def operation():\n    return bench.workspace.connection.close\noperation()()',
    'def operation(store=bench.workspace):\n    return store.connection.deserialize\noperation()(payload)',
    'operation = lambda store=bench.workspace: store.finish_media_job\noperation()(synthetic)',
    'operation = lambda: bench.workspace.connection.close\noperation()()',
    'shared = bench.workspace\ndef operation():\n    return shared.queue_media_job\noperation()(synthetic)',
    'def get():\n    return bench.workspace.connection.close\ndef wrapper():\n    return get()\nwrapper()()',
])
def test_known_defaults_and_no_argument_explicit_returns_preserve_shared_provenance(source):
    assert check_fixture_writes(source)


@pytest.mark.parametrize('source', [
    'def operation():\n    return resource.close\noperation()()',
    "import sqlite3\nindependent = sqlite3.connect(':memory:')\ndef operation(handle=independent):\n    return handle.close\noperation()()",
    'def operation():\n    return bench.workspace.close\noperation()()',
])
def test_no_argument_and_default_return_controls_preserve_nonshared_cleanup(source):
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('source', [
    'def operation():\n    return shared.close\nshared = bench.workspace.connection\noperation()()',
    'operation = lambda: shared.close\nshared = bench.workspace.connection\noperation()()',
    'def outer():\n    def operation():\n        return shared.close\n    shared = bench.workspace.connection\n    operation()()\nouter()',
    "import sqlite3\nshared = sqlite3.connect(':memory:')\ndef operation():\n    return shared.close\nshared = bench.workspace.connection\noperation()()",
])
def test_explicit_return_uses_late_global_and_closure_binding_history(source):
    assert check_fixture_writes(source)


@pytest.mark.parametrize('source', [
    "import sqlite3\ndef operation():\n    return shared.close\nshared = sqlite3.connect(':memory:')\noperation()()",
    "import sqlite3\nindependent = sqlite3.connect(':memory:')\ndef operation(shared=independent):\n    return shared.close\nshared = bench.workspace.connection\noperation()()",
    "import sqlite3\nshared = bench.workspace.connection\n[(lambda: shared.close)()() for shared in [sqlite3.connect(':memory:')]]",
])
def test_late_return_history_preserves_independent_defaults_and_comprehension_shadowing(source):
    assert check_fixture_writes(source) == []
