"""Validated storage boundary and capacity policy for temporary matter bytes."""
from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

GIB = 1024**3
MIB = 1024**2
DEFAULT_MATTER_QUOTA_BYTES = 250 * GIB
DEFAULT_UPLOAD_SESSION_BYTES = 100 * GIB
DEFAULT_MEDIA_FILE_BYTES = 100 * GIB
DEFAULT_DOCUMENT_FILE_BYTES = 512 * MIB
DEFAULT_STORAGE_RESERVE_BYTES = 100 * GIB
MARKER_NAME = ".exculpata-managed-storage.json"
LEGACY_MARKER_NAME = ".recordbench-managed-storage.json"
MARKER_VERSION = 1
_OWNED_DIRECTORIES = ("matters", ".matter-purging", "ingestion-staging")


def format_bytes(value: int) -> str:
    """Format an integer byte boundary without overstating precision."""

    bounded = max(int(value), 0)
    for unit, size in (("TiB", 1024**4), ("GiB", GIB), ("MiB", MIB), ("KiB", 1024)):
        if bounded >= size:
            amount = bounded / size
            digits = 0 if amount >= 10 or amount.is_integer() else 1
            return f"{amount:.{digits}f} {unit}"
    return f"{bounded} B"


def _configured_units(name: str, default: int, *, unit: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    if not raw.isdecimal():
        raise RuntimeError(f"{name} must be a whole positive number")
    value = int(raw) * unit
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} is outside the supported storage policy range")
    return value


@dataclass(frozen=True)
class StoragePolicy:
    matter_quota_bytes: int = DEFAULT_MATTER_QUOTA_BYTES
    upload_session_bytes: int = DEFAULT_UPLOAD_SESSION_BYTES
    media_file_bytes: int = DEFAULT_MEDIA_FILE_BYTES
    document_file_bytes: int = DEFAULT_DOCUMENT_FILE_BYTES
    reserve_bytes: int = DEFAULT_STORAGE_RESERVE_BYTES

    def __post_init__(self) -> None:
        values = (
            self.matter_quota_bytes,
            self.upload_session_bytes,
            self.media_file_bytes,
            self.document_file_bytes,
        )
        if any(isinstance(value, bool) or int(value) <= 0 for value in values):
            raise ValueError("storage limits must be positive byte counts")
        if isinstance(self.reserve_bytes, bool) or int(self.reserve_bytes) < 0:
            raise ValueError("the storage reserve must be a non-negative byte count")
        if self.upload_session_bytes > self.matter_quota_bytes:
            raise ValueError("the upload collection limit cannot exceed the matter quota")
        if max(self.media_file_bytes, self.document_file_bytes) > self.upload_session_bytes:
            raise ValueError("an individual source limit cannot exceed the collection limit")

    @classmethod
    def from_environment(cls) -> "StoragePolicy":
        matter = _configured_units(
            "CASE_INTELLIGENCE_MATTER_QUOTA_GIB",
            DEFAULT_MATTER_QUOTA_BYTES,
            unit=GIB,
            minimum=GIB,
            maximum=16 * 1024 * GIB,
        )
        session = _configured_units(
            "CASE_INTELLIGENCE_UPLOAD_COLLECTION_GIB",
            DEFAULT_UPLOAD_SESSION_BYTES,
            unit=GIB,
            minimum=GIB,
            maximum=16 * 1024 * GIB,
        )
        media = _configured_units(
            "CASE_INTELLIGENCE_MEDIA_FILE_GIB",
            DEFAULT_MEDIA_FILE_BYTES,
            unit=GIB,
            minimum=GIB,
            maximum=16 * 1024 * GIB,
        )
        document = _configured_units(
            "CASE_INTELLIGENCE_DOCUMENT_FILE_MIB",
            DEFAULT_DOCUMENT_FILE_BYTES,
            unit=MIB,
            minimum=MIB,
            maximum=16 * 1024 * GIB,
        )
        reserve = _configured_units(
            "CASE_INTELLIGENCE_STORAGE_RESERVE_GIB",
            DEFAULT_STORAGE_RESERVE_BYTES,
            unit=GIB,
            minimum=0,
            maximum=16 * 1024 * GIB,
        )
        return cls(matter, session, media, document, reserve)


@dataclass(frozen=True)
class StorageCapacity:
    ready: bool
    total_bytes: int
    free_bytes: int
    reserve_bytes: int
    available_bytes: int
    managed_bytes: int
    optimized_finalize: bool


class ManagedMatterStorage:
    """Own exact matter byte directories without exposing their host path."""

    def __init__(self, root: Path, *, policy: StoragePolicy, require_marker: bool) -> None:
        self.root = Path(root)
        self.policy = policy
        self.require_marker = bool(require_marker)
        self._validate_root()
        self.matters = self.root / "matters"
        self.purging = self.root / ".matter-purging"
        self.ingestion_staging = self.root / "ingestion-staging"
        self._validate_owned_directories()
        self.optimized_finalize = self._probe_hardlinks()

    @classmethod
    def legacy_runtime(cls, runtime_dir: Path, *, policy: StoragePolicy) -> "ManagedMatterStorage":
        root = Path(runtime_dir)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in _OWNED_DIRECTORIES:
            (root / name).mkdir(exist_ok=True, mode=0o700)
        return cls(root, policy=policy, require_marker=False)

    @classmethod
    def initialize(cls, root: Path, *, policy: StoragePolicy | None = None) -> "ManagedMatterStorage":
        target = Path(root)
        if not target.is_absolute() or target == Path("/"):
            raise RuntimeError("the managed storage root must be an exact absolute directory")
        if any(parent.is_symlink() for parent in target.parents):
            raise RuntimeError("the managed storage path cannot traverse a symbolic link")
        if target.exists() and (target.is_symlink() or not target.is_dir()):
            raise RuntimeError("the managed storage root is unsafe")
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.is_symlink() or not target.is_dir():
            raise RuntimeError("the managed storage root is unsafe")
        names = os.environ.get("EXCULPATA_STORAGE_NAMES", "exculpata")
        if names not in {"recordbench", "exculpata"}:
            raise RuntimeError("the managed storage naming generation is invalid")
        marker_name = LEGACY_MARKER_NAME if names == "recordbench" else MARKER_NAME
        marker = target / marker_name
        if any((target / name).exists() or (target / name).is_symlink()
               for name in (MARKER_NAME, LEGACY_MARKER_NAME)):
            raise RuntimeError("the managed storage root is already initialized")
        for name in _OWNED_DIRECTORIES:
            directory = target / name
            directory.mkdir(exist_ok=False, mode=0o700)
            if directory.is_symlink() or not directory.is_dir():
                raise RuntimeError("a managed storage directory is unsafe")
        payload = {
            "format_version": MARKER_VERSION,
            "product": "RecordBench" if names == "recordbench" else "Exculpata",
            "storage_id": f"{names}-storage-{uuid.uuid4().hex}",
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        temporary = target / f".{marker_name}-{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, marker)
            cls._fsync_directory(target)
        finally:
            temporary.unlink(missing_ok=True)
        return cls(target, policy=policy or StoragePolicy(), require_marker=True)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _validate_root(self) -> None:
        if not self.root.is_absolute() or self.root == Path("/"):
            raise RuntimeError("the managed storage root must be an exact absolute directory")
        if self.root.is_symlink() or not self.root.is_dir():
            raise RuntimeError("the managed storage root is unavailable or unsafe")
        if any(parent.is_symlink() for parent in self.root.parents):
            raise RuntimeError("the managed storage path cannot traverse a symbolic link")
        if not os.access(self.root, os.R_OK | os.W_OK | os.X_OK):
            raise RuntimeError("the managed storage root is not writable")
        if not self.require_marker:
            return
        markers = [self.root / name for name in (MARKER_NAME, LEGACY_MARKER_NAME)
                   if (self.root / name).exists() or (self.root / name).is_symlink()]
        if len(markers) != 1:
            raise RuntimeError("the Exculpata managed storage marker is unavailable or ambiguous")
        marker = markers[0]
        names = "recordbench" if marker.name == LEGACY_MARKER_NAME else "exculpata"
        if marker.is_symlink() or not marker.is_file():
            raise RuntimeError("the Exculpata managed storage marker is unavailable")
        try:
            metadata = marker.stat(follow_symlinks=False)
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("the Exculpata managed storage marker is invalid") from exc
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4_096:
            raise RuntimeError("the Exculpata managed storage marker is invalid")
        if (
            not isinstance(payload, dict)
            or payload.get("format_version") != MARKER_VERSION
            or payload.get("product") != ("RecordBench" if names == "recordbench" else "Exculpata")
            or not isinstance(payload.get("storage_id"), str)
            or not payload["storage_id"].startswith(f"{names}-storage-")
        ):
            raise RuntimeError("the Exculpata managed storage marker is invalid")

    def _validate_owned_directories(self) -> None:
        root_device = self.root.stat(follow_symlinks=False).st_dev
        for directory in (self.matters, self.purging, self.ingestion_staging):
            if directory.is_symlink() or not directory.is_dir():
                raise RuntimeError("an Exculpata managed storage directory is unavailable")
            metadata = directory.stat(follow_symlinks=False)
            if metadata.st_dev != root_device or not os.access(directory, os.R_OK | os.W_OK | os.X_OK):
                raise RuntimeError("the managed storage boundary is not writable on one filesystem")

    def _probe_hardlinks(self) -> bool:
        source = self.ingestion_staging / f".hardlink-probe-{uuid.uuid4().hex}"
        linked = self.ingestion_staging / f".hardlink-probe-{uuid.uuid4().hex}"
        descriptor = -1
        try:
            descriptor = os.open(
                source,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            os.write(descriptor, b"recordbench-storage-probe")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.link(source, linked, follow_symlinks=False)
            return source.stat(follow_symlinks=False).st_ino == linked.stat(
                follow_symlinks=False
            ).st_ino
        except OSError:
            return False
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            linked.unlink(missing_ok=True)
            source.unlink(missing_ok=True)
            self._fsync_directory(self.ingestion_staging)

    @staticmethod
    def _tree_usage(root: Path) -> int:
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError("managed storage contains an unsafe directory")
        seen: set[tuple[int, int]] = set()
        total = 0
        pending = [root]
        while pending:
            directory = pending.pop()
            if directory.is_symlink() or not directory.is_dir():
                raise RuntimeError("managed storage contains an unsafe directory")
            with os.scandir(directory) as entries:
                children = tuple(entries)
            for entry in children:
                if entry.is_symlink():
                    raise RuntimeError("managed storage contains a symbolic link")
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    # Atomic manifest/playback writes can rename a temporary
                    # file after scandir returned it. It no longer consumes
                    # capacity, so a concurrent usage projection may skip it.
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(metadata.st_mode):
                    identity = (int(metadata.st_dev), int(metadata.st_ino))
                    if identity not in seen:
                        seen.add(identity)
                        total += int(metadata.st_size)
                else:
                    raise RuntimeError("managed storage contains an unexpected file type")
        return total

    def matter_usage_bytes(self, matter_id: str) -> int:
        root = self.matters / matter_id
        if root.parent != self.matters or root.is_symlink():
            raise RuntimeError("matter storage escaped its boundary")
        if not root.exists():
            return 0
        return self._tree_usage(root)

    def matter_payload_usage_bytes(self, matter_id: str) -> int:
        """Return quota-bearing source/derived bytes, excluding registry overhead.

        Registry bytes still participate in the filesystem reserve and managed
        usage totals. They are excluded only from the staff-facing per-matter
        payload allowance so fixed SQLite page overhead cannot consume a new
        matter's source quota before its first upload.
        """

        root = self.matters / matter_id
        total = self.matter_usage_bytes(matter_id)
        for relative in (
            Path("sources/manifest.json"),
            Path("sources/source-registry.sqlite3"),
            Path("sources/source-registry.sqlite3-journal"),
        ):
            path = root / relative
            if path.is_symlink():
                raise RuntimeError("managed storage contains a symbolic link")
            try:
                metadata = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError("managed storage contains an unexpected file type")
            total = max(total - int(metadata.st_size), 0)
        return total

    def managed_usage_bytes(self) -> int:
        return self._tree_usage(self.root)

    def capacity(self, *, include_managed_usage: bool = False) -> StorageCapacity:
        self._validate_root()
        self._validate_owned_directories()
        usage = shutil.disk_usage(self.root)
        available = max(int(usage.free) - self.policy.reserve_bytes, 0)
        return StorageCapacity(
            ready=usage.free > self.policy.reserve_bytes,
            total_bytes=int(usage.total),
            free_bytes=int(usage.free),
            reserve_bytes=self.policy.reserve_bytes,
            available_bytes=available,
            managed_bytes=(self.managed_usage_bytes() if include_managed_usage else -1),
            optimized_finalize=self.optimized_finalize,
        )

    def ensure_capacity(self, reserved_bytes: int) -> None:
        value = int(reserved_bytes)
        if value < 0:
            raise ValueError("reserved bytes cannot be negative")
        capacity = self.capacity()
        if not capacity.ready or value > capacity.available_bytes:
            raise RuntimeError("managed matter storage does not have enough protected capacity")
