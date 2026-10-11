#!/usr/bin/env python3
"""Encrypted RecordBench node backups and non-destructive restore drills."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from case_intelligence.compat_env import required_env as _required_env
sys.path.pop(0)
from exculpata_install import NamingError, STORAGE_MARKERS, _node_names, _compose_environment
POSTGRES_IMAGE = "pgvector/pgvector:pg17"
ACTIVE_JOB_KEYS = (
    "ingest_jobs",
    "media_jobs",
    "media_summaries",
    "answer_jobs",
    "research_jobs",
    "review_runs",
    "report_compilation_jobs",
)
SAFE_RETENTION = re.compile(r"^[1-9][0-9]{0,3}[dhmwy]$")


class BackupError(RuntimeError):
    """An operator-safe backup failure."""


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _private_file(path: Path, *, label: str, allow_missing: bool = False) -> Path:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return path
        raise BackupError(f"{label} is unavailable") from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
    ):
        raise BackupError(f"{label} must be a regular owner-only mode-0600 file")
    return path


def _private_write(path: Path, data: bytes, *, exclusive: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def _read_json(path: Path, *, label: str, private: bool = True) -> Mapping[str, Any]:
    if private:
        _private_file(path, label=label)
    elif path.is_symlink() or not path.is_file():
        raise BackupError(f"{label} is unavailable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise BackupError(f"{label} is invalid")
    return value


def _node_root(value: Path) -> Path:
    candidate = value.expanduser().resolve(strict=True)
    if candidate == Path("/") or candidate.is_symlink() or not candidate.is_dir():
        raise BackupError("node root must be an existing exact directory other than /")
    installation = candidate / "installation.json"
    _private_file(installation, label="node installation record")
    return candidate


def _dotenv(path: Path) -> dict[str, str]:
    _private_file(path, label="node Compose environment")
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, encoded = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise BackupError(f"invalid Compose environment line {number}")
        encoded = encoded.strip()
        if encoded.startswith('"'):
            try:
                value = json.loads(encoded)
            except json.JSONDecodeError as exc:
                raise BackupError(f"invalid Compose environment line {number}") from exc
            if not isinstance(value, str):
                raise BackupError(f"invalid Compose environment line {number}")
        else:
            value = encoded
        if "\x00" in value or "\r" in value or "\n" in value:
            raise BackupError(f"invalid Compose environment line {number}")
        values[key] = value
    return values


def _exact_directory(value: str, *, label: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute() or candidate == Path("/"):
        raise BackupError(f"{label} must be an exact absolute directory other than /")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise BackupError(f"{label} is unavailable") from exc
    if resolved.is_symlink() or not resolved.is_dir():
        raise BackupError(f"{label} is unavailable or unsafe")
    return resolved


def _installation(node: Path) -> Mapping[str, Any]:
    value = _read_json(node / "installation.json", label="node installation record")
    if value.get("format_version") != 1 or not isinstance(value.get("auth"), str):
        raise BackupError("node installation record does not match this backup tool")
    return value


def _release_root(node: Path, installation: Mapping[str, Any]) -> Path:
    configured = installation.get("release_path")
    candidate = Path(configured) if isinstance(configured, str) and configured else PROJECT
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise BackupError("installed release is unavailable") from exc
    if not (candidate / "compose.yaml").is_file():
        raise BackupError("installed release does not contain compose.yaml")
    return candidate


def _compose(node: Path, installation: Mapping[str, Any]) -> list[str]:
    release = _release_root(node, installation)
    command = [
        "docker",
        "compose",
        "--env-file",
        str(node / "compose.env"),
        "-f",
        str(release / "compose.yaml"),
    ]
    if installation.get("auth") == "kerberos":
        command.extend(("-f", str(release / "compose.kerberos.yaml")))
    if installation.get("local_account_management") is True:
        command.extend(("-f", str(release / "compose.local-accounts.yaml")))
    profiles = installation.get("profiles", [])
    if not isinstance(profiles, list) or any(not isinstance(item, str) for item in profiles):
        raise BackupError("node profile configuration is invalid")
    for profile in profiles:
        command.extend(("--profile", profile))
    return command


def _run(
    command: Sequence[str],
    *,
    input_bytes: bytes | None = None,
    cwd: Path | None = None,
    capture: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            list(command),
            cwd=cwd,
            env=_compose_environment(command),
            input=input_bytes,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            check=check,
        )
    except FileNotFoundError as exc:
        raise BackupError(f"required command is unavailable: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise BackupError(f"command failed: {command[0]}{suffix}") from exc


def _restic(config: Mapping[str, Any], password: Path, arguments: Iterable[str], **kwargs: Any):
    executable = shutil.which("restic")
    if executable is None:
        raise BackupError("restic is unavailable; install restic before enabling backups")
    environment = os.environ.copy()
    environment["RESTIC_PASSWORD_FILE"] = str(password)
    repository = str(config["repository"])
    command = [executable, "-r", repository, *arguments]
    try:
        return subprocess.run(command, env=environment, check=True, **kwargs)
    except subprocess.CalledProcessError as exc:
        raise BackupError("encrypted repository operation failed") from exc


def _status_path(node: Path) -> Path:
    return node / "state" / "backup-status.json"


def _write_status(
    node: Path,
    *,
    state: str,
    message: str,
    started_at: str,
    repository: str,
    snapshot_id: str = "",
    active_jobs: int = 0,
) -> None:
    payload = {
        "format_version": 1,
        "state": state,
        "message": message,
        "started_at": started_at,
        "completed_at": _utc(),
        "repository": repository,
        "snapshot_id": snapshot_id,
        "active_jobs": active_jobs,
    }
    destination = _status_path(node)
    temporary = destination.with_name(destination.name + f".tmp-{os.getpid()}")
    _private_write(temporary, (json.dumps(payload, indent=2) + "\n").encode())
    os.replace(temporary, destination)


def _backup_config(node: Path) -> Mapping[str, Any]:
    value = _read_json(node / "config" / "backup.json", label="backup configuration")
    expected = {"format_version", "repository", "recovery_key_output", "retention"}
    if set(value) != expected or value.get("format_version") != 1:
        raise BackupError("backup configuration does not match this tool")
    repository = Path(str(value.get("repository", "")))
    recovery = Path(str(value.get("recovery_key_output", "")))
    retention = value.get("retention")
    if (
        not repository.is_absolute()
        or repository == Path("/")
        or not recovery.is_absolute()
        or recovery == Path("/")
        or not isinstance(retention, str)
        or SAFE_RETENTION.fullmatch(retention) is None
    ):
        raise BackupError("backup configuration contains invalid paths or retention")
    return value


def _mounted_parent(path: Path, *, allow_local: bool) -> None:
    parent = path.parent.resolve(strict=True)
    result = _run(("findmnt", "-T", str(parent), "-n", "-o", "TARGET"))
    target = result.stdout.decode("utf-8", "replace").strip()
    if not target:
        raise BackupError("backup repository parent is not on a recognized filesystem")
    if target == "/" and not allow_local:
        raise BackupError(
            "backup repository resolves to the server root filesystem; mount backup storage or pass --allow-local-repository for a deliberate local test"
        )


def initialize(args: argparse.Namespace) -> int:
    node = _node_root(args.node_root)
    if args.repository is None or args.recovery_key_output is None:
        raise BackupError("initialization requires --repository and --recovery-key-output")
    repository = args.repository.expanduser()
    recovery = args.recovery_key_output.expanduser()
    if not repository.is_absolute() or repository == Path("/"):
        raise BackupError("repository must be an exact absolute directory other than /")
    if not recovery.is_absolute() or recovery == Path("/"):
        raise BackupError("recovery key output must be an exact absolute file")
    repository = repository.resolve(strict=False)
    recovery = recovery.resolve(strict=False)
    if repository == node or node in repository.parents:
        raise BackupError("backup repository must be outside the RecordBench node")
    if recovery == node or node in recovery.parents or repository == recovery or repository in recovery.parents:
        raise BackupError("recovery key output must be outside the node and repository")
    if recovery.parent.is_symlink() or not recovery.parent.is_dir():
        raise BackupError("recovery key parent must already exist and cannot be a symlink")
    _mounted_parent(repository, allow_local=args.allow_local_repository)
    if not SAFE_RETENTION.fullmatch(args.retention):
        raise BackupError("retention must look like 14d, 8w, or 1y")
    config_path = node / "config" / "backup.json"
    if config_path.exists():
        existing = _backup_config(node)
        if (
            Path(str(existing["repository"])) != repository
            or Path(str(existing["recovery_key_output"])) != recovery
        ):
            raise BackupError("backup is already initialized with different destinations")
        print("Backup control plane is already initialized and unchanged.")
        return 0
    password = node / "secrets" / "restic-password"
    repository.mkdir(parents=True, exist_ok=True, mode=0o700)
    if password.exists() or recovery.exists():
        raise BackupError("refusing to replace an existing backup or recovery credential")
    credential = (secrets.token_urlsafe(64) + "\n").encode("ascii")
    try:
        _private_write(password, credential)
        _private_write(recovery, credential)
        _private_file(recovery, label="recovery key output")
        config = {
            "format_version": 1,
            "repository": str(repository),
            "recovery_key_output": str(recovery),
            "retention": args.retention,
        }
        _restic(config, password, ("init", "--repository-version", "2"))
        _restic(config, password, ("cat", "config"), stdout=subprocess.DEVNULL)
        _private_write(config_path, (json.dumps(config, indent=2) + "\n").encode())
        _write_status(
            node,
            state="initialized",
            message="Encrypted backup repository initialized; run a backup and restore drill.",
            started_at=_utc(),
            repository=str(repository),
        )
    except Exception:
        if not config_path.exists():
            password.unlink(missing_ok=True)
            recovery.unlink(missing_ok=True)
        raise
    print("Encrypted backup repository initialized.")
    print(f"Recovery credential written to the separate operator path: {recovery}")
    return 0


def _health(compose: Sequence[str]) -> Mapping[str, Any]:
    probe = (
        "import json,urllib.request;"
        "print(json.dumps(json.load(urllib.request.urlopen("
        "'http://127.0.0.1:8786/health',timeout=10))))"
    )
    result = _run((*compose, "exec", "-T", "app", "python", "-c", probe))
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BackupError("RecordBench health response is invalid") from exc
    if not isinstance(value, dict) or value.get("product") != "RecordBench":
        raise BackupError("RecordBench application health check failed")
    return value


def _active_jobs(health: Mapping[str, Any]) -> int:
    total = 0
    for key in ACTIVE_JOB_KEYS:
        counts = health.get(key)
        if not isinstance(counts, dict):
            raise BackupError(f"health response omitted {key}")
        for state in ("queued", "running"):
            value = counts.get(state, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise BackupError(f"health response contains an invalid {key} count")
            total += value
    return total


def _running_services(compose: Sequence[str]) -> frozenset[str]:
    result = _run((*compose, "ps", "--status", "running", "--services"))
    return frozenset(result.stdout.decode().split())


def _same_filesystem(paths: Iterable[Path]) -> None:
    devices = {path.stat().st_dev for path in paths}
    if len(devices) != 1:
        raise BackupError(
            "node state spans filesystems; place the node root and matter storage on one filesystem for short-quiescence snapshots"
        )


def _copy_linked(source: Path, destination: Path) -> None:
    _run(("cp", "-al", "--", str(source), str(destination)), capture=True)


def _snapshot_files(*roots: Path) -> list[Path]:
    """Inventory the frozen boundary without reading through symbolic links."""
    def walk_error(error: OSError) -> None:
        raise BackupError("snapshot boundary could not be read") from error

    files: list[Path] = []
    for root in roots:
        if root.is_symlink() or not root.is_dir():
            raise BackupError("snapshot boundary is unavailable or is a symbolic link")
        for parent, directories, names in os.walk(root, followlinks=False, onerror=walk_error):
            for name in directories + names:
                path = Path(parent) / name
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    raise BackupError("snapshot boundary contains a symbolic link")
                if stat.S_ISREG(mode):
                    files.append(path)
                elif not stat.S_ISDIR(mode):
                    raise BackupError("snapshot boundary contains an unsupported file type")
    return sorted(files)


def _sqlite_files(files: Iterable[Path]) -> list[Path]:
    return [path for path in files if path.suffix.casefold() in {".sqlite", ".sqlite3", ".db"}]


def _check_sqlite(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA quick_check;").fetchall() != [("ok",)]:
        raise BackupError("SQLite integrity validation failed")
    if connection.execute("PRAGMA foreign_key_check;").fetchall():
        raise BackupError("SQLite foreign-key validation failed")


def _validate_sqlite(path: Path) -> None:
    """Consolidate a stopped SQLite store into an independent snapshot file.

    Copy the journal boundary before opening SQLite: even a read-only WAL
    connection may update shared memory. Neither it nor resumed live writes
    may touch a hard-linked snapshot companion.
    """
    companions = [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal")]
    try:
        with tempfile.TemporaryDirectory(prefix=".sqlite-freeze-", dir=path.parent) as temporary:
            copied = Path(temporary) / "source.sqlite"
            frozen = Path(temporary) / "frozen.sqlite"
            shutil.copy2(path, copied)
            for companion in companions:
                if companion.exists():
                    shutil.copy2(companion, copied.with_name(copied.name + companion.name[len(path.name):]))
            source = sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True)
            try:
                destination = sqlite3.connect(frozen)
                try:
                    source.backup(destination)
                    destination.execute("PRAGMA journal_mode=DELETE")
                    _check_sqlite(destination)
                finally:
                    destination.close()
            finally:
                source.close()
            shutil.copystat(path, frozen)
            os.replace(frozen, path)
        # These are names in the snapshot only; the live companions remain.
        for companion in companions:
            companion.unlink(missing_ok=True)
    except sqlite3.Error as exc:
        raise BackupError("SQLite snapshot validation failed") from exc


def _hash_controls(payload: Path) -> None:
    roots = ("configuration", "control", "metadata", "postgres", "secrets", "accounts")
    lines: list[str] = []
    for root_name in roots:
        root = payload / root_name
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            lines.append(f"{digest.hexdigest()}  {path.relative_to(payload).as_posix()}")
    (payload / "CONTROL_SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="ascii")


def _validate_frozen_accounts(payload: Path, installation: Mapping[str, Any]) -> None:
    if installation.get("local_account_management") is not True:
        return
    if installation.get("auth") != "local":
        raise BackupError("browser account snapshot requires local authentication")
    # Load only this reviewed release's stdlib validator. Isolated Python ignores
    # ambient PYTHONPATH and user-site packages, with no Argon2 host dependency.
    validator = PROJECT / "src/case_intelligence/local_account_format.py"
    try:
        subprocess.run(
            [sys.executable, "-I", str(validator), str(payload / "accounts/local-accounts.json")],
            check=True, capture_output=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupError("frozen local account snapshot is missing, unsafe or invalid") from exc


def _snapshot_id(output: str) -> str:
    found = ""
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("message_type") == "summary":
            candidate = event.get("snapshot_id")
            if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{8,64}", candidate):
                found = candidate
    if not found:
        raise BackupError("restic did not return a verified snapshot identifier")
    return found


def backup(args: argparse.Namespace) -> int:
    node = _node_root(args.node_root)
    config = _backup_config(node)
    installation = _installation(node)
    names = _node_names(node)
    environment = _dotenv(node / "compose.env")
    compose = _compose(node, installation)
    password = _private_file(node / "secrets" / "restic-password", label="backup credential")
    repository = Path(str(config["repository"]))
    if not (repository / "config").is_file():
        raise BackupError("encrypted backup repository is unavailable")
    _restic(config, password, ("cat", "config"), stdout=subprocess.DEVNULL)
    started = _utc()
    lock_path = node / "state" / "backup.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = lock_path.open("a+b")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise BackupError("another RecordBench backup is already running") from None
    running = _running_services(compose)
    if "app" not in running:
        raise BackupError("RecordBench app is not running; use a documented offline recovery procedure")
    health = _health(compose)
    active = _active_jobs(health)
    if active:
        _write_status(
            node,
            state="deferred",
            message="Backup deferred because background work is active; the quiet-hour timer will retry.",
            started_at=started,
            repository=str(repository),
            active_jobs=active,
        )
        print(f"Backup deferred: {active} queued or running background job(s).")
        return 0
    if args.dry_run:
        print(json.dumps({"dry_run": True, "repository": str(repository), "active_jobs": 0}))
        return 0

    sources = {
        "runtime": _exact_directory(_required_env("RECORDBENCH_RUNTIME_ROOT", environ=environment), label="runtime storage"),
        "configuration": _exact_directory(_required_env("RECORDBENCH_CONFIG_ROOT", environ=environment), label="configuration root"),
        "secrets": _exact_directory(_required_env("RECORDBENCH_SECRETS_ROOT", environ=environment), label="secrets root"),
    }
    if installation.get("local_account_management") is True:
        sources["accounts"] = _exact_directory(_required_env("RECORDBENCH_LOCAL_ACCOUNT_ROOT", environ=environment), label="local account root")
    managed_storage = _exact_directory(
        _required_env("RECORDBENCH_STORAGE_ROOT", environ=environment), label="matter storage"
    )
    markers = [managed_storage / name for name in STORAGE_MARKERS
               if (managed_storage / name).exists() or (managed_storage / name).is_symlink()]
    if len(markers) != 1 or markers[0].is_symlink() or not markers[0].is_file():
        raise BackupError("matter storage ownership marker is unavailable")
    _same_filesystem((node, *(source for name, source in sources.items() if name != "accounts")))
    snapshot = Path(tempfile.mkdtemp(prefix=f".{names}-backup-snapshot-", dir=node))
    os.chmod(snapshot, 0o700)
    payload = snapshot / "payload"
    payload.mkdir(mode=0o700)
    if managed_storage.parent.is_symlink():
        raise BackupError("matter storage parent cannot be a symbolic link")
    storage_snapshot_parent = Path(
        tempfile.mkdtemp(
            prefix=f".{names}-storage-snapshot-", dir=managed_storage.parent
        )
    )
    os.chmod(storage_snapshot_parent, 0o700)
    storage_snapshot = storage_snapshot_parent / "managed-storage"
    app_stopped = False
    try:
        # Stop the writer before capturing either the projection or source/control
        # stores. Work finishing after the admission probe must be in all of them.
        app_stopped = True
        _run((*compose, "stop", "--timeout", "120", "gateway", "app"), capture=False)
        (payload / "postgres").mkdir()
        postgres_dump = _run(
            (*compose, "exec", "-T", "postgres", "pg_dump", "-U", "recordbench", "-d", "recordbench", "--format=custom")
        )
        (payload / "postgres" / "review-index.dump").write_bytes(postgres_dump.stdout)
        _run(
            (*compose, "exec", "-T", "postgres", "pg_restore", "--list"),
            input_bytes=postgres_dump.stdout,
        )

        for name, source in sources.items():
            if name == "accounts":
                # The bounded account directory may live on another filesystem.
                # Copy it during quiescence; preserve symlinks for the subsequent
                # rejection rather than following them into another boundary.
                _snapshot_files(source)
                shutil.copytree(source, payload / name, symlinks=True)
            else:
                _copy_linked(source, payload / name)
        _copy_linked(managed_storage, storage_snapshot)
        control = payload / "control"
        control.mkdir()
        shutil.copy2(node / "compose.env", control / "compose.env")
        shutil.copy2(node / "installation.json", control / "installation.json")
        databases = _sqlite_files(_snapshot_files(payload, storage_snapshot))
        _validate_frozen_accounts(payload, installation)
        for database in databases:
            _validate_sqlite(database)
        metadata = payload / "metadata"
        metadata.mkdir()
        (metadata / "backup-manifest.json").write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "created_at": started,
                    "recordbench_version": installation.get("version", "unknown"),
                    "release_id": installation.get("release_id", "untracked"),
                    "retention": config["retention"],
                    "layout": "split-hardlink-snapshot-v1",
                    "managed_storage_root": str(managed_storage),
                    "sqlite_stores": len(databases),
                    "managed_sqlite_stores": sum(database.is_relative_to(storage_snapshot) for database in databases),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        _hash_controls(payload)

        _run((*compose, "up", "-d", "--no-deps", "app", "gateway"), capture=False)
        app_stopped = False
        deadline = time.monotonic() + 300
        while True:
            try:
                _health(compose)
                break
            except BackupError:
                if time.monotonic() >= deadline:
                    raise BackupError("RecordBench did not recover after its consistency snapshot")
                time.sleep(2)

        result = _restic(
            config,
            password,
            (
                "backup",
                "--json",
                "--tag",
                names,
                "--tag",
                f"node-{installation.get('node_id', 'default')}",
                str(payload),
                str(storage_snapshot),
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        snapshot_id = _snapshot_id(result.stdout.decode("utf-8", "replace"))
        _restic(
            config,
            password,
            ("forget", "--tag", names, "--keep-within", str(config["retention"]), "--prune"),
        )
        _restic(config, password, ("check", "--read-data-subset=1/14"))
        _write_status(
            node,
            state="succeeded",
            message="Encrypted backup and repository subset check succeeded; restore-drill status is tracked separately.",
            started_at=started,
            repository=str(repository),
            snapshot_id=snapshot_id,
        )
        print(f"Encrypted RecordBench snapshot succeeded: {snapshot_id}")
        return 0
    except Exception:
        _write_status(
            node,
            state="failed",
            message="Backup failed; inspect the operator terminal or timer journal.",
            started_at=started,
            repository=str(repository),
        )
        raise
    finally:
        if app_stopped:
            try:
                _run((*compose, "up", "-d", "--no-deps", "app", "gateway"), capture=False)
            except Exception as exc:
                print(f"CRITICAL: automatic application restart failed: {exc}", file=sys.stderr)
        try:
            resolved = snapshot.resolve(strict=True)
            if resolved.parent == node and resolved.name.startswith(f".{names}-backup-snapshot-"):
                shutil.rmtree(resolved)
        except FileNotFoundError:
            pass
        try:
            resolved_storage = storage_snapshot_parent.resolve(strict=True)
            if (
                resolved_storage.parent == managed_storage.parent
                and resolved_storage.name.startswith(f".{names}-storage-snapshot-")
            ):
                shutil.rmtree(resolved_storage)
        except FileNotFoundError:
            pass


def status(args: argparse.Namespace) -> int:
    node = _node_root(args.node_root)
    path = _status_path(node)
    if not path.exists():
        print(json.dumps({"state": "not_initialized", "message": "No backup status exists."}, indent=2))
        return 0
    value = _read_json(path, label="backup status")
    print(json.dumps(value, indent=2))
    if (node / "config" / "backup.json").exists():
        config = _backup_config(node)
        password = _private_file(node / "secrets" / "restic-password", label="backup credential")
        result = _restic(
            config,
            password,
            ("snapshots", "--latest", "5", "--tag", "recordbench", "--tag", "exculpata", "--json"),
            stdout=subprocess.PIPE,
        )
        print(result.stdout.decode("utf-8", "replace"))
    return 0


def _verify_checksum_line(payload: Path, line: str) -> None:
    digest, separator, relative = line.partition("  ")
    if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise BackupError("restored checksum manifest is invalid")
    candidate = (payload / relative).resolve(strict=True)
    if payload not in candidate.parents or not candidate.is_file():
        raise BackupError("restored checksum path escapes the drill target")
    actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
    if not secrets.compare_digest(actual, digest):
        raise BackupError(f"restored control checksum failed for {relative}")


def restore(args: argparse.Namespace) -> int:
    node = _node_root(args.node_root)
    config = _backup_config(node)
    password = _private_file(node / "secrets" / "restic-password", label="backup credential")
    if args.target is None:
        raise BackupError("restore drill requires --target")
    target = args.target.expanduser().resolve(strict=False)
    if not target.is_absolute() or target == Path("/") or target.exists():
        raise BackupError("restore target must be a new exact absolute directory other than /")
    if target == node or node in target.parents or target in node.parents:
        raise BackupError("restore drill target must be separate from the live node")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(mode=0o700)
    try:
        _restic(
            config,
            password,
            ("restore", args.snapshot, "--verify", "--tag", "recordbench", "--tag", "exculpata", "--target", str(target)),
        )
        files = _snapshot_files(target)
        checksums = [
            path
            for path in files
            if path.name == "CONTROL_SHA256SUMS"
        ]
        if len(checksums) != 1:
            raise BackupError("restored control checksum manifest is unavailable")
        checksum = checksums[0]
        payload = checksum.parent
        for line in checksum.read_text(encoding="ascii").splitlines():
            _verify_checksum_line(payload, line)
        restored_installation = _read_json(payload / "control/installation.json", label="restored installation record")
        _validate_frozen_accounts(payload, restored_installation)
        storage_markers = [path for path in files if path.name in STORAGE_MARKERS]
        if len(storage_markers) != 1:
            raise BackupError("restored managed storage boundary is unavailable or ambiguous")
        managed_storage = storage_markers[0].parent
        databases = _sqlite_files(files)
        if not any(database.is_relative_to(payload) for database in databases):
            raise BackupError("restore drill found no SQLite control stores")
        managed_databases = sum(database.is_relative_to(managed_storage) for database in databases)
        manifest = _read_json(payload / "metadata/backup-manifest.json", label="backup manifest", private=False)
        # Older snapshots lack these counts and still receive all-store checks.
        for name, count in (("sqlite_stores", len(databases)), ("managed_sqlite_stores", managed_databases)):
            if name in manifest and (type(manifest[name]) is not int or manifest[name] != count):
                raise BackupError("restored SQLite inventory does not match the backup manifest")
        for database in databases:
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
                _check_sqlite(connection)
            except sqlite3.Error as exc:
                raise BackupError("restored SQLite validation failed") from exc
            finally:
                if connection is not None:
                    connection.close()
        dumps = [
            path
            for path in files
            if path.name == "review-index.dump"
        ]
        if len(dumps) != 1:
            raise BackupError("restored PostgreSQL projection dump is unavailable")
        dump = dumps[0]
        _run(("docker", "run", "--rm", "-i", POSTGRES_IMAGE, "pg_restore", "--list"), input_bytes=dump.read_bytes())
        receipt = {
            "format_version": 1,
            "state": "verified",
            "verified_at": _utc(),
            "snapshot": args.snapshot,
            "target": str(target),
            "sqlite_stores": len(databases),
            "managed_sqlite_stores": managed_databases,
        }
        _private_write(
            target / "RESTORE_DRILL_VERIFIED.json",
            (json.dumps(receipt, indent=2) + "\n").encode(),
        )
        print(f"Restore drill verified at {target}")
        print("The drill target was not connected to the live RecordBench node.")
        return 0
    except Exception:
        print(f"Restore drill failed; the isolated target was retained for inspection: {target}", file=sys.stderr)
        raise


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    for name in ("initialize", "backup", "status", "restore"):
        command = sub.add_parser(name)
        command.add_argument("--node-root", type=Path, required=True)
        if name == "initialize":
            command.add_argument("--repository", type=Path, required=True)
            command.add_argument("--recovery-key-output", type=Path, required=True)
            command.add_argument("--retention", default="14d")
            command.add_argument("--allow-local-repository", action="store_true")
        elif name == "backup":
            command.add_argument("--dry-run", action="store_true")
        elif name == "restore":
            command.add_argument("--target", type=Path, required=True)
            command.add_argument("--snapshot", default="latest")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        return {
            "initialize": initialize,
            "backup": backup,
            "status": status,
            "restore": restore,
        }[args.command](args)
    except (BackupError, NamingError, OSError, KeyError, ValueError) as exc:
        print(f"RecordBench backup error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
