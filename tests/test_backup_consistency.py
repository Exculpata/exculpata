"""Synthetic split-filesystem backup boundaries; no running services needed."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import recordbench_backup as backup  # noqa: E402


def _private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def _version(path: Path) -> str:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        return connection.execute("SELECT version FROM synthetic_state").fetchone()[0]
    finally:
        connection.close()


class SyntheticNode:
    def __init__(self, root: Path, monkeypatch, *, wal: bool = False,
                 node_path: Path | None = None, storage_path: Path | None = None, names: str = "recordbench"):
        self.root = root
        self.names = names
        self.restic_arguments = []
        self.node = node_path or root / "node"
        self.storage = storage_path or root / "separate-storage"
        self.archive = root / "archive"
        self.events: list[str] = []
        self.corrupt_at_stop = False
        self.fail_dump = False
        self.projected_version = "initial"
        self.connections = []
        for name in ("runtime", "config", "secrets", "state"):
            (self.node / name).mkdir(parents=True, exist_ok=True)
        self.storage.mkdir(exist_ok=storage_path is not None)
        from case_intelligence.managed_storage import ManagedMatterStorage
        with monkeypatch.context() as naming:
            naming.setenv("EXCULPATA_STORAGE_NAMES", names)
            ManagedMatterStorage.initialize(self.storage)
        repository = root / "repository"
        repository.mkdir()
        (repository / "config").write_text("synthetic repository stub")
        _private_json(self.node / "installation.json", {
            "format_version": 1, "auth": "local", "profiles": [],
            "release_path": str(ROOT), "release_id": "synthetic-release",
            "node_id": "synthetic-node",
        })
        _private_json(self.node / "config/backup.json", {
            "format_version": 1, "repository": str(repository),
            "recovery_key_output": str(root / "recovery-key"), "retention": "14d",
        })
        environment = {
            "RECORDBENCH_RUNTIME_ROOT": str(self.node / "runtime"),
            "RECORDBENCH_CONFIG_ROOT": str(self.node / "config"),
            "RECORDBENCH_SECRETS_ROOT": str(self.node / "secrets"),
            "RECORDBENCH_STORAGE_ROOT": str(self.storage),
        }
        environment = {key.replace("RECORDBENCH_", names.upper() + "_"): value for key, value in environment.items()}
        env = self.node / "compose.env"
        env.write_text("".join(f"{key}={json.dumps(value)}\n" for key, value in environment.items()))
        env.chmod(0o600)
        password = self.node / "secrets/restic-password"
        password.write_text("synthetic credential for a mocked repository")
        password.chmod(0o600)
        self.control = self.node / "runtime/workbench.sqlite"
        self.registry = self.storage / "matters/synthetic-matter/sources/source-registry.sqlite3"
        for path in (self.control, self.registry):
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path)
            if wal and path == self.control:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("CREATE TABLE synthetic_state(version TEXT NOT NULL)")
            connection.execute("INSERT INTO synthetic_state VALUES ('initial')")
            connection.commit()
            self.connections.append(connection)
        self.original_run = backup._run
        monkeypatch.setattr(backup, "_run", self.run)
        monkeypatch.setattr(backup, "_restic", self.restic)
        monkeypatch.setattr(backup, "_running_services", lambda _: frozenset({"app", "gateway"}))
        monkeypatch.setattr(backup, "_health", lambda _: {
            "product": "RecordBench",
            **{key: {"queued": 0, "running": 0} for key in backup.ACTIVE_JOB_KEYS},
        })

    def close(self):
        for connection in self.connections:
            connection.close()

    def run(self, command, **kwargs):
        if command[0] == "cp":
            return self.original_run(command, **kwargs)
        if "stop" in command:
            self.events.append("stop")
            # A request admitted after the initial health probe finishes at stop.
            for connection in self.connections:
                connection.execute("UPDATE synthetic_state SET version='frozen'")
                connection.commit()
            self.projected_version = "frozen"
            if self.corrupt_at_stop:
                self.connections[1].close()
                self.registry.write_bytes(b"synthetic damaged database")
        elif "up" in command:
            self.events.append("restart")
            for index, connection in enumerate(self.connections):
                if self.corrupt_at_stop and index == 1:
                    continue
                connection.execute("UPDATE synthetic_state SET version='live'")
                connection.commit()
        elif "pg_dump" in command:
            self.events.append("dump")
            if self.fail_dump:
                raise backup.BackupError("synthetic projection failure")
            return subprocess.CompletedProcess(command, 0, self.projected_version.encode())
        elif "pg_restore" not in command:
            raise AssertionError("unexpected external command")
        return subprocess.CompletedProcess(command, 0, b"")

    def restic(self, config, password, arguments, **kwargs):
        arguments = tuple(arguments)
        self.restic_arguments.append(arguments)
        action = arguments[0]
        if action == "backup":
            self.events.append("transfer")
            payload, storage = map(Path, arguments[-2:])
            assert storage not in payload.parents and payload not in storage.parents
            shutil.copytree(payload, self.archive / "payload")
            shutil.copytree(storage, self.archive / "managed-storage", symlinks=True)
            output = json.dumps({"message_type": "summary", "snapshot_id": "a" * 64}).encode()
            return subprocess.CompletedProcess(arguments, 0, output)
        if action == "restore":
            target = Path(arguments[arguments.index("--target") + 1])
            shutil.copytree(self.archive, target, dirs_exist_ok=True, symlinks=True)
        return subprocess.CompletedProcess(arguments, 0, b"")

    def backup(self):
        return backup.backup(argparse.Namespace(node_root=self.node, dry_run=False))

    def restore(self):
        return backup.restore(argparse.Namespace(
            node_root=self.node, target=self.root / "restored", snapshot="a" * 64,
        ))


@pytest.fixture
def node_factory(tmp_path, monkeypatch):
    nodes = []

    def create(**kwargs):
        node = SyntheticNode(tmp_path, monkeypatch, **kwargs)
        nodes.append(node)
        return node

    yield create
    for node in nodes:
        node.close()


def test_browser_accounts_are_in_snapshot_and_clean_restore(node_factory, monkeypatch):
    from case_intelligence.identity import LocalAccountSettings
    from case_intelligence.local_accounts import LocalAccountRepository
    node = node_factory()
    repository = LocalAccountRepository(node.node / "accounts/local-accounts.json")
    repository.initialize("synthetic.admin", "Synthetic Administrator", "synthetic-backup-password", actor="synthetic-operator")
    record = json.loads((node.node / "installation.json").read_text())
    record["local_account_management"] = True
    _private_json(node.node / "installation.json", record)
    with (node.node / "compose.env").open("a") as stream:
        stream.write(f"RECORDBENCH_LOCAL_ACCOUNT_ROOT={json.dumps(str(repository.path.parent))}\n")
    before = repository.path.read_bytes()
    same_filesystem = backup._same_filesystem
    def reject_account_hardlink_boundary(roots):
        assert repository.path.parent not in roots
        return same_filesystem(roots)
    monkeypatch.setattr(backup, "_same_filesystem", reject_account_hardlink_boundary)
    assert node.backup() == 0
    assert (node.archive / "payload/accounts/local-accounts.json").read_bytes() == before
    assert node.restore() == 0
    restored = node.root / "restored/payload/accounts/local-accounts.json"
    settings = LocalAccountSettings(restored, management_root=restored.parent)
    assert settings.authenticate("synthetic.admin", "synthetic-backup-password") is not None


def test_managed_registry_is_frozen_before_restart(node_factory):
    node = node_factory()
    assert node.backup() == 0
    restored_registry = node.archive / "managed-storage" / node.registry.relative_to(node.storage)
    assert _version(node.registry) == "live"
    assert _version(restored_registry) == "frozen"
    assert node.events.index("restart") < node.events.index("transfer")
    assert not list(node.node.glob(".recordbench-backup-snapshot-*"))
    assert not list(node.storage.parent.glob(".recordbench-storage-snapshot-*"))


def test_wal_control_is_consolidated_before_restart(node_factory):
    node = node_factory(wal=True)
    assert node.backup() == 0
    frozen = node.archive / "payload/runtime/workbench.sqlite"
    assert _version(node.control) == "live"
    assert _version(frozen) == "frozen"
    assert not frozen.with_name(frozen.name + "-wal").exists()
    assert not frozen.with_name(frozen.name + "-shm").exists()


def test_projection_and_stores_share_the_stopped_boundary(node_factory):
    node = node_factory()
    assert node.backup() == 0
    assert (node.archive / "payload/postgres/review-index.dump").read_text() == "frozen"
    assert node.events.index("stop") < node.events.index("dump") < node.events.index("restart")


def test_corrupt_managed_registry_refuses_backup_and_restarts_app(node_factory):
    node = node_factory()
    node.corrupt_at_stop = True
    with pytest.raises(backup.BackupError, match="SQLite"):
        node.backup()
    assert node.events.count("restart") == 1
    assert "transfer" not in node.events
    assert json.loads((node.node / "state/backup-status.json").read_text())["state"] == "failed"
    assert not list(node.node.glob(".recordbench-backup-snapshot-*"))
    assert not list(node.storage.parent.glob(".recordbench-storage-snapshot-*"))


def test_restore_validates_the_managed_registry(node_factory):
    node = node_factory()
    assert node.backup() == 0
    registry = node.archive / "managed-storage" / node.registry.relative_to(node.storage)
    registry.write_bytes(b"synthetic damaged restored registry")
    with pytest.raises(backup.BackupError, match="SQLite"):
        node.restore()
    assert not (node.root / "restored/RESTORE_DRILL_VERIFIED.json").exists()
    assert node.events.count("restart") == 1


def test_restore_requires_managed_storage_boundary(node_factory):
    node = node_factory()
    assert node.backup() == 0
    shutil.rmtree(node.archive / "managed-storage")
    with pytest.raises(backup.BackupError, match="managed storage"):
        node.restore()
    assert not (node.root / "restored/RESTORE_DRILL_VERIFIED.json").exists()


def test_restore_detects_missing_registry_even_with_storage_marker(node_factory):
    node = node_factory()
    assert node.backup() == 0
    (node.archive / "managed-storage" / node.registry.relative_to(node.storage)).unlink()
    with pytest.raises(backup.BackupError, match="SQLite inventory"):
        node.restore()


def test_restore_receipt_counts_control_and_managed_stores(node_factory):
    node = node_factory(wal=True)
    assert node.backup() == 0
    assert node.restore() == 0
    target = node.root / "restored"
    receipt = json.loads((target / "RESTORE_DRILL_VERIFIED.json").read_text())
    assert receipt["sqlite_stores"] == 2
    assert receipt["managed_sqlite_stores"] == 1
    assert _version(target / "payload/runtime/workbench.sqlite") == "frozen"
    assert _version(target / "managed-storage" / node.registry.relative_to(node.storage)) == "frozen"
    assert _version(node.control) == "live"
    assert _version(node.registry) == "live"


@pytest.mark.parametrize("active_key,state", [("research_jobs", "queued"), ("report_compilation_jobs", "queued"), ("report_compilation_jobs", "running")])
def test_active_work_still_defers_without_stopping_services(node_factory, monkeypatch, active_key, state):
    node = node_factory()
    monkeypatch.setattr(backup, "_health", lambda _: {
        "product": "RecordBench",
        **{key: {"queued": int(key == active_key and state == "queued"), "running": int(key == active_key and state == "running")} for key in backup.ACTIVE_JOB_KEYS},
    })
    assert node.backup() == 0
    assert node.events == []
    assert json.loads((node.node / "state/backup-status.json").read_text())["state"] == "deferred"


def test_stopped_projection_failure_restarts_and_cleans_up(node_factory):
    node = node_factory()
    node.fail_dump = True
    with pytest.raises(backup.BackupError, match="synthetic projection failure"):
        node.backup()
    assert node.events == ["stop", "dump", "restart"]
    assert not list(node.node.glob(".recordbench-backup-snapshot-*"))
    assert not list(node.storage.parent.glob(".recordbench-storage-snapshot-*"))


def test_snapshot_does_not_follow_managed_symlinks(node_factory):
    node = node_factory()
    outside = node.root / "outside.sqlite"
    outside.write_bytes(b"synthetic external original")
    (node.storage / "linked.sqlite").symlink_to(outside)
    with pytest.raises(backup.BackupError, match="symbolic link"):
        node.backup()
    assert outside.read_bytes() == b"synthetic external original"
    assert "transfer" not in node.events
    assert node.events.count("restart") == 1


@pytest.mark.skipif(
    os.environ.get("RECORDBENCH_BACKUP_INTEGRATION") != "1",
    reason="opt-in synthetic restic/PostgreSQL drill",
)
@pytest.mark.parametrize("names", ["recordbench", "exculpata"])
def test_encrypted_split_snapshot_and_postgres_restore(node_factory, monkeypatch, names):
    """Real repository and dump/import; simulated app stop/restart only.

    Requires restic and an already installed pgvector image. No image pull,
    published port, live volume, or existing service is used.
    """
    assert shutil.which("restic") and shutil.which("docker")
    original_restic = backup._restic
    node = node_factory(wal=True, names=names)
    monkeypatch.setattr(backup, "_restic", original_restic)
    config = backup._backup_config(node.node)
    password = node.node / "secrets/restic-password"
    (Path(config["repository"]) / "config").unlink()  # Replace the fixture stub only.
    original_restic(config, password, ("init", "--repository-version", "2"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    image = subprocess.run(
        ["docker", "image", "inspect", backup.POSTGRES_IMAGE, "--format", "{{.Id}}"],
        capture_output=True, check=True, text=True,
    ).stdout.strip()
    container = subprocess.run([
        "docker", "run", "--detach", "--rm", "--pull=never", "--network", "none",
        "--read-only", "--log-driver", "none", "--memory", "512m", "--cpus", "1",
        "--tmpfs", "/var/lib/postgresql/data:rw,nosuid,nodev,size=256m",
        "--tmpfs", "/var/run/postgresql:rw,nosuid,nodev,size=16m",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=16m",
        "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_USER=recordbench",
        "-e", "POSTGRES_DB=recordbench", image, "postgres", "-c", "listen_addresses=",
    ], capture_output=True, check=True, text=True).stdout.strip()

    def execute(*command, input_bytes=None):
        return subprocess.run(
            ["docker", "exec", "-i", container, *command], input=input_bytes,
            capture_output=True, check=True, timeout=30,
        )

    def sql(statement, database="recordbench"):
        return execute(
            "psql", "-X", "-U", "recordbench", "-d", database, "-v", "ON_ERROR_STOP=1", "-At",
            input_bytes=statement.encode(),
        ).stdout.decode().strip()

    try:
        for attempt in range(60):
            ready = subprocess.run(
                ["docker", "exec", container, "pg_isready", "-U", "recordbench"],
                capture_output=True, timeout=10,
            )
            main_process = subprocess.run(
                ["docker", "exec", container, "cat", "/proc/1/comm"],
                capture_output=True, timeout=10,
            )
            if ready.returncode == 0 and main_process.stdout.strip() == b"postgres":
                break
            time.sleep(0.5)
        else:
            pytest.fail("isolated PostgreSQL did not become ready")
        sql("CREATE TABLE synthetic_state(version text); INSERT INTO synthetic_state VALUES ('initial');")

        def commands(command, **kwargs):
            if "pg_dump" in command:
                node.events.append("dump")
                return execute("pg_dump", "-U", "recordbench", "-d", "recordbench", "--format=custom")
            if "pg_restore" in command:
                return execute("pg_restore", "--list", input_bytes=kwargs["input_bytes"])
            result = node.run(command, **kwargs)
            if "stop" in command:
                sql("UPDATE synthetic_state SET version='frozen';")
            elif "up" in command:
                sql("UPDATE synthetic_state SET version='live';")
            return result

        monkeypatch.setattr(backup, "_run", commands)
        assert node.backup() == 0
        original_restic(config, password, ("check", "--read-data"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        receipt = json.loads((node.node / "state/backup-status.json").read_text())
        snapshots = json.loads(original_restic(config, password, ("snapshots", "--json"), stdout=subprocess.PIPE).stdout)
        assert len(snapshots) == 1
        assert names in snapshots[0]["tags"]
        other = "recordbench" if names == "exculpata" else "exculpata"
        assert other not in snapshots[0]["tags"]
        target = node.root / "restored"
        assert backup.restore(argparse.Namespace(
            node_root=node.node, target=target, snapshot="latest",
        )) == 0
        files = backup._snapshot_files(target)
        databases = backup._sqlite_files(files)
        assert len(databases) == 2
        assert all(_version(path) == "frozen" for path in databases)
        dump, = [path for path in files if path.name == "review-index.dump"]
        sql("CREATE DATABASE restored_recordbench;")
        execute(
            "pg_restore", "--exit-on-error", "--no-owner", "--no-privileges",
            "-U", "recordbench", "-d", "restored_recordbench", input_bytes=dump.read_bytes(),
        )
        assert sql("SELECT version FROM synthetic_state;", "restored_recordbench") == "frozen"
        assert sql("SELECT version FROM synthetic_state;") == "live"
        assert _version(node.control) == _version(node.registry) == "live"
        execute("pg_amcheck", "-U", "recordbench", "-d", "restored_recordbench", "--install-missing")
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True, check=True)


def test_selection_receipts_round_trip_through_normal_backup_restore(node_factory):
    from case_intelligence.intake_receipts import IntakeReceipts
    from case_intelligence.workspace_store import WorkspaceStore

    node = node_factory(wal=True)
    owner = 'generated-receipt-owner'
    with_store = WorkspaceStore(node.control)
    try:
        with_store.upsert_principal('test', owner, 'Generated Owner', owner, preferred_principal_id=owner)
        matter = with_store.create_matter('Generated receipt backup', '', owner)
        receipts = IntakeReceipts(with_store)
        receipt = receipts.create(matter.matter_id, owner, selection_key='a'*32,
            selection_fingerprint='b'*64, selected_count=2, eligible_indexes=[0], collection_name='Generated selection')
        receipts.append(matter.matter_id, owner, receipt['receipt_id'], start=0,
            files=[{'name': 'record.txt', 'relative_path': 'Folder/record.txt', 'size': 48},
                   {'name': 'opaque.bin', 'relative_path': 'Skipped/opaque.bin', 'size': 4}],
            reviewed_states=['valid', 'unsupported'], document_limit=1024, media_limit=1024,
            malware_scan_mode='off', scanner_ready=True)
        receipts.seal(matter.matter_id, owner, receipt['receipt_id'])
        session, items = with_store.create_upload_session(matter.matter_id, owner, 'Generated selection',
            [{'display_name': 'record.txt', 'relative_path': 'Folder/record.txt', 'media_type': 'text/plain', 'expected_size': 48}],
            intake_receipt_id=receipt['receipt_id'], intake_ordinals=[0])
        with_store.set_upload_item_offset(matter.matter_id, owner, session.upload_session_id, items[0].upload_item_id, 0, 7)
        selected_before = receipts.snapshot(matter.matter_id, owner, receipt['receipt_id'])
        charged_before = with_store.connection.execute('SELECT metadata_bytes FROM workbench_intake_receipt '
            'WHERE receipt_id=?', (receipt['receipt_id'],)).fetchone()[0]
        assert charged_before > 0
        partial = node.storage / 'matters' / matter.matter_id / 'sources' / 'incoming' / items[0].upload_item_id
        partial.parent.mkdir(parents=True)
        partial.write_bytes(b'Partial')
    finally:
        with_store.close()
    assert node.backup() == 0
    assert node.restore() == 0
    restored = node.root / 'restored'
    reopened = WorkspaceStore(restored / 'payload/runtime/workbench.sqlite')
    try:
        assert IntakeReceipts(reopened).snapshot(matter.matter_id, owner, receipt['receipt_id']) == selected_before
        assert reopened.connection.execute('SELECT metadata_bytes FROM workbench_intake_receipt '
            'WHERE receipt_id=?', (receipt['receipt_id'],)).fetchone()[0] == charged_before
        assert (restored / 'managed-storage' / partial.relative_to(node.storage)).read_bytes() == b'Partial'
        assert reopened.connection.execute('PRAGMA foreign_key_check').fetchall() == []
    finally:
        reopened.close()


def test_upload_occurrences_and_pending_identity_survive_normal_backup_restore(node_factory):
    from case_intelligence.pilot_uploads import PilotStore
    from case_intelligence.workspace_store import WorkspaceStore

    node = node_factory(wal=True)
    control = WorkspaceStore(node.control)
    owner = 'generated-occurrence-owner'
    control.upsert_principal('test', owner, 'Generated owner', owner, preferred_principal_id=owner)
    matter = control.create_matter('Generated occurrence backup', '', owner)
    source_root = node.storage / 'matters' / matter.matter_id / 'sources'
    source_root.parent.mkdir(parents=True)
    sources = PilotStore(source_root)
    body = b'Synthetic repeated occurrence for backup.\n'
    saved = []
    items = []
    try:
        for index in range(2):
            session, created = control.create_upload_session(matter.matter_id, owner, f'Generated production {index + 1}',
                [{'display_name': 'report.txt', 'relative_path': 'Records/report.txt', 'media_type': 'text/plain', 'expected_size': len(body)}])
            item = created[0]
            items.append(item)
            sources.append_resumable_chunk(item.upload_item_id, offset=0, expected_size=len(body), chunk=body)
            control.set_upload_item_offset(matter.matter_id, owner, session.upload_session_id, item.upload_item_id, 0, len(body))
            document = sources.finalize_resumable_upload(item.upload_item_id, display_name='report.txt',
                relative_path='Records/report.txt', content_type='text/plain', expected_size=len(body))
            saved.append((document.document_id, document.version_id, document.name_key))
        assert saved[0][0] != saved[1][0]
        # Both sources are durable immediately before their control commit.
    finally:
        sources.close()
        control.close()
    assert node.backup() == 0 and node.restore() == 0
    restored = node.root / 'restored'
    current = PilotStore(restored / 'managed-storage' / source_root.relative_to(node.storage))
    reopened = WorkspaceStore(restored / 'payload/runtime/workbench.sqlite')
    try:
        assert len(current.documents) == 2
        for item, expected in zip(items, saved):
            document = current.finalize_resumable_upload(item.upload_item_id, display_name='report.txt',
                relative_path='Records/report.txt', content_type='text/plain', expected_size=len(body))
            assert (document.document_id, document.version_id, document.name_key) == expected
            assert current.source_path(document.document_id).read_bytes() == body
            assert reopened.upload_item(matter.matter_id, owner, item.upload_session_id, item.upload_item_id).received_size == len(body)
        assert len(current.documents) == 2
    finally:
        current.close()
        reopened.close()


def test_exact_byte_comparisons_restore_and_rebuild_from_received_registry(node_factory):
    from fastapi.testclient import TestClient
    from case_intelligence.generation import UnavailableGenerator
    from case_intelligence.workbench import create_workbench_app
    from case_intelligence.workspace_store import WorkspaceStore
    from tests.test_intake_receipt_http import ACTOR
    from tests.test_exact_byte_matches import matched_sources

    node = node_factory(wal=True)
    with TestClient(create_workbench_app(node.node / 'runtime', generator=UnavailableGenerator(),
            auth_mode='test', background_ingestion=True, ingestion_workers=1)) as client:
        response = client.post('/matters', data={'name': 'Generated byte match backup'}, follow_redirects=False)
        slug = response.headers['location'].split('/')[2]
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        documents = matched_sources(client, bench, matter)
        saved = [(d.document_id, d.version_id, d.digest, d.size) for d in documents]
        token = bench.source_store(matter).action_token(documents[0])
        assert bench.source_library(matter, same_content=token).total == 2
    assert node.backup() == 0 and node.restore() == 0
    restored_runtime = node.root / 'restored/payload/runtime'
    restored = WorkspaceStore(restored_runtime / 'workbench.sqlite')
    try:
        assert restored.source_catalog_page(matter.matter_id, same_content=token).total == 2
        assert restored.connection.execute('PRAGMA foreign_key_check').fetchall() == []
        # Discard only this synthetic derived lookup to prove that normal opening
        # rebuilds it from restored received source identities and bytes.
        with restored.connection:
            restored.connection.execute('DELETE FROM workbench_source_byte_match WHERE matter_id=?', (matter.matter_id,))
    finally:
        restored.close()
    with TestClient(create_workbench_app(restored_runtime, generator=UnavailableGenerator(),
            auth_mode='test', background_ingestion=False)) as client:
        bench = client.app.state.workbench
        store = bench.source_store(bench.matter(slug, ACTOR))
        assert [(store.get(identity).document_id, store.get(identity).version_id,
            store.get(identity).digest, store.get(identity).size) for identity, *_ in saved] == saved
        assert bench.source_library(matter, same_content=token).total == 2
        assert store.source_path(saved[0][0]).read_bytes() == store.source_path(saved[1][0]).read_bytes()
        assert store.source_path(saved[0][0]).read_bytes() != store.source_path(saved[2][0]).read_bytes()


def test_installer_prepared_nested_storage_round_trips_to_clean_target(tmp_path, node_factory):
    import stat
    import recordbench_install as installer

    base = tmp_path.resolve()
    node_path = base / "created-node-parent" / "nested" / "node"
    storage_path = base / "created-storage-parent" / "nested" / "matters"
    old_umask = os.umask(0)
    try:
        paths = installer._prepare_directories(installer.Console(color=False, quiet=True),
            node_path, storage_root=storage_path, resume=False, dry_run=False)
    finally:
        os.umask(old_umask)
    for leaf in (node_path, *paths.values()):
        for directory in (leaf, *leaf.parents):
            if directory == base:
                break
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    node = node_factory(wal=True, node_path=node_path, storage_path=storage_path)
    assert node.backup() == 0
    target = node.root / "restored"
    assert not target.exists()
    assert node.restore() == 0
    receipt = json.loads((target / "RESTORE_DRILL_VERIFIED.json").read_text())
    assert receipt["sqlite_stores"] == 2
    assert receipt["managed_sqlite_stores"] == 1
    assert _version(target / "payload/runtime/workbench.sqlite") == "frozen"
    assert _version(target / "managed-storage" / node.registry.relative_to(node.storage)) == "frozen"
    assert (target / "payload/postgres/review-index.dump").read_text() == "frozen"
    assert _version(node.control) == "live"
    assert _version(node.registry) == "live"


def _enable_browser_accounts(node):
    from case_intelligence.local_accounts import LocalAccountRepository
    repo = LocalAccountRepository(node.node / "accounts/local-accounts.json")
    repo.initialize("synthetic.admin", "Synthetic Administrator", "synthetic-backup-password", actor="synthetic-operator")
    record = json.loads((node.node / "installation.json").read_text())
    record["local_account_management"] = True
    _private_json(node.node / "installation.json", record)
    with (node.node / "compose.env").open("a") as stream:
        stream.write(f"RECORDBENCH_LOCAL_ACCOUNT_ROOT={json.dumps(str(repo.path.parent))}\n")
    return repo


def _damage_accounts(path, damage):
    if damage == "missing":
        path.unlink()
    elif damage == "mode":
        path.chmod(0o644)
    elif damage == "directory-mode":
        path.parent.chmod(0o755)
    elif damage == "malformed":
        path.write_bytes(b"synthetic invalid account JSON")
    elif damage == "no-administrator":
        payload = json.loads(path.read_bytes())
        payload["accounts"][0]["roles"] = []
        path.write_text(json.dumps(payload))
    elif damage == "invalid-hash":
        payload = json.loads(path.read_bytes())
        payload["accounts"][0]["password_hash"] = "$argon2id$v=19$m=0,t=0,p=0$c2FsdA$aGFzaA"
        path.write_text(json.dumps(payload))


@pytest.mark.parametrize("damage", ["missing", "mode", "directory-mode", "malformed", "no-administrator", "invalid-hash"])
def test_invalid_browser_account_store_blocks_backup_before_transfer(node_factory, damage):
    node = node_factory()
    repo = _enable_browser_accounts(node)
    _damage_accounts(repo.path, damage)
    with pytest.raises(backup.BackupError, match="local account snapshot"):
        node.backup()
    assert "restart" in node.events and "transfer" not in node.events
    assert not node.archive.exists()
    assert not list(node.node.glob(".recordbench-backup-snapshot-*"))
    assert json.loads((node.node / "state/backup-status.json").read_text())["state"] == "failed"


def test_account_backup_validates_frozen_copy_before_restart(node_factory, monkeypatch):
    node = node_factory()
    repo = _enable_browser_accounts(node)
    original = shutil.copytree
    def damaged_copy(source, destination, *args, **kwargs):
        result = original(source, destination, *args, **kwargs)
        if Path(source) == repo.path.parent:
            assert "restart" not in node.events
            (Path(destination) / "local-accounts.json").write_bytes(b"synthetic damaged frozen copy")
        return result
    monkeypatch.setattr(backup.shutil, "copytree", damaged_copy)
    with pytest.raises(backup.BackupError, match="local account snapshot"):
        node.backup()
    assert "restart" in node.events and "transfer" not in node.events
    assert repo.read()["synthetic.admin"].enabled


@pytest.mark.parametrize("damage", ["missing", "mode", "directory-mode", "malformed", "no-administrator", "invalid-hash"])
def test_restore_revalidates_account_contents_beyond_snapshot_checksums(node_factory, damage):
    node = node_factory()
    _enable_browser_accounts(node)
    assert node.backup() == 0
    account = node.archive / "payload/accounts/local-accounts.json"
    _damage_accounts(account, damage)
    backup._hash_controls(node.archive / "payload")
    with pytest.raises(backup.BackupError, match="local account snapshot"):
        node.restore()
    assert not (node.root / "restored/RESTORE_DRILL_VERIFIED.json").exists()

def test_full_text_ledger_and_budget_survive_normal_backup_restore(node_factory):
    from case_intelligence.full_text_review import FullTextReviewLedger
    from case_intelligence.pilot_uploads import PilotUnit
    from case_intelligence.workspace_store import WorkspaceStore

    node = node_factory(wal=True)
    owner = 'synthetic-text-owner'
    store = WorkspaceStore(node.control)
    try:
        store.upsert_principal('test', owner, 'Synthetic Reviewer', owner, preferred_principal_id=owner)
        matter = store.create_matter('Synthetic full-text backup', '', owner)
        store.upsert_source_catalog(matter.matter_id, [dict(
            document_id='a'*32,version_id='c'*32,action_token='d'*32,
            display_name='Synthetic source',relative_path='synthetic.txt',media_type='text/plain',kind='TXT',
            source_state='ready',tone='ready',state_label='Searchable',count_label='2 units',processing_stage='',
            completed_units=2,total_units=2,page_count=0,duration_ms=0,byte_size=20,origin='upload',retryable=False,
            removable=True,has_video=False,content_basis_digest='b'*64)])
        _,version=store.create_review_criterion(matter.matter_id,owner,title='Synthetic rule',instructions='Find the amber bicycle.')
        store.queue_review_run(matter.matter_id,owner,version.criterion_version_id,run_kind='full',review_mode='full_text')
        run=store.claim_review_run('synthetic-backup-worker')
        decision=store.next_review_decision(run.run_id)
        ledger=FullTextReviewLedger(store)
        ledger.inventory(run,decision,[PilotUnit(1,'Synthetic completed text'),PilotUnit(2,'Synthetic pending text')],current_source=lambda:True)
        ledger.charge_call(run)
        chunk=ledger.unit_chunks(run.run_id,decision.document_id,1)[0]
        ledger.record(run,decision,chunk,state='processed',label='exclude',current_source=lambda:True)
        store.fail_review_run(run.run_id,'Synthetic checkpoint for backup')
        before=ledger.coverage(matter.matter_id,owner,run.run_id)
    finally:
        store.close()
    assert node.backup()==0
    assert node.restore()==0
    restored=WorkspaceStore(node.root/'restored/payload/runtime/workbench.sqlite')
    try:
        ledger=FullTextReviewLedger(restored)
        assert ledger.coverage(matter.matter_id,owner,run.run_id)==before
        assert restored.connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert restored.connection.execute('PRAGMA foreign_key_check').fetchall()==[]
        ledger.delete(matter.matter_id,owner,run.run_id)
        assert restored.connection.execute('SELECT count(*) FROM workbench_text_review_budget').fetchone()[0]==0
        assert restored.connection.execute('SELECT count(*) FROM workbench_text_review_counter').fetchone()[0]==0
        assert restored.connection.execute('PRAGMA foreign_key_check').fetchall()==[]
    finally:
        restored.close()


@pytest.mark.parametrize("names", ["recordbench", "exculpata"])
def test_backup_and_restore_preserve_generation_and_accept_both_tags(node_factory, names):
    node = node_factory(names=names)
    before = (node.node / "compose.env").read_bytes()
    assert node.backup() == 0
    assert node.restore() == 0
    marker = f".{names}-managed-storage.json"
    assert (node.root / "restored/managed-storage" / marker).read_bytes() == (node.storage / marker).read_bytes()
    assert (node.root / "restored/payload/control/compose.env").read_bytes() == before
    assert (node.node / "compose.env").read_bytes() == before
    transfer = next(args for args in node.restic_arguments if args[0] == "backup")
    retention = next(args for args in node.restic_arguments if args[0] == "forget")
    restore = next(args for args in node.restic_arguments if args[0] == "restore")
    assert transfer[transfer.index("--tag") + 1] == names
    assert retention[retention.index("--tag") + 1] == names
    assert restore[restore.index("--tag"):restore.index("--tag") + 4] == ("--tag", "recordbench", "--tag", "exculpata")
    assert not list(node.node.glob(f".{names}-backup-snapshot-*"))
    assert not list(node.storage.parent.glob(f".{names}-storage-snapshot-*"))
