"""Final-review ownership losses reject without extending container evaluation."""
from tests.fixture_probe import fixture_probe
import sqlite3
from types import SimpleNamespace

import pytest

from tests.fixture_write_policy import check_fixture_writes


REASON = '# fixture-write: Isolated synthetic expression deliberately carries fixture provenance.'
HANDLES = [
    'handles = () + (bench.workspace.connection,)\nfor handle in handles:\n    handle.close()',
    'handles = (bench.workspace.connection,) * 2\nfor handle in handles:\n    handle.close()',
    'handles = 2 * (bench.workspace.connection,)\nfor handle in handles:\n    handle.close()',
    'handles = []\nhandles += (bench.workspace.connection,)\nfor handle in handles:\n    handle.close()',
    'handles = (handle for handle in (bench.workspace.connection,))\nfor handle in handles:\n    handle.close()',
    'original = (bench.workspace.connection,)\nhandles = original + (original := ())\nfor handle in handles:\n    handle.close()',
]
PATHS = [
    'paths = () + (bench.workspace.path,)',
    'paths = (bench.workspace.path,) * 2',
    'paths = []\npaths += (bench.workspace.path,)',
    "paths = {key: bench.workspace.path for key in ('db',)}.values()",
    'paths = {bench.workspace.path: None for _ in (1,)}',
    'paths = (path for path in (bench.workspace.path,))',
    'paths = {bench.workspace.path: None}',
    "paths = {'workbench.sqlite': None}",
    'original = (bench.workspace.path,)\npaths = original + (original := ())',
]
OPEN = '\nfor path in paths:\n    with sqlite3.connect(path) as other:\n        other.execute("DELETE FROM synthetic")'


@pytest.mark.parametrize('source', HANDLES)
@pytest.mark.parametrize('annotated', [False, True])
def test_final_review_transports_are_structurally_rejected(source, annotated):
    assert_structural_rejection(source, annotated)


@pytest.mark.parametrize('transport', PATHS)
@pytest.mark.parametrize('annotated', [False, True])
def test_final_review_paths_are_structurally_rejected(transport, annotated):
    assert_structural_rejection(transport + OPEN, annotated)


def assert_structural_rejection(source, annotated):
    if annotated:
        source = '\n'.join(line + ' ' + REASON for line in source.splitlines())
    violations = check_fixture_writes('import sqlite3\n' + source)
    assert violations
    assert any(v.structural for v in violations)
    assert all('fixture_write' in str(v) for v in violations)


@pytest.mark.parametrize('source', HANDLES)
def test_same_source_really_closes_shared_connection_before_rejection(source):
    raw = sqlite3.connect(':memory:')
    bench = SimpleNamespace(workspace=SimpleNamespace(connection=raw))
    try:
        error, _ = fixture_probe(source, bench=bench)
        if error is not None:
            raise error
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            raw.execute('SELECT 1')
        assert check_fixture_writes(source)
    finally:
        raw.close()


@pytest.mark.parametrize('transport', PATHS)
def test_same_source_really_mutates_active_database_before_rejection(tmp_path, monkeypatch, transport):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / 'workbench.sqlite'
    raw = sqlite3.connect(path)  # fixture-write: Isolated synthetic database deliberately uses the active database basename for a negative probe.
    bench = SimpleNamespace(workspace=SimpleNamespace(path=path, connection=raw))
    source = 'import sqlite3\n' + transport + OPEN
    try:
        raw.execute('CREATE TABLE synthetic(value TEXT)')  # fixture-write: Initialize the isolated negative-probe database before executing the rejected source.
        raw.execute("INSERT INTO synthetic VALUES ('retained')")  # fixture-write: Seed the isolated negative-probe database before executing the rejected source.
        raw.commit()  # fixture-write: Publish the isolated negative-probe seed so its second connection can delete it.
        error, _ = fixture_probe(source, bench=bench)
        if error is not None:
            raise error
        assert raw.execute('SELECT count(*) FROM synthetic').fetchone() == (0,)
        assert check_fixture_writes(source)
    finally:
        raw.close()  # fixture-write: Close the isolated synthetic negative-probe database after checking the actual mutation.


@pytest.mark.parametrize('source', [
    "sql = 'SELECT ' + '1'\nbench.workspace.connection.execute(sql)",
    'path = bench.workspace.path / "synthetic"\nlabel = str(path)',
    'handles = () + (independent,)\nfor handle in handles:\n    handle.close()',
    'handles = []\nhandles += (independent,)\nfor handle in handles:\n    handle.close()',
    'handles = (handle for handle in (independent,))\nfor handle in handles:\n    handle.close()',
    'labels = {path: None for path in ("synthetic.sqlite",)}',
])
def test_scalar_operations_and_independent_resource_controls_stay_supported(source):
    prefix = "import sqlite3\nindependent = sqlite3.connect(':memory:')\n"
    assert check_fixture_writes(prefix + source) == []
