"""Regression coverage for the fixture-write guard, including its CI inventory."""

from pathlib import Path
from textwrap import dedent

import pytest

from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize("source", [
    "with bench.workspace.connection: pass",
    "workspace = bench.workspace\nconnection = workspace.connection\nwith connection: pass",
    "connection = bench.workspace.connection\nconnection.execute('UPDATE workbench_matter SET display_name=?', ('Synthetic',))",
    "connection = bench.workspace.connection\ncursor = connection.cursor()\ncursor.execute('DELETE FROM workbench_matter')",
    "sql = 'UPDATE ' + 'workbench_matter SET display_name=?'\nbench.workspace.connection.execute(sql)",
    "table = 'workbench_matter'\nbench.workspace.connection.execute(f'DELETE FROM {table}')",
    "connection = bench.workspace.connection\nconnection.executescript('SELECT 1; DELETE FROM workbench_matter;')",
    "connection = bench.workspace.connection\nconnection.execute(sql_from_file)",
    "connection = bench.workspace.connection\nconnection.execute(f'{sql_from_file}')",
    "def injected(connection, statement):\n    connection.execute(statement)\ninjected(bench.workspace.connection, \"UPDATE workbench_matter SET display_name='Synthetic'\")",
    "def injected(connection=bench.workspace.connection):\n    connection.execute(sql)\nmonkeypatch.setattr(worker, '_recover', injected)",
    "def injected(connection, statements):\n    connection.executemany(statements, parameters)\nmonkeypatch.setattr(worker, '_recover', injected)",
    "def injected(connection, statement):\n    execute = connection.execute\n    execute(statement)",
    "from sqlite3 import Connection as Database\ndef injected(connection: Database):\n    with connection:\n        pass",
    "import sqlite3 as db\ndef injected(connection: db.Connection):\n    connection.execute(statement)",
    "import sqlite3\nfrom case_intelligence.workspace_store import WorkspaceStore as Store\ndef injected(workspace: Store):\n    sqlite3.connect(workspace.path)",
    "import sqlite3\ndef injected(connection=sqlite3.connect(bench.workspace.path)):\n    pass",
    "def injected(*, connection=bench.workspace.connection):\n    with connection:\n        pass",
    "connection = bench.workspace.connection\nif flag:\n    connection = unrelated\nconnection.execute(statement)",
    "bench.workspace.connection.executescript(MIGRATION.read_text())",
    "connection = bench.workspace.connection\nexecute = connection.execute\nexecute('DELETE FROM workbench_matter')",
    "bench.workspace.connection.execute('-- Synthetic setup\\nDELETE FROM workbench_matter')",
    "bench.workspace.connection.execute('WITH selected AS (SELECT 1) DELETE FROM workbench_matter')",
    "import sqlite3\nsqlite3.connect(bench.workspace.path)",
    "import sqlite3 as db\nworkspace = bench.workspace\npath = workspace.path\ndb.connect(path)",
    "from sqlite3 import connect as open_db\nopen_db('workbench.sqlite')",
    "import sqlite3\nsqlite3.connect(tmp_path / 'runtime' / 'workbench.sqlite')",
    "bench.workspace.queue_media_job('synthetic')",
    "workspace = bench.workspace\nworkspace.import_media_transcript('synthetic')",
    "with fixture_write(other):\n    bench.workspace.connection.execute('DELETE FROM workbench_matter')",
    "with fixture_write(bench.workspace) as connection:\n    pass\nconnection.execute('DELETE FROM workbench_matter')",
    "with fixture_write(bench.workspace) as connection:\n    def later():\n        connection.execute('DELETE FROM workbench_matter')",
    "with fixture_write(bench.workspace):\n    callback = lambda: bench.workspace.connection.execute('DELETE FROM workbench_matter')",
])
def test_guard_rejects_unlocked_fixture_mutations(source):
    violations = check_fixture_writes(source, "synthetic_test.py")
    assert violations
    assert all("synthetic_test.py:" in str(item) and "fixture_write" in str(item) for item in violations)


@pytest.mark.parametrize("source", [
    "bench.workspace.connection.execute('SELECT * FROM workbench_matter')",
    "bench.workspace.connection.execute('PRAGMA integrity_check')",
    "bench.workspace.connection.execute(\"SELECT 'update', 'delete' FROM workbench_matter\")",
    "bench.workspace.connection.execute('SELECT * FROM workbench_matter -- UPDATE is a comment')",
    "workspace = bench.workspace\nwith fixture_write(workspace) as connection:\n    connection.execute('DELETE FROM workbench_matter')",
    "with fixture_write(bench.workspace):\n    bench.workspace.connection.execute('UPDATE workbench_matter SET display_name=?', ('Synthetic',))",
    "from tests.fixture_write import fixture_write as seed\nwith seed(bench.workspace):\n    bench.workspace.import_media_transcript('synthetic')",
    "import tests.fixture_write as fw\nwith fw.fixture_write(bench.workspace):\n    bench.workspace.import_media_transcript('synthetic')",
    "import sqlite3\nwith sqlite3.connect(backup_path) as destination:\n    destination.execute('SELECT 1')",
    "import sqlite3\nconnection = sqlite3.connect(':memory:')\nconnection.execute('SELECT 1')",
    "def first():\n    connection = bench.workspace.connection\ndef second():\n    connection = unrelated\n    connection.execute('SELECT 1')",
    "def execute(case):\n    return case.run()\ndef test_workflow(case):\n    execute(case)",
    "def test_workflow(case):\n    execute(case)\ndef execute(case):\n    return case.run()",
    "def test_workflow(case):\n    def execute(case):\n        return case.run()\n    execute(case)",
    "from tests.test_answer_context import execute\nexecute(case)",
    "from tests.test_answer_context import execute as workflow\nworkflow(case)",
])
def test_guard_accepts_locked_writes_and_reads(source):
    assert check_fixture_writes(source) == []


def test_statement_exemption_is_specific_and_does_not_cover_with_body():
    source = dedent('''\
        # fixture-write: Exercise a deliberately independent writer for contention.
        with bench.workspace.connection:
            bench.workspace.connection.execute('DELETE FROM workbench_matter')
    ''')
    violations = check_fixture_writes(source)
    assert [(item.line, item.rule) for item in violations] == [(3, "SQL fixture write outside fixture_write")]


def test_statement_exemption_accepts_a_reason_but_not_bare_suppression():
    assert not check_fixture_writes("bench.workspace.connection.execute('DELETE FROM workbench_matter')  # fixture-write: Offline migration intentionally constructs the previous schema.")
    assert check_fixture_writes("bench.workspace.connection.execute('DELETE FROM workbench_matter')  # fixture-write: ignore")
    assert check_fixture_writes("note = '# fixture-write: Offline migration intentionally constructs the previous schema.'\nbench.workspace.connection.execute('DELETE FROM workbench_matter')")


def test_inline_with_exception_does_not_exempt_its_body():
    source = dedent('''\
        with bench.workspace.connection:  # fixture-write: Exercise an intentional raw connection rollback.
            bench.workspace.connection.execute('DELETE FROM workbench_matter')
    ''')
    assert [item.line for item in check_fixture_writes(source)] == [2]


def test_exception_on_final_body_statement_does_not_exempt_outer_context():
    source = dedent('''\
        with bench.workspace.connection:
            bench.workspace.connection.execute('DELETE FROM workbench_matter')  # fixture-write: Exercise an intentional raw connection rollback.
    ''')
    assert [(item.line, item.rule) for item in check_fixture_writes(source)] == [(1, "naked connection context")]


def test_exception_applies_to_only_one_statement_on_a_semicolon_line():
    source = "bench.workspace.connection.execute('DELETE FROM workbench_matter'); bench.workspace.connection.execute('DELETE FROM workbench_matter')  # fixture-write: Exercise an intentional raw connection rollback."
    assert len(check_fixture_writes(source)) == 1


def test_exception_cannot_license_deferred_lambda_write():
    source = "callback = lambda: bench.workspace.connection.execute('DELETE FROM workbench_matter')  # fixture-write: Exercise an intentional raw connection rollback."
    assert check_fixture_writes(source)


def test_test_modules_observe_fixture_write_boundary():
    root = Path(__file__).parent
    violations = []
    for path in sorted(root.rglob("*.py")):
        # The implementation is the sole owner of the underlying raw boundary.
        if path == root / "fixture_write.py":
            continue
        violations.extend(check_fixture_writes(path.read_text(encoding="utf-8"), str(path.relative_to(root.parent))))
    assert not violations, "\n" + "\n".join(map(str, violations))
