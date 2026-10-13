"""Atomic, idempotent migration from date-driven deletion to explicit exits."""
from importlib import resources
import sqlite3

from .case_lifecycle_guards import install_content_guards


def migrate_case_lifecycle(connection: sqlite3.Connection) -> None:
    """Run before any backfill, maintenance or worker can claim old deadlines."""
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workbench_case_policy'"
        ).fetchone()
        if exists:
            guards = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name IN "
                "('workbench_case_writer_insert','workbench_case_writer_update','workbench_case_writer_delete')"
            ).fetchone()[0]
            if guards != 3:
                raise RuntimeError('Case lifecycle writer guards are missing; restore a verified backup.')
            if connection.execute(
                'SELECT 1 FROM workbench_case_policy WHERE policy_version NOT IN (1,2) LIMIT 1'
            ).fetchone():
                raise RuntimeError('Unsupported case lifecycle policy version.')
            migrate_exit_authority(connection)
            install_content_guards(connection)
            return
        source = resources.files('case_intelligence').joinpath(
            'migrations/sqlite/0040_case_lifecycle.sql').read_text(encoding='utf-8')
        original_rename_mode = connection.execute('PRAGMA legacy_alter_table').fetchone()[0]
        statement = ''
        try:
            for line in source.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    connection.execute(statement)
                    statement = ''
            if statement.strip():
                raise RuntimeError('Incomplete case lifecycle migration.')
            migrate_exit_authority(connection)
            install_content_guards(connection)
        finally:
            connection.execute(f'PRAGMA legacy_alter_table = {int(original_rename_mode)}')


def migrate_exit_authority(connection):
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='workbench_case_purge_authority'").fetchone():
        return
    source = resources.files('case_intelligence').joinpath(
        'migrations/sqlite/0041_case_exit_authority.sql').read_text(encoding='utf-8')
    statement = ''
    for line in source.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ''
    if statement.strip():
        raise RuntimeError('Incomplete exit authority migration.')
