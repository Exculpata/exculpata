"""Known non-SQL SQLite mutations must retain shared-workspace ownership."""
import pytest

from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize('attribute', ['row_factory', 'text_factory'])
@pytest.mark.parametrize('form', ['del', 'delattr', 'saved_delattr', 'descriptor', 'saved_descriptor', 'getattr'])
def test_shared_factory_deletion_is_unsupported(attribute, form):
    receiver = 'bench.workspace.connection'
    operation = {
        'del': f'del {receiver}.{attribute}',
        'delattr': f"delattr({receiver}, '{attribute}')",
        'saved_delattr': f"remove = delattr\nremove({receiver}, '{attribute}')",
        'descriptor': f'sqlite3.Connection.{attribute}.__delete__({receiver})',
        'saved_descriptor': f'remove = sqlite3.Connection.{attribute}.__delete__\nremove({receiver})',
        'getattr': f"remove = getattr(sqlite3.Connection.{attribute}, '__delete__')\nremove({receiver})",
    }[form]
    assert check_fixture_writes('import sqlite3\n' + operation)


@pytest.mark.parametrize('source', [
    'with bench.workspace._lock:\n    del bench.workspace.connection.text_factory',
    'with fixture_write(bench.workspace) as connection:\n    del connection.row_factory',
    'import sqlite3\nsqlite3.Cursor.row_factory.__delete__(bench.workspace.connection.cursor())',
    'del (bench.workspace.connection.row_factory, bench.workspace.connection.text_factory)',
])
def test_locks_and_scoped_handles_do_not_allow_shared_factory_deletion(source):
    assert check_fixture_writes(source)


@pytest.mark.parametrize('source', [
    'del resource.row_factory', 'delattr(resource, "text_factory")',
    "import sqlite3\nconnection = sqlite3.connect(':memory:')\ndel connection.row_factory",
    "import sqlite3\nconnection = sqlite3.connect(':memory:')\nsqlite3.Connection.row_factory.__delete__(connection)",
    'del alias',
])
def test_factory_deletion_preserves_nonshared_controls_without_executing_deletion(source):
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('method, arguments', [
    ('deserialize', 'payload'), ('blobopen', "'synthetic', 'value', 1"),
    ('blobopen', "'synthetic', 'value', 1, readonly=True"),
    ('setlimit', 'category, limit'),
])
@pytest.mark.parametrize('form', ['direct', 'saved', 'getattr', 'unbound', 'saved_unbound'])
def test_non_sql_connection_mutations_cannot_escape_inventory(method, arguments, form):
    receiver = 'bench.workspace.connection'
    operation = {
        'direct': f'{receiver}.{method}({arguments})',
        'saved': f'operate = {receiver}.{method}\noperate({arguments})',
        'getattr': f"operate = getattr({receiver}, '{method}')\noperate({arguments})",
        'unbound': f'sqlite3.Connection.{method}({receiver}, {arguments})',
        'saved_unbound': f'operate = sqlite3.Connection.{method}\noperate({receiver}, {arguments})',
    }[form]
    assert check_fixture_writes('import sqlite3\n' + operation)


@pytest.mark.parametrize('operation', ['deserialize(payload)', "blobopen('synthetic', 'value', 1)"])
@pytest.mark.parametrize('boundary, receiver', [
    ('with bench.workspace._lock:', 'bench.workspace.connection'),
    ('with fixture_write(bench.workspace) as connection:', 'connection'),
])
def test_unsupported_non_sql_apis_are_not_licensed_by_a_boundary(operation, boundary, receiver):
    assert check_fixture_writes(f'{boundary}\n    {receiver}.{operation}')


@pytest.mark.parametrize('source', [
    'source.backup(bench.workspace.connection)',
    'source.backup(target=bench.workspace.connection)',
    'backup = source.backup\nbackup(bench.workspace.connection)',
    'backup = getattr(source, "backup")\nbackup(bench.workspace.connection)',
    'import sqlite3\nsqlite3.Connection.backup(source, bench.workspace.connection)',
    'import sqlite3\nbackup = sqlite3.Connection.backup\nbackup(source, target=bench.workspace.connection)',
    'with fixture_write(bench.workspace) as connection:\n    source.backup(connection)',
    'with fixture_write(bench.workspace) as connection:\n    connection.backup(independent)',
])
def test_backup_rejects_shared_destinations_and_unsupported_scoped_source(source):
    assert check_fixture_writes(source)


@pytest.mark.parametrize('source', [
    "import sqlite3\nindependent = sqlite3.connect(':memory:')\nindependent.deserialize(payload)",
    "import sqlite3\nindependent = sqlite3.connect(':memory:')\nindependent.blobopen('synthetic', 'value', 1)",
    "import sqlite3\nindependent = sqlite3.connect(':memory:')\nindependent.setlimit(category, limit)",
    "import sqlite3\nindependent = sqlite3.connect(':memory:')\nwith bench.workspace._lock:\n    bench.workspace.connection.backup(independent)",
    'resource.deserialize(payload)', 'resource.blobopen("synthetic", "value", 1)',
    'resource.setlimit(category, limit)', 'resource.backup()',
    'bench.workspace.connection.serialize()', 'bench.workspace.connection.getlimit(category)',
    'with bench.workspace._lock:\n    bench.workspace.connection.setlimit(category, limit)',
    'with fixture_write(bench.workspace) as connection:\n    connection.setlimit(category, limit)',
])
def test_non_sql_controls_preserve_independent_resources_reads_and_supported_configuration(source):
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('arguments', ['bench.workspace', '*(bench.workspace,)', "**{'store': bench.workspace}"])
def test_manual_media_sequence_inside_local_helper_retains_live_workspace_provenance(arguments):
    source = ('def seed(store):\n'
              '    store.queue_media_job(synthetic)\n'
              '    store.claim_media_job(synthetic)\n'
              '    store.import_media_transcript(synthetic)\n'
              '    store.finish_media_job(synthetic)\n'
              f'seed({arguments})')
    assert {item.line for item in check_fixture_writes(source)} == ({2, 4, 5, 6} if arguments.startswith('**') else {2, 4, 5})


@pytest.mark.parametrize('source', [
    'seed = lambda store: store.queue_media_job(synthetic)\nseed(bench.workspace)',
    'def seed(store):\n    queue = store.queue_media_job\n    queue(synthetic)\nhelper = seed\nhelper(bench.workspace)',
    'def seed(store):\n    store.finish_media_job(synthetic)\ndef wrapper(value):\n    seed(value)\nwrapper(bench.workspace)',
])
def test_workspace_aliases_lambdas_and_helper_chains_are_specialized(source):
    assert check_fixture_writes(source)


@pytest.mark.parametrize('source', [
    'def seed(store):\n    store.queue_media_job(synthetic)\nseed(resource)',
    'def seed(store):\n    with fixture_write(store, media=True):\n        store.queue_media_job(synthetic)\nseed(bench.workspace)',
    'def seed(store):\n    store.queue_media_job(synthetic)\nwith fixture_write(bench.workspace, media=True):\n    seed(bench.workspace)',
    'def read(store):\n    return store.list_media_jobs(synthetic)\nread(bench.workspace)',
])
def test_workspace_specialization_preserves_explicit_media_scope_and_non_sql_controls(source):
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('source', [
    "import sqlite3\nindependent = sqlite3.connect(':memory:')\nbench.workspace.connection.backup(independent)",
    "import sqlite3\nindependent = sqlite3.connect(':memory:')\nwith other._lock:\n    bench.workspace.connection.backup(independent)",
    "import sqlite3\nindependent = sqlite3.connect(':memory:')\nsqlite3.Connection.backup(bench.workspace.connection, independent)",
])
def test_shared_source_backup_requires_owning_lock(source):
    assert check_fixture_writes(source)


@pytest.mark.parametrize('method, arguments', [
    ('setconfig', 'category, enabled'), ('enable_load_extension', 'enabled'),
    ('load_extension', 'synthetic_extension_path'),
])
@pytest.mark.parametrize('form', ['direct', 'saved', 'getattr', 'unbound', 'scoped'])
def test_known_unsupported_sqlite_setters_are_inventoried_without_invoking_them(method, arguments, form):
    operation = {
        'direct': f'bench.workspace.connection.{method}({arguments})',
        'saved': f'operate = bench.workspace.connection.{method}\noperate({arguments})',
        'getattr': f"operate = getattr(bench.workspace.connection, '{method}')\noperate({arguments})",
        'unbound': f'sqlite3.Connection.{method}(bench.workspace.connection, {arguments})',
        'scoped': f'with fixture_write(bench.workspace) as connection:\n    connection.{method}({arguments})',
    }[form]
    assert check_fixture_writes('import sqlite3\n' + operation)


@pytest.mark.parametrize('source', [
    'bench.workspace.connection.getconfig(category)',
    'bench.workspace.connection.getlimit(category)',
    'bench.workspace.connection.serialize()',
    'bench.workspace.connection.iterdump()',
    'with bench.workspace._lock:\n    bench.workspace.connection.interrupt()',
    'with fixture_write(bench.workspace) as connection:\n    connection.interrupt()',
    'resource.setconfig(category, enabled)',
    "import sqlite3\nindependent = sqlite3.connect(':memory:')\nindependent.setconfig(category, enabled)",
])
def test_finite_api_inventory_preserves_reads_supported_cancellation_and_nonshared_controls(source):
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('source', [
    'bench.workspace.connection.interrupt()',
    'cancel = bench.workspace.connection.interrupt\ncancel()',
    'cancel = getattr(bench.workspace.connection, "interrupt")\ncancel()',
    'import sqlite3\nsqlite3.Connection.interrupt(bench.workspace.connection)',
    'with other._lock:\n    bench.workspace.connection.interrupt()',
])
def test_supported_interrupt_still_requires_owning_boundary(source):
    assert check_fixture_writes(source)


@pytest.mark.parametrize('boundary', [
    'with fixture_write(bench.workspace):',
    'with fixture_write(bench.workspace), bench.workspace._lock:',
])
def test_raw_shared_source_backup_is_unsupported_inside_owning_fixture(boundary):
    source = ("import sqlite3\nindependent = sqlite3.connect(':memory:')\n"
              + boundary + '\n    bench.workspace.connection.backup(independent)')
    assert check_fixture_writes(source)
