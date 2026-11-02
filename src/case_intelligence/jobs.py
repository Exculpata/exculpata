from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .contracts import ContentOperation, PersistedSourceVersion
from .inventory import _utc
from .isolation import MatterStateRegistry
from .scanner import SyntheticScanner
from .sqlite_store import SQLiteStore
from .text_records import build_plain_text_record, exact_keys_json


class JobService:
    def __init__(self, store: SQLiteStore, registry: MatterStateRegistry, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.store = store
        self.registry = registry
        self.clock = clock

    def run_next(self, matter_id: str) -> str | None:
        claimed = self.registry.perform_active(
            matter_id, ContentOperation.INGEST, lambda: self._claim(matter_id)
        )
        if claimed is None:
            return None
        job_id, attempt_id, version = claimed
        try:
            payload = self._read_exact(version)
            record = build_plain_text_record(version, payload)
        except BaseException:
            self.registry.perform_active(
                matter_id, ContentOperation.INGEST,
                lambda: self._fail(job_id, attempt_id, "derive_failed"),
            )
            raise
        self.registry.perform_active(
            matter_id, ContentOperation.INGEST,
            lambda: self._succeed(job_id, attempt_id, record),
        )
        return job_id

    def retry(self, matter_id: str, job_id: str) -> None:
        def update() -> None:
            with self.store.transaction(immediate=True) as db:
                changed = db.execute(
                    "UPDATE job SET state='queued',updated_at=? WHERE job_id=? AND matter_id=? AND state='failed'",
                    (_utc(self.clock), job_id, matter_id),
                )
                if changed.rowcount != 1:
                    raise ValueError("only failed jobs may be explicitly retried")
        self.registry.perform_active(matter_id, ContentOperation.INGEST, update)

    def recover_interrupted(self, matter_id: str) -> int:
        def recover() -> int:
            now = _utc(self.clock)
            with self.store.transaction(immediate=True) as db:
                jobs = db.execute(
                    "SELECT job_id FROM job WHERE matter_id=? AND state='running'", (matter_id,)
                ).fetchall()
                for row in jobs:
                    db.execute(
                        "UPDATE job_attempt SET state='failed',failure_category='interrupted',completed_at=? WHERE job_id=? AND state='running'",
                        (now, row["job_id"]),
                    )
                    db.execute("UPDATE job SET state='failed',updated_at=? WHERE job_id=?", (now, row["job_id"]))
                return len(jobs)
        return self.registry.perform_active(matter_id, ContentOperation.INGEST, recover)

    def _claim(self, matter_id: str):
        with self.store.transaction(immediate=True) as db:
            job = db.execute(
                "SELECT j.*,v.* FROM job j JOIN source_version v USING(source_version_id) "
                "WHERE j.matter_id=? AND j.state='queued' ORDER BY j.created_at,j.job_id LIMIT 1",
                (matter_id,),
            ).fetchone()
            if job is None:
                return None
            number = db.execute(
                "SELECT count(*) FROM job_attempt WHERE job_id=?", (job["job_id"],)
            ).fetchone()[0] + 1
            attempt_id = f"attempt-{job['job_id']}-{number}"
            now = _utc(self.clock)
            db.execute("UPDATE job SET state='running',updated_at=? WHERE job_id=? AND state='queued'", (now, job["job_id"]))
            db.execute("INSERT INTO job_attempt VALUES(?,?,?,?,?,?,?)",
                       (attempt_id, job["job_id"], number, "running", None, now, None))
            version = PersistedSourceVersion(
                source_version_id=job["source_version_id"], source_file_id=job["source_file_id"],
                matter_id=job["matter_id"], source_location_id=job["source_location_id"],
                relative_path=job["relative_path"], display_name=job["display_name"],
                media_type=job["media_type"], byte_size=job["byte_size"], sha256=job["sha256"],
                stable_device=job["stable_device"], stable_inode=job["stable_inode"],
                stable_mtime_ns=job["stable_mtime_ns"], discovered_at=job["discovered_at"],
            )
            return job["job_id"], attempt_id, version

    def _read_exact(self, version: PersistedSourceVersion) -> bytes:
        row = self.store.connection.execute(
            "SELECT b.synthetic_root FROM scanner_binding b WHERE b.matter_id=? AND b.source_location_id=?",
            (version.matter_id, version.source_location_id),
        ).fetchone()
        if row is None:
            raise ValueError("scanner binding missing")
        items = SyntheticScanner(Path(row["synthetic_root"])).scan()
        matches = [item for item in items if item.relative_path == version.relative_path]
        if len(matches) != 1 or matches[0].sha256 != version.sha256:
            raise ValueError("immutable source bytes no longer available")
        return matches[0].content

    def _fail(self, job_id: str, attempt_id: str, category: str) -> None:
        now = _utc(self.clock)
        with self.store.transaction(immediate=True) as db:
            db.execute("UPDATE job_attempt SET state='failed',failure_category=?,completed_at=? WHERE job_attempt_id=? AND state='running'",
                       (category, now, attempt_id))
            db.execute("UPDATE job SET state='failed',updated_at=? WHERE job_id=? AND state='running'", (now, job_id))

    def _succeed(self, job_id: str, attempt_id: str, record) -> None:
        now = _utc(self.clock)
        stale = False
        with self.store.transaction(immediate=True) as db:
            job = db.execute("SELECT state FROM job WHERE job_id=?", (job_id,)).fetchone()
            attempt = db.execute("SELECT state FROM job_attempt WHERE job_attempt_id=?", (attempt_id,)).fetchone()
            if not job or not attempt or job["state"] != "running" or attempt["state"] != "running":
                stale = True
                if attempt and attempt["state"] == "running":
                    db.execute(
                        "UPDATE job_attempt SET state='superseded',failure_category='stale_completion',completed_at=? "
                        "WHERE job_attempt_id=? AND state='running'",
                        (now, attempt_id),
                    )
            else:
                db.execute(
                    "INSERT INTO representation VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_version_id,kind,processor_version) DO NOTHING",
                    (record.representation_id, record.matter_id, record.source_file_id,
                     record.source_version_id, "plain_text", "plain-text-v1", record.text_sha256),
                )
                existing = db.execute("SELECT * FROM segment WHERE segment_id=?", (record.segment_id,)).fetchone()
                values = (record.segment_id, record.matter_id, record.source_file_id,
                          record.source_version_id, record.representation_id, 1, record.text,
                          record.text_sha256, record.character_start, record.character_end,
                          record.line_start, record.line_end, exact_keys_json(record))
                if existing is None:
                    db.execute("INSERT INTO segment VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
                elif tuple(existing) != values:
                    raise ValueError("conflicting derived segment identity")
                db.execute("UPDATE job_attempt SET state='succeeded',completed_at=? WHERE job_attempt_id=?", (now, attempt_id))
                db.execute("UPDATE job SET state='succeeded',updated_at=? WHERE job_id=?", (now, job_id))
        if stale:
            raise ValueError("stale attempt completion")
