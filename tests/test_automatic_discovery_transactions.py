"""Overlap the readiness trace with a real idle report coordinator claim."""
import threading

from tests import test_automatic_discovery as discovery
from tests.test_automatic_discovery import workbench  # noqa: F401


def test_readiness_trace_excludes_background_report_claim(workbench, monkeypatch):
    from case_intelligence.entity_discovery import EntityDiscovery

    _, bench, _, _ = workbench
    jobs = bench.report_compilation.jobs
    original_claim = jobs.claim
    original_progress = EntityDiscovery.automatic_progress
    requested = threading.Event()
    completed = threading.Event()
    claim_threads = []

    def observed_claim(worker_id):
        overlap = requested.is_set()
        result = original_claim(worker_id)
        if overlap:
            assert result is None, "the synthetic report queue must be empty"
            claim_threads.append(threading.get_ident())
            completed.set()
        return result

    def overlapping_progress(self, *args, **kwargs):
        # The readiness test has installed its connection trace at this boundary.
        # Keep the actual coordinator alive and let its claim finish first.
        requested.set()
        bench.report_compilation.coordinator.notify()
        assert completed.wait(5), "the report coordinator did not complete a claim"
        assert all(thread != threading.get_ident() for thread in claim_threads)
        return original_progress(self, *args, **kwargs)

    monkeypatch.setattr(jobs, "claim", observed_claim)
    monkeypatch.setattr(EntityDiscovery, "automatic_progress", overlapping_progress)
    discovery.test_readiness_progress_never_scans_the_coverage_ledger(workbench)
    assert claim_threads, "the real background claim must be exercised"
