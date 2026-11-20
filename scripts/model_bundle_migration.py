"""Offline installation checks and configuration recovery for a bundle cutover."""
from contextlib import closing, contextmanager
import fcntl
from pathlib import Path
import json
import os
import sqlite3
import stat


JOURNAL = ".bundle-update-recovery.json"
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
_PHASE_RECEIPTS = {"state/provisioned.json", "state/install-progress.json"}
_APP_QUERY = "SELECT 1 FROM workbench_media_job WHERE state IN ('queued','running') LIMIT 1"
_PROCESSOR_QUERY = "SELECT 1 FROM jobs WHERE status NOT IN ('succeeded','failed','canceled') LIMIT 1"


def require_idle_media(root: Path) -> None:
    # The old app and its private processor are stopped. Opening either store
    # with its normal writer would run schema migrations on the rollback data.
    for path, query in ((root / "runtime/workbench.sqlite", _APP_QUERY),
                        (root / "transcription/jobs.sqlite3", _PROCESSOR_QUERY)):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeError("Saved media job database is unsafe.")
        if not path.exists():
            continue
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                active = db.execute(query).fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError("Cannot verify the saved media queues before retiring their worker.") from exc
        if active:
            raise RuntimeError("Finish or cancel pending media jobs on the saved node before retiring its worker.")


def configuration(root, app_path):
    return (root / "installation.json", root / "compose.env", app_path, root / "config/transcription.env")


def journal_exists(root):
    path = root / JOURNAL
    return path.exists() or path.is_symlink()


def write_journal(root, paths, atomic_write):
    payload = {str(path.relative_to(root)): path.read_bytes().decode("utf-8") for path in paths}
    for name in _PHASE_RECEIPTS:
        path = root / name
        if path.is_symlink() or path.parent.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeError("Saved installation progress is unsafe.")
        payload[name] = path.read_text() if path.exists() else None
    encoded = json.dumps(payload)
    if len(encoded.encode("utf-8")) > MAX_JOURNAL_BYTES:
        raise RuntimeError("Saved configuration exceeds the bundle recovery file limit.")
    atomic_write(root / JOURNAL, encoded)


def restore_journal(root, atomic_write):
    journal = root / JOURNAL
    info = journal.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_size > MAX_JOURNAL_BYTES):
        raise RuntimeError("Bundle update recovery file is unsafe.")
    payload = json.loads(journal.read_text())
    required = {"installation.json", "compose.env", "config/transcription.env"}
    required.update(_PHASE_RECEIPTS)
    if not isinstance(payload, dict) or not required.issubset(payload) or len(payload) != len(required) + 1:
        raise RuntimeError("Bundle update recovery file has invalid configuration keys.")
    app = Path(next(iter(payload.keys() - required)))
    if len(app.parts) != 2 or app.parent != Path("config") or app.name == ".." or "\\" in app.name:
        raise RuntimeError("Bundle update recovery file has an invalid application path.")
    for name, value in payload.items():
        path = root / name
        if path.is_symlink() or path.parent.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeError("Bundle update recovery files are unavailable or unsafe.")
        if name in _PHASE_RECEIPTS and value is None:
            continue
        if not isinstance(value, str) or (name not in _PHASE_RECEIPTS and not path.is_file()):
            raise RuntimeError("Bundle update recovery files are unavailable or unsafe.")
    for name, value in payload.items():
        if value is None:
            (root / name).unlink(missing_ok=True)
        else:
            atomic_write(root / name, value)


@contextmanager
def lifecycle_lock(root: Path):
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another update or resume is in progress for this node.") from exc
        yield
    finally:
        os.close(descriptor)
