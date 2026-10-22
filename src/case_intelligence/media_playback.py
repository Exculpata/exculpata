"""Matter-owned browser playback preparation for uploaded video sources."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .pilot_uploads import PilotDocument, PilotStore, UploadProblem
from .workspace_store import MatterRecord


@dataclass(frozen=True)
class PlaybackTask:
    matter_id: str
    document_id: str
    source_version_id: str
    force_full: bool = False


class BrowserPlaybackCoordinator:
    """Serialize compatibility renditions and stop safely before matter purge."""

    def __init__(
        self,
        *,
        resolve_matter: Callable[[str], MatterRecord],
        resolve_store: Callable[[MatterRecord], PilotStore],
        reserve_capacity: Callable[[str, str, int], None],
        release_capacity: Callable[[str, str], None],
        admit_matter: Callable[[MatterRecord], bool],
    ) -> None:
        self.resolve_matter = resolve_matter
        self.resolve_store = resolve_store
        self.reserve_capacity = reserve_capacity
        self.release_capacity = release_capacity
        self.admit_matter = admit_matter
        self._condition = threading.Condition()
        self._queue: deque[PlaybackTask] = deque()
        self._pending: set[PlaybackTask] = set()
        self._cancelled_matters: set[str] = set()
        self._cancelled_documents: set[tuple[str, str]] = set()
        self._active: PlaybackTask | None = None
        self._stop = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"{LEGACY_STEM}-playback-1",
            daemon=True,
        )
        self._thread.start()

    def ensure(
        self,
        matter: MatterRecord,
        document: PilotDocument,
        *,
        retry: bool = False,
    ) -> PilotDocument:
        store = self.resolve_store(matter)
        with store.mutation_guard():
            if not self.admit_matter(matter):
                raise UploadProblem(
                    "This matter is closing and cannot start playback preparation.",
                    409,
                )
            force_full = retry and document.playback_state in {"original", "ready"}
            queued = store.queue_browser_playback(document.document_id, retry=retry)
            if queued.playback_state != "queued":
                return queued
            task = PlaybackTask(
                matter.matter_id,
                queued.document_id,
                queued.version_id,
                force_full,
            )
            with self._condition:
                if (
                    not self._stop
                    and matter.matter_id not in self._cancelled_matters
                    and (matter.matter_id, queued.document_id)
                    not in self._cancelled_documents
                    and task not in self._pending
                    and task != self._active
                ):
                    self._queue.append(task)
                    self._pending.add(task)
                    self._condition.notify_all()
            return queued

    def _cancelled(self, matter_id: str, document_id: str) -> bool:
        with self._condition:
            return (
                self._stop
                or matter_id in self._cancelled_matters
                or (matter_id, document_id) in self._cancelled_documents
            )

    def has_matter_work(self, matter_id: str) -> bool:
        """Report queued or active rendition work without changing it."""

        with self._condition:
            return bool(
                (self._active is not None and self._active.matter_id == matter_id)
                or any(task.matter_id == matter_id for task in self._pending)
                or any(task.matter_id == matter_id for task in self._queue)
            )

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._stop:
                    self._condition.wait(timeout=0.5)
                if self._stop:
                    return
                task = self._queue.popleft()
                self._pending.discard(task)
                if task.matter_id in self._cancelled_matters:
                    continue
                self._active = task
            reserved = False
            try:
                matter = self.resolve_matter(task.matter_id)
                store = self.resolve_store(matter)
                document = store.get(task.document_id)
                if document.version_id != task.source_version_id:
                    continue
                requested = store.playback_reservation_bytes(task.document_id)
                self.reserve_capacity(task.matter_id, task.document_id, requested)
                reserved = True
                store.prepare_browser_playback(
                    task.document_id,
                    task.source_version_id,
                    maximum_output_bytes=requested,
                    cancelled=lambda: self._cancelled(
                        task.matter_id, task.document_id
                    ),
                    force_full=task.force_full,
                )
            except UploadProblem:
                try:
                    matter = self.resolve_matter(task.matter_id)
                    self.resolve_store(matter).fail_browser_playback(
                        task.document_id,
                        "The original video is retained, but protected storage capacity is not available for a browser-compatible copy.",
                    )
                except Exception:
                    pass
            except Exception:
                try:
                    matter = self.resolve_matter(task.matter_id)
                    self.resolve_store(matter).fail_browser_playback(
                        task.document_id,
                        "The original video is retained, but browser playback preparation did not finish. Try again.",
                    )
                except Exception:
                    pass
            finally:
                if reserved:
                    self.release_capacity(task.matter_id, task.document_id)
                with self._condition:
                    self._active = None
                    self._condition.notify_all()

    def cancel_matter(self, matter_id: str, *, timeout: float = 5.0) -> bool:
        with self._condition:
            self._cancelled_matters.add(matter_id)
            self._queue = deque(
                task for task in self._queue if task.matter_id != matter_id
            )
            self._pending = {
                task for task in self._pending if task.matter_id != matter_id
            }
            self._condition.notify_all()
            deadline = time.monotonic() + max(float(timeout), 0)
            while self._active is not None and self._active.matter_id == matter_id:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
        return True

    def cancel_document(
        self, matter_id: str, document_id: str, *, timeout: float = 5.0
    ) -> bool:
        key = (matter_id, document_id)
        with self._condition:
            self._cancelled_documents.add(key)
            self._queue = deque(
                task
                for task in self._queue
                if (task.matter_id, task.document_id) != key
            )
            self._pending = {
                task
                for task in self._pending
                if (task.matter_id, task.document_id) != key
            }
            self._condition.notify_all()
            deadline = time.monotonic() + max(float(timeout), 0)
            while self._active is not None and (
                self._active.matter_id, self._active.document_id
            ) == key:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
        return True

    def close(self) -> None:
        with self._condition:
            self._stop = True
            self._condition.notify_all()
        self._thread.join(timeout=5)
