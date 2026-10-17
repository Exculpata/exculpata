"""Bounded, disposable summary computations; no source text is retained in jobs."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from uuid import uuid4

from .workspace_store import WorkspaceProblem


@dataclass
class SummaryJob:
    job_id: str
    key: tuple
    started: float
    state: str = 'queued'
    message: str = ''


class SummaryJobs:
    """A restart discards these refreshable computations, never saved work."""
    def __init__(self):
        self.lock = Lock()
        self.jobs = {}
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='production-summary')

    def start(self, key, compute):
        with self.lock:
            self.jobs = {key: job for key, job in self.jobs.items()
                         if job.state in ('queued', 'running') or monotonic() - job.started < 900}
            for job in self.jobs.values():
                if job.key == key and job.state in ('queued', 'running'):
                    return job.job_id
            if len(self.jobs) >= 64 or sum(job.state in ('queued', 'running') for job in self.jobs.values()) >= 8:
                raise WorkspaceProblem('Summary capacity is busy. Try again after a running summary finishes.')
            job = SummaryJob(uuid4().hex, key, monotonic())
            self.jobs[job.job_id] = job
            self.executor.submit(self._run, job, compute)
            return job.job_id

    def _run(self, job, compute):
        with self.lock:
            job.state = 'running'
        try:
            compute()  # Validate bounded completeness; do not retain source text.
        except WorkspaceProblem as exc:
            state, message = 'failed', str(exc)
        except Exception:
            state, message = 'failed', 'The summary could not be read. Review processing status and try again.'
        else:
            state, message = 'completed', ''
        with self.lock:
            job.state, job.message = state, message

    def get(self, key, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job.key != key or monotonic() - job.started >= 900:
                raise KeyError(job_id)
            return dict(job_id=job.job_id, state=job.state, message=job.message)

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)
