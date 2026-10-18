"""Workspace-before-cache ordering for shared live source stores."""
from contextlib import contextmanager
import os
import threading


class SourceCacheLock:
    """Check ownership before blocking, including direct/legacy acquisitions."""

    def __init__(self, workspace_lock):
        # Resolve the active workspace, including after a restore/reopen.
        self._workspace_lock = workspace_lock
        self._lock = threading.RLock()
        self._check_order = os.environ.get("CASE_INTELLIGENCE_TEST_MODE", "").strip() == "1"

    def acquire(self, *args, **kwargs):
        if self._check_order and not self._workspace_lock()._is_owned():
            raise RuntimeError("source_cache_guard: acquire the workspace lock before the source cache")
        return self._lock.acquire(*args, **kwargs)

    def release(self):
        self._lock.release()

    def _is_owned(self):
        return self._lock._is_owned()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


@contextmanager
def source_cache_guard(bench):
    """Acquire workspace then cache; callers take any matter source guard first.

    Never acquire a different matter's source guard inside this boundary. Close
    detached stores after leaving it, since closing takes their source guards.
    """
    if (bench._store_lock._check_order and bench._store_lock._is_owned()
            and not bench.workspace._lock._is_owned()):
        raise RuntimeError("source_cache_guard: source cache was acquired before the workspace lock")
    with bench.workspace._lock, bench._store_lock:
        yield
