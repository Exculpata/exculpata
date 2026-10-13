"""Durable, bounded background ingestion for the case workbench."""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Callable

from .pilot_uploads import PilotDocument, PilotStore, UploadProblem
from .retrieval_limits import INPUT_BLOCKED_STAGE, RetrievalInputProblem
from .source_locations import SourceLocationProblem, SourceLocationRegistry, SourceScanItem
from .workspace_store import IngestJobRecord, MatterRecord, WorkspaceStore


class IngestionCoordinator:
    """Claim SQLite jobs and run extraction/indexing outside request threads."""

    def __init__(
        self,
        workspace: WorkspaceStore,
        registry: SourceLocationRegistry,
        staging_root: Path,
        *,
        resolve_matter: Callable[[str], MatterRecord],
        resolve_store: Callable[[MatterRecord], PilotStore],
        index_document: Callable[[MatterRecord, PilotDocument], None],
        workers: int = 2,
    ) -> None:
        self.workspace = workspace
        self.registry = registry
        self.resolve_matter = resolve_matter
        self.resolve_store = resolve_store
        self.index_document = index_document
        self.worker_count = min(max(int(workers), 1), 4)
        self.staging_root = Path(staging_root)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        if self.staging_root.is_symlink() or not self.staging_root.is_dir():
            raise RuntimeError("ingestion staging directory is unsafe")
        self._reconcile_staging()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self.workspace.recover_running_ingest_jobs()
        for ordinal in range(1, self.worker_count + 1):
            thread = threading.Thread(
                target=self._run,
                name=f"case-ingest-{ordinal}",
                args=(f"ingest-worker-{ordinal}",),
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def _reconcile_staging(self) -> None:
        directory_fd = os.open(
            self.staging_root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        )
        try:
            with os.scandir(self.staging_root) as entries:
                for entry in entries:
                    if re.fullmatch(r"\.source-[0-9a-f]{32}\.part", entry.name):
                        os.unlink(entry.name, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)

    def notify(self) -> None:
        self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            thread.join(timeout=30)

    def _run(self, worker_id: str) -> None:
        while not self._stop.is_set():
            job = self.workspace.claim_ingest_job(worker_id)
            if job is None:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            self._process(job)

    def _remove_staged_source(self, staged_path: Path) -> None:
        """Durably remove one coordinator-owned staged copy before job completion."""

        if (
            staged_path.parent != self.staging_root
            or not re.fullmatch(r"\.source-[0-9a-f]{32}\.part", staged_path.name)
        ):
            raise RuntimeError("ingestion staging path is unsafe")
        directory_fd = os.open(
            self.staging_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            try:
                os.unlink(staged_path.name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _process(self, job: IngestJobRecord) -> None:
        staged_path: Path | None = None
        terminal: tuple[bool, str] | None = None
        try:
            matter = self.resolve_matter(job.matter_id)
            store = self.resolve_store(matter)
            document = store.get(job.document_id)
            staged_digest = ""
            if document.origin == "registered":
                if (
                    not job.source_location_id
                    or job.source_location_id != document.source_location_id
                    or job.relative_path != document.relative_path
                ):
                    raise SourceLocationProblem("The queued source metadata no longer matches this matter.")
                self.workspace.update_ingest_job(job.job_id, stage="Copying confirmed source")
                item = SourceScanItem(
                    document.relative_path,
                    Path(document.relative_path).name,
                    document.media_type,
                    document.size,
                    document.stable_device,
                    document.stable_inode,
                    document.stable_mtime_ns,
                )
                staged = self.registry.stage(
                    document.source_location_id, item, self.staging_root
                )
                staged_path = staged.path
                staged_digest = staged.sha256

            def progress(stage: str, completed: int, total: int) -> None:
                self.workspace.update_ingest_job(
                    job.job_id,
                    stage=stage,
                    completed_units=completed,
                    total_units=total,
                )

            document = store.process(
                job.document_id,
                staged_source=staged_path,
                staged_digest=staged_digest,
                progress=progress,
            )
            if document.state not in {"ready", "needs_ocr"}:
                raise UploadProblem(document.message or "Processing did not finish.")
            if document.state == "needs_ocr":
                terminal = (False, document.message)
            else:
                self.workspace.update_ingest_job(
                    job.job_id,
                    stage="Indexing for search",
                    completed_units=document.total_units,
                    total_units=document.total_units,
                )
                self.index_document(matter, document)
                terminal = (True, document.message)
        except RetrievalInputProblem as exc:
            terminal = (False, self._mark_failed(job, str(exc), stage=INPUT_BLOCKED_STAGE))
        except (UploadProblem, SourceLocationProblem) as exc:
            terminal = (False, self._mark_failed(job, str(exc)))
        except Exception:
            terminal = (
                False,
                self._mark_failed(job, "Processing did not finish. Choose Try again."),
            )
        finally:
            cleanup_complete = True
            if staged_path is not None:
                try:
                    self._remove_staged_source(staged_path)
                except Exception:
                    # Keep the durable job active so matter deletion cannot
                    # overtake an owned staging copy. Restart reconciliation
                    # removes it before running jobs are recovered.
                    cleanup_complete = False
            if cleanup_complete and terminal is not None:
                succeeded, message = terminal
                try:
                    self.workspace.finish_ingest_job(
                        job.job_id, succeeded=succeeded, message=message
                    )
                except Exception:
                    pass

    def _mark_failed(self, job: IngestJobRecord, message: str, *, stage: str = "Needs attention") -> str:
        safe_message = (message or "Processing did not finish. Choose Try again.")[:240]
        try:
            matter = self.resolve_matter(job.matter_id)
            self.resolve_store(matter).mark_failed(job.document_id, safe_message, stage=stage)
        except Exception:
            pass
        return safe_message
