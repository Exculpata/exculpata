from __future__ import annotations

from tests.fixture_write import fixture_write

import sqlite3

import pytest

from case_intelligence.sqlite_store import SQLiteStore


def test_fresh_migration_foreign_keys_and_reopen(tmp_path):
    path = tmp_path / "slice1a.sqlite"
    store = SQLiteStore(path)
    assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    expected = {
        "schema_migration", "matter_catalog", "source_location", "scanner_binding",
        "windows_mapping", "source_file", "source_version", "scan_run",
        "scan_observation", "job", "job_attempt", "representation", "segment",
    }
    actual = {row[0] for row in store.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert expected <= actual
    store.close()

    reopened = SQLiteStore(path)
    assert [row[0] for row in reopened.connection.execute(
        "SELECT version FROM schema_migration"
    )] == [1]
    reopened.register_matter("matter-alpha", "Synthetic Alpha")
    with pytest.raises(sqlite3.IntegrityError):
        with fixture_write(reopened):
            reopened.connection.execute(
                "INSERT INTO source_location(source_location_id,matter_id,display_name,enabled,catalog_revision) VALUES(?,?,?,?,?)",
                ("bad-location", "unknown", "bad", 1, 0),
            )
    reopened.close()
