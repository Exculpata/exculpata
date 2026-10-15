"""Synthetic persistence, legacy migration and clean backup/restore coverage."""
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workspace_store import WorkspaceStore, WorkspaceProblem
from tests.test_identity_membership_audit import _login
from tests.test_matter_archive import archive_case  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def principal(store, name="owner"):
    return store.upsert_principal("test", name, "Synthetic Reviewer", name,
                                  preferred_principal_id=f"principal-{name}").principal_id


@pytest.mark.parametrize("mode", ["dark", "light"])
@pytest.mark.parametrize("accent", ["cyan", "green", "amber"])
def test_preferences_are_independent_and_survive_reopen(tmp_path, mode, accent):
    path = tmp_path / "workspace.sqlite"
    store = WorkspaceStore(path)
    actor = principal(store)
    other = principal(store, "other")
    store.set_principal_accent(actor, accent)
    store.set_principal_theme(actor, mode)
    saved = store.principal_preference(actor)
    assert (saved.theme, saved.accent) == (mode, accent)
    assert store.principal_preference(other).accent == "cyan"
    store.close()
    store = WorkspaceStore(path)
    assert store.principal_preference(actor) == saved
    store.set_principal_theme(actor, "light" if mode == "dark" else "dark")
    assert store.principal_preference(actor).accent == accent
    store.set_principal_accent(actor, "green")
    assert store.principal_preference(actor).theme != mode
    for invalid in ("dusk", "retro", "purple", ""):
        with pytest.raises(WorkspaceProblem):
            store.set_principal_accent(actor, invalid)
    store.close()


def backup(source, destination):
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dest:
        src.backup(dest)
        assert dest.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert dest.execute("PRAGMA foreign_key_check").fetchall() == []


def test_legacy_dusk_migration_backup_clean_restore_and_rollback(tmp_path):
    path = tmp_path / "old.sqlite"
    store = WorkspaceStore(path)
    actor = principal(store)
    light = principal(store, "light")
    store.close()
    # Recreate the exact shipped preference schema, including its old CHECK.
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE workbench_principal_preference")
        conn.executescript((ROOT / "migrations/sqlite/0017_matter_retention_and_preferences.sql").read_text())
        conn.executemany("INSERT INTO workbench_principal_preference VALUES (?,?,?)",
                         [(actor, "dusk", "2026-01-01T00:00:00Z"), (light, "light", "2026-01-01T00:00:00Z")])
    snapshot = tmp_path / "before-backup.sqlite"
    backup(path, snapshot)
    path.unlink()  # The clean target cannot depend on the original database.
    restored = tmp_path / "restored.sqlite"
    backup(snapshot, restored)
    store = WorkspaceStore(restored)
    assert (store.principal_preference(actor).theme, store.principal_preference(actor).accent) == ("dark", "cyan")
    assert store.principal_preference(light).theme == "light"
    assert store.principal_preference(actor).updated_at == "2026-01-01T00:00:00Z"
    store.set_principal_accent(actor, "amber")
    saved = store.principal_preference(actor)
    store.close()
    after = tmp_path / "after-backup.sqlite"
    backup(restored, after)
    restored.unlink()
    clean = tmp_path / "clean.sqlite"
    backup(after, clean)
    store = WorkspaceStore(clean)
    assert store.principal_preference(actor) == saved
    store.close()
    # Matching old-reader rollback uses the pre-upgrade backup, not new data.
    rollback = tmp_path / "rollback.sqlite"
    backup(snapshot, rollback)
    with sqlite3.connect(rollback) as conn:
        assert conn.execute("SELECT theme FROM workbench_principal_preference WHERE principal_id=?", (actor,)).fetchone() == ("dusk",)
        assert "accent" not in {row[1] for row in conn.execute("PRAGMA table_info(workbench_principal_preference)")}
    name = "migrations/sqlite/0043_appearance_accents.sql"
    assert (ROOT / name).read_bytes() == (ROOT / "src/case_intelligence" / name).read_bytes()


def test_accent_route_persists_across_sessions_and_rejects_invalid_or_unprotected_writes(tmp_path):
    app = create_workbench_app(tmp_path / "runtime", auth_mode="preview", generator=UnavailableGenerator())
    with TestClient(app) as client:
        assert client.post("/preferences/accent", data={"accent": "green"}, follow_redirects=False).status_code in (303, 403)
        _, csrf = _login(client, "taylor-morgan", "/matters/new")
        assert client.post("/preferences/accent", data={"accent": "green"}).status_code == 403
        for accent in ("green", "amber", "cyan", "green"):
            response = client.post("/preferences/accent", data={"accent": accent, "csrf_token": csrf, "next": "//example.invalid"}, follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"] == "/"
            assert response.headers["X-Exculpata-Accent"] == accent
            assert f'data-accent="{accent}"' in client.get("/matters/new").text
        assert client.post("/preferences/theme", data={"theme": "light", "csrf_token": csrf}).status_code == 200
        for field, value in (("accent", "purple"), ("theme", "dusk")):
            assert client.post(f"/preferences/{field}", data={field: value, "csrf_token": csrf}).status_code == 400
        assert client.post("/auth/logout", data={"csrf_token": csrf}, follow_redirects=False).status_code == 303
        _, csrf = _login(client, "taylor-morgan", "/matters/new")
        page = client.get("/matters/new").text
        assert 'data-theme="light"' in page and 'data-accent="green"' in page
        client.post("/auth/logout", data={"csrf_token": csrf})
        _login(client, "jordan-lee", "/matters/new")
        page = client.get("/matters/new").text
        assert 'data-theme="dark"' in page and 'data-accent="cyan"' in page


def test_matter_archive_accepts_accent_schema_without_exporting_personal_preferences(archive_case):
    import zipfile

    from case_intelligence.matter_archive import (
        build_matter_archive, cleanup_archive, matter_snapshot_digest,
    )
    from case_intelligence.matter_archive_verifier import verify_matter_archive
    from tests.test_matter_archive import OWNER, OTHER, close

    bench, matter, source_store = archive_case
    workspace = bench.workspace
    workspace.set_principal_theme(OWNER, "light")
    workspace.set_principal_accent(OWNER, "amber")
    workspace.set_principal_accent(OTHER, "green")
    preference_timestamp = "2098-04-17T03:26:51Z"
    with workspace._lock, workspace.connection:
        workspace.connection.execute(
            "UPDATE workbench_principal_preference SET updated_at=?", (preference_timestamp,)
        )
    close(bench, matter)
    snapshot = matter_snapshot_digest(bench, matter, source_store)
    artifact = build_matter_archive(bench, matter, OWNER, source_store=source_store)
    try:
        manifest = verify_matter_archive(artifact.path, expected_sha256=artifact.sha256)
        coverage = manifest["coverage"]["workbench_principal_preference"]
        assert coverage["disposition"] == "Global UI preferences"
        assert "accent" in coverage["columns"]
        with zipfile.ZipFile(artifact.path) as archive:
            assert "records/workbench_principal_preference.jsonl" not in archive.namelist()
            material = b"\n".join(archive.read(name) for name in archive.namelist())
        for private_value in (preference_timestamp, "amber", "green"):
            assert private_value.encode() not in material
        # Personal UI changes must not invalidate the preserved matter's content snapshot.
        workspace.set_principal_theme(OWNER, "dark")
        workspace.set_principal_accent(OWNER, "cyan")
        assert matter_snapshot_digest(bench, matter, source_store) == snapshot
    finally:
        cleanup_archive(artifact)
