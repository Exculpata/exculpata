"""Bounded ephemeral refresh jobs for a recomputed (never saved) draft."""
from threading import Lock
from .workspace_store import WorkspaceProblem

class TimelineJobs:
    def __init__(self):
        self._lock = Lock()
        self._states = {}

    def state(self, matter_id, actor):
        with self._lock:
            return self._states.get((matter_id, actor), '')

    def submit(self, background, matter_id, actor, calculate):
        key = (matter_id, actor)
        with self._lock:
            if self._states.get(key) == 'running':
                raise WorkspaceProblem('A draft refresh is already running.')
            if sum(v == 'running' for v in self._states.values()) >= 2:
                raise WorkspaceProblem('Draft refresh workers are busy. Try again shortly.')
            if len(self._states) >= 100:
                self._states = {k:v for k,v in self._states.items() if v == 'running'}
            self._states[key] = 'running'
        background.add_task(self._run, key, calculate)

    def _run(self, key, calculate):
        state = 'completed'
        try:
            calculate()
        except Exception:
            state = 'failed'
        finally:
            with self._lock:
                self._states[key] = state
