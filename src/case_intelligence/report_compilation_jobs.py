"""Durable compilation queue with lease-fenced, transactional report completion."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from typing import Callable, Sequence, TYPE_CHECKING

from .report_compilation import CompilationProblem

if TYPE_CHECKING:
    from .workspace_store import WorkspaceStore


class CompilationLeaseLost(CompilationProblem):
    """This worker can no longer mutate the job or save its result."""


def _sqlite_contention(error: sqlite3.OperationalError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        return (code & 0xff) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    return str(error) in {"database is locked", "database table is locked", "database schema is locked"}


@dataclass(frozen=True)
class CompilationJobPolicy:
    actor_active_limit: int = 3
    pending_limit: int = 200
    lease_seconds: float = 120.0
    maximum_attempts: int = 3
    maximum_selections: int = 500

    def __post_init__(self):
        for name in ("actor_active_limit", "pending_limit", "maximum_attempts", "maximum_selections"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive.")
        if not 0 < self.lease_seconds < float("inf"):
            raise ValueError("lease_seconds must be finite and positive.")


@dataclass(frozen=True)
class CompilationJobRecord:
    job_id: str
    matter_id: str
    actor_id: str
    request_key: str
    kind: str
    topic: str
    selections: tuple[str, ...]
    state: str
    attempts: int
    worker_id: str | None
    lease_token: str | None
    lease_expires_at: float | None
    cancellation_requested: int
    input_fingerprint: str
    report_id: str | None
    message: str
    created_at: float
    updated_at: float
    finished_at: float | None


def _record(row) -> CompilationJobRecord:
    value = dict(row)
    value["selections"] = tuple(json.loads(value.pop("selections_json")))
    return CompilationJobRecord(**value)


class ReportCompilationJobs:
    def __init__(self, workspace: WorkspaceStore, *, policy: CompilationJobPolicy | None = None,
                 clock: Callable[[], float] = time.time):
        self.workspace = workspace
        self.policy = policy or CompilationJobPolicy()
        self.clock = clock

    @contextmanager
    def _transaction(self):
        with self.workspace._lock, self.workspace.connection:
            self.workspace.connection.execute("BEGIN IMMEDIATE")
            yield self.workspace.connection

    @staticmethod
    def _authorized(connection, matter_id, actor_id) -> bool:
        return connection.execute(
            "SELECT 1 FROM workbench_effective_membership m JOIN workbench_principal p "
            "ON p.principal_id=m.principal_id JOIN workbench_matter_lifecycle l "
            "ON l.matter_id=m.matter_id WHERE m.matter_id=? AND m.principal_id=? "
            "AND m.state='active' AND p.active=1 AND l.state='active'", (matter_id, actor_id)
        ).fetchone() is not None

    def counts(self) -> dict[str, int]:
        with self.workspace._lock:
            rows = self.workspace.connection.execute(
                "SELECT state,COUNT(*) AS count FROM workbench_report_compilation_job GROUP BY state"
            ).fetchall()
        counts = {state: 0 for state in ("queued", "running", "succeeded", "failed", "cancelled")}
        counts.update({row["state"]: int(row["count"]) for row in rows})
        return counts

    def get(self, matter_id: str, actor_id: str, job_id: str) -> CompilationJobRecord:
        with self.workspace._lock:
            connection = self.workspace.connection
            if not self._authorized(connection, matter_id, actor_id):
                raise KeyError(job_id)
            row = connection.execute("SELECT * FROM workbench_report_compilation_job WHERE job_id=? AND matter_id=? AND actor_id=?",
                                     (job_id, matter_id, actor_id)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return _record(row)

    def list(self, matter_id: str, actor_id: str, *, limit: int = 50, actionable_only: bool = False) -> tuple[CompilationJobRecord, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("Job list limit must be between 1 and 200.")
        with self.workspace._lock:
            connection = self.workspace.connection
            if not self._authorized(connection, matter_id, actor_id):
                raise KeyError(matter_id)
            actionable = " AND (state!='succeeded' OR report_id IS NULL)" if actionable_only else ""
            rows = connection.execute("SELECT * FROM workbench_report_compilation_job WHERE matter_id=? AND actor_id=?" + actionable + " ORDER BY created_at DESC,job_id DESC LIMIT ?",
                                      (matter_id, actor_id, limit)).fetchall()
        return tuple(_record(row) for row in rows)

    def queue(self, matter_id: str, actor_id: str, kind: str, topic: str,
              selections: Sequence[str], request_key: str) -> tuple[CompilationJobRecord, bool]:
        topic = " ".join(topic.split())
        if kind not in {"timeline", "entities", "topic"} or (kind == "topic" and not topic) or len(topic) > 500:
            raise CompilationProblem("Choose a report type and a specific topic when needed.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,120}", request_key):
            raise CompilationProblem("The compilation request key is invalid.")
        if any(not isinstance(item, str) for item in selections):
            raise CompilationProblem("Saved-work identifiers must be strings.")
        selected = tuple(dict.fromkeys(selections))
        if not 1 <= len(selected) <= self.policy.maximum_selections or any(
            not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,240}", item) for item in selected
        ):
            raise CompilationProblem("Select a bounded list of saved-work identifiers.")
        encoded = json.dumps(selected, separators=(",", ":"))
        now = self.clock()
        with self._transaction() as connection:
            if not self._authorized(connection, matter_id, actor_id):
                raise KeyError(matter_id)
            prior = connection.execute("SELECT * FROM workbench_report_compilation_job WHERE matter_id=? AND actor_id=? AND request_key=?",
                                       (matter_id, actor_id, request_key)).fetchone()
            if prior is not None:
                if (prior["kind"], prior["topic"], prior["selections_json"]) != (kind, topic, encoded):
                    raise CompilationProblem("This request key was already used for different compilation inputs.")
                return _record(prior), False
            self._admit(connection, actor_id)
            identifier = f"report-compilation-{uuid.uuid4().hex}"
            connection.execute("INSERT INTO workbench_report_compilation_job(job_id,matter_id,actor_id,request_key,kind,topic,selections_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'queued',?,?)",
                               (identifier, matter_id, actor_id, request_key, kind, topic, encoded, now, now))
            return _record(connection.execute("SELECT * FROM workbench_report_compilation_job WHERE job_id=?", (identifier,)).fetchone()), True

    def _admit(self, connection, actor_id):
        # Frozen legacy jobs are preserved for recovery/export, not runnable load.
        count = connection.execute("SELECT COUNT(*),SUM(CASE WHEN actor_id=? THEN 1 ELSE 0 END) FROM workbench_report_compilation_job WHERE state IN ('queued','running') AND matter_id IN (SELECT matter_id FROM workbench_matter_lifecycle WHERE state='active')", (actor_id,)).fetchone()
        if count[0] >= self.policy.pending_limit or (count[1] or 0) >= self.policy.actor_active_limit:
            raise CompilationProblem("The compilation queue is full. Wait for an active report to finish.")

    def _recover(self, connection, now):
        # Recovery must not mutate frozen snapshots or let one block active work.
        expired = connection.execute("UPDATE workbench_report_compilation_job SET state=CASE WHEN cancellation_requested=1 THEN 'cancelled' WHEN attempts>=? THEN 'failed' ELSE 'queued' END, message=CASE WHEN cancellation_requested=1 THEN 'Compilation cancelled.' WHEN attempts>=? THEN 'Compilation stopped after repeated worker interruptions. Retry when the service is ready.' ELSE 'Interrupted compilation queued for retry.' END,worker_id=NULL,lease_token=NULL,lease_expires_at=NULL,input_fingerprint='',updated_at=?,finished_at=CASE WHEN cancellation_requested=1 OR attempts>=? THEN ? ELSE NULL END WHERE state='running' AND lease_expires_at<=? AND matter_id IN (SELECT matter_id FROM workbench_matter_lifecycle WHERE state='active') RETURNING job_id,matter_id,actor_id,state",
                           (self.policy.maximum_attempts, self.policy.maximum_attempts, now, self.policy.maximum_attempts, now, now)).fetchall()
        revoked = connection.execute("UPDATE workbench_report_compilation_job SET state='failed',message='Matter access is no longer active.',lease_token=NULL,worker_id=NULL,lease_expires_at=NULL,updated_at=?,finished_at=? WHERE state IN ('queued','running') AND matter_id IN (SELECT matter_id FROM workbench_matter_lifecycle WHERE state='active') AND NOT EXISTS (SELECT 1 FROM workbench_effective_membership m JOIN workbench_principal p ON p.principal_id=m.principal_id JOIN workbench_matter_lifecycle l ON l.matter_id=m.matter_id WHERE m.matter_id=workbench_report_compilation_job.matter_id AND m.principal_id=workbench_report_compilation_job.actor_id AND m.state='active' AND p.active=1 AND l.state='active') RETURNING job_id,matter_id,actor_id,state", (now, now)).fetchall()
        # RETURNING captures only transitions made by this recovery transaction.
        # Terminal rows cannot be selected again on a later scan. A job that was
        # requeued and then lost access contributes only its terminal transition.
        for row in (*expired, *revoked):
            if row["state"] not in {"failed", "cancelled"}:
                continue
            self.workspace._append_audit_event_locked(
                actor_principal_id=row["actor_id"], session_id=None, matter_id=row["matter_id"],
                request_id=row["job_id"], action="report.compile.recover", outcome="failure",
                object_type="report_compilation", object_id=row["job_id"], details={"state": row["state"]},
            )

    def recover_expired(self) -> None:
        with self._transaction() as connection:
            self._recover(connection, self.clock())

    def claim(self, worker_id: str) -> CompilationJobRecord | None:
        now = self.clock()
        with self._transaction() as connection:
            self._recover(connection, now)
            row = connection.execute("SELECT * FROM workbench_report_compilation_job WHERE state='queued' AND matter_id IN (SELECT matter_id FROM workbench_matter_lifecycle WHERE state='active') ORDER BY created_at,job_id LIMIT 1").fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            connection.execute("UPDATE workbench_report_compilation_job SET state='running',attempts=attempts+1,worker_id=?,lease_token=?,lease_expires_at=?,message='Compiling selected saved work.',updated_at=? WHERE job_id=? AND state='queued'",
                               (worker_id, token, now+self.policy.lease_seconds, now, row["job_id"]))
            return _record(connection.execute("SELECT * FROM workbench_report_compilation_job WHERE job_id=?", (row["job_id"],)).fetchone())

    def _owned(self, connection, job: CompilationJobRecord, *, allow_cancelled=False):
        row = connection.execute("SELECT * FROM workbench_report_compilation_job WHERE job_id=? AND matter_id=? AND actor_id=? AND state='running' AND lease_token=? AND lease_expires_at>?",
                                 (job.job_id, job.matter_id, job.actor_id, job.lease_token, self.clock())).fetchone()
        if row is None or (row["cancellation_requested"] and not allow_cancelled):
            raise CompilationLeaseLost("The compilation was cancelled or moved to another worker.")
        return row

    def heartbeat(self, job: CompilationJobRecord) -> bool:
        # Source validation holds the workspace lock while reading potentially
        # large selections. Lease renewal must progress independently of that
        # Python lock. SQLite still serializes this short transaction with
        # cancellation, recovery and completion, and _owned fences old workers.
        # mode=rw avoids creating a replacement database after removal.
        with closing(sqlite3.connect(self.workspace.path.resolve().as_uri() + "?mode=rw", uri=True,
                                     timeout=min(5.0, self.policy.lease_seconds / 3))) as connection, connection:
            connection.create_function("recordbench_principal_enabled", 2,
                                       lambda provider, subject: int(self.workspace.principal_enabled(provider, subject)))
            connection.create_function("recordbench_case_content_cleanup", 1, lambda matter_id: 0)
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._owned(connection, job)
            except CompilationLeaseLost:
                return False
            if not self._authorized(connection, job.matter_id, job.actor_id):
                return False
            connection.execute("UPDATE workbench_report_compilation_job SET lease_expires_at=?,updated_at=? WHERE job_id=? AND lease_token=?",
                               (self.clock()+self.policy.lease_seconds, self.clock(), job.job_id, job.lease_token))
            return True

    def heartbeat_deadline(self, job: CompilationJobRecord) -> float | None:
        """Read the durable lease independently while a WAL writer is busy."""
        with closing(sqlite3.connect(self.workspace.path.resolve().as_uri() + "?mode=ro", uri=True,
                                     timeout=min(0.25, self.policy.lease_seconds / 12))) as connection:
            connection.create_function("recordbench_principal_enabled", 2,
                                       lambda provider, subject: int(self.workspace.principal_enabled(provider, subject)))
            connection.row_factory = sqlite3.Row
            try:
                row = self._owned(connection, job)
            except CompilationLeaseLost:
                return None
            if not self._authorized(connection, job.matter_id, job.actor_id):
                return None
            return float(row["lease_expires_at"])

    def cancelled(self, job: CompilationJobRecord) -> bool:
        with self.workspace._lock:
            try:
                self._owned(self.workspace.connection, job)
            except CompilationLeaseLost:
                return True
            return not self._authorized(self.workspace.connection, job.matter_id, job.actor_id)

    def record_input_fingerprint(self, job: CompilationJobRecord, fingerprint: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise CompilationProblem("The compilation basis fingerprint is invalid.")
        with self._transaction() as connection:
            row = self._owned(connection, job)
            if row["input_fingerprint"] and row["input_fingerprint"] != fingerprint:
                raise CompilationProblem("Compilation inputs changed during this attempt.")
            connection.execute("UPDATE workbench_report_compilation_job SET input_fingerprint=?,updated_at=? WHERE job_id=? AND lease_token=?",
                               (fingerprint, self.clock(), job.job_id, job.lease_token))

    def complete(self, job: CompilationJobRecord, create_report: Callable, *, fingerprint: str):
        """Builder must use this transaction (no BEGIN, commit, or network work)."""
        with self._transaction() as connection:
            row = self._owned(connection, job)
            if not self._authorized(connection, job.matter_id, job.actor_id):
                raise CompilationLeaseLost("Matter access changed before the report could be saved.")
            if not row["input_fingerprint"] or row["input_fingerprint"] != fingerprint:
                raise CompilationProblem("Compilation inputs changed before the report could be saved.")
            result = create_report()
            report_id = result if isinstance(result, str) else result.report_id
            report = connection.execute("SELECT 1 FROM workbench_report WHERE report_id=? AND matter_id=?", (report_id, job.matter_id)).fetchone()
            if report is None:
                raise CompilationProblem("The compiled report was not saved in this matter.")
            self._owned(connection, job)
            connection.execute("UPDATE workbench_report_compilation_job SET state='succeeded',report_id=?,message='Report draft ready.',worker_id=NULL,lease_token=NULL,lease_expires_at=NULL,finished_at=?,updated_at=? WHERE job_id=? AND lease_token=?",
                               (report_id, self.clock(), self.clock(), job.job_id, job.lease_token))
            self.workspace._append_audit_event_locked(
                actor_principal_id=job.actor_id, session_id=None, matter_id=job.matter_id,
                request_id=job.job_id, action="report.compile.complete", outcome="success",
                object_type="report", object_id=report_id, details={"state": "succeeded"},
            )
            return result

    def fail(self, job: CompilationJobRecord, message: str = "Compilation could not finish. Retry the saved request.") -> None:
        with self._transaction() as connection:
            row = self._owned(connection, job, allow_cancelled=True)
            state = "cancelled" if row["cancellation_requested"] else "failed"
            connection.execute("UPDATE workbench_report_compilation_job SET state=?,message=?,worker_id=NULL,lease_token=NULL,lease_expires_at=NULL,finished_at=?,updated_at=? WHERE job_id=? AND lease_token=?",
                               (state, "Compilation cancelled." if state == "cancelled" else message[:240], self.clock(), self.clock(), job.job_id, job.lease_token))

            self.workspace._append_audit_event_locked(
                actor_principal_id=job.actor_id, session_id=None, matter_id=job.matter_id,
                request_id=job.job_id, action="report.compile.finish", outcome="failure",
                object_type="report_compilation", object_id=job.job_id, details={"state": state},
            )

    def cancel(self, matter_id: str, actor_id: str, job_id: str) -> CompilationJobRecord:
        with self._transaction() as connection:
            job = self.get(matter_id, actor_id, job_id)
            if job.state not in {"queued", "running"}:
                return job
            state = "cancelled" if job.state == "queued" else "running"
            connection.execute("UPDATE workbench_report_compilation_job SET state=?,cancellation_requested=1,message=?,finished_at=?,updated_at=? WHERE job_id=?",
                               (state, "Compilation cancelled." if state == "cancelled" else "Cancelling compilation.", self.clock() if state == "cancelled" else None, self.clock(), job_id))
        return self.get(matter_id, actor_id, job_id)

    def retry(self, matter_id: str, actor_id: str, job_id: str) -> CompilationJobRecord:
        with self._transaction() as connection:
            job = self.get(matter_id, actor_id, job_id)
            if job.state not in {"failed", "cancelled"} and not (job.state == "succeeded" and not job.report_id):
                return job
            self._admit(connection, actor_id)
            connection.execute("UPDATE workbench_report_compilation_job SET state='queued',attempts=0,cancellation_requested=0,input_fingerprint='',worker_id=NULL,lease_token=NULL,lease_expires_at=NULL,finished_at=NULL,message='Queued for retry.',updated_at=? WHERE job_id=?", (self.clock(), job_id))
        return self.get(matter_id, actor_id, job_id)

    def release(self, job: CompilationJobRecord) -> None:
        """Fence a shutting-down worker before another worker resumes its intent."""
        with self._transaction() as connection:
            self._owned(connection, job, allow_cancelled=True)
            connection.execute("UPDATE workbench_report_compilation_job SET state=CASE WHEN cancellation_requested=1 THEN 'cancelled' ELSE 'queued' END,worker_id=NULL,lease_token=NULL,lease_expires_at=NULL,input_fingerprint='',attempts=MAX(0,attempts-1),message=CASE WHEN cancellation_requested=1 THEN 'Compilation cancelled.' ELSE 'Compilation interrupted by worker shutdown.' END,finished_at=CASE WHEN cancellation_requested=1 THEN ? ELSE NULL END,updated_at=? WHERE job_id=? AND lease_token=?", (self.clock(), self.clock(), job.job_id, job.lease_token))


class ReportCompilationCoordinator:
    def __init__(self, jobs: ReportCompilationJobs, *, process: Callable, finish: Callable, workers: int = 1):
        if type(workers) is not int or workers < 1:
            raise ValueError("Compilation worker count must be positive.")
        self.jobs, self.process, self.finish = jobs, process, finish
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._active: dict[str, CompilationJobRecord] = {}
        self._threads = [threading.Thread(target=self._run, args=(f"compiler-{uuid.uuid4().hex}",), daemon=True) for _ in range(workers)]
        for thread in self._threads:
            thread.start()

    def notify(self):
        self._wake.set()

    def close(self):
        self._stop.set()
        self._wake.set()
        with self._lock:
            for job in tuple(self._active.values()):
                try:
                    self.jobs.release(job)
                except Exception:
                    pass
        for thread in self._threads:
            thread.join(timeout=2)

    def _run(self, worker_id):
        while not self._stop.is_set():
            try:
                job = self.jobs.claim(worker_id)
            except Exception:
                self._stop.wait(0.5)
                continue
            if job is None:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            with self._lock:
                if self._stop.is_set():
                    try:
                        self.jobs.release(job)
                    except CompilationLeaseLost:
                        pass
                    continue
                self._active[worker_id] = job
            done = threading.Event()
            lost = threading.Event()

            def renew(job=job, done=done, lost=lost):
                interval = max(0.01, self.jobs.policy.lease_seconds / 3)
                delay = interval
                known_deadline = job.lease_expires_at or self.jobs.clock()
                while not done.wait(delay) and not self._stop.is_set():
                    attempted_at = self.jobs.clock()
                    try:
                        if not self.jobs.heartbeat(job):
                            lost.set()
                            return
                    except sqlite3.OperationalError as exc:
                        if not _sqlite_contention(exc):
                            lost.set()
                            return
                        try:
                            durable_deadline = self.jobs.heartbeat_deadline(job)
                        except sqlite3.OperationalError as read_error:
                            if not _sqlite_contention(read_error):
                                lost.set()
                                return
                            # Even when a read is also busy, never wait beyond
                            # the conservative deadline of our last renewal.
                            durable_deadline = known_deadline
                        except Exception:
                            lost.set()
                            return
                        if durable_deadline is None or self.jobs.clock() >= durable_deadline:
                            lost.set()
                            return
                        known_deadline = durable_deadline
                        delay = min(0.1, interval, max(0.01, durable_deadline - self.jobs.clock()))
                        continue
                    except Exception:
                        lost.set()
                        return
                    # The UPDATE renews after attempted_at, so this is a lower
                    # bound if a later busy read cannot inspect its exact value.
                    known_deadline = max(known_deadline, attempted_at + self.jobs.policy.lease_seconds)
                    delay = interval

            heartbeat = threading.Thread(target=renew, daemon=True)
            heartbeat.start()
            cancelled = lambda job=job, lost=lost: self._stop.is_set() or lost.is_set() or self.jobs.cancelled(job)
            try:
                if cancelled():
                    raise CompilationLeaseLost("Compilation cancelled.")
                draft = self.process(job, cancelled)
                if cancelled():
                    raise CompilationLeaseLost("Compilation cancelled.")
                self.finish(job, draft)
                if self.jobs.get(job.matter_id, job.actor_id, job.job_id).state != "succeeded":
                    raise CompilationProblem("Compilation completion did not save the job atomically.")
            except Exception as exc:
                try:
                    message = str(exc) if isinstance(exc, CompilationProblem) else "Compilation could not finish. Retry the saved request."
                    self.jobs.fail(job, message)
                except Exception:
                    pass  # A fenced worker must never overwrite its replacement.
            finally:
                done.set()
                heartbeat.join(timeout=1)
                with self._lock:
                    self._active.pop(worker_id, None)
