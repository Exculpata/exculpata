import threading

from case_intelligence.workflow_jobs import ResearchCoordinator
from case_intelligence.workspace_store import WorkspaceStore


def test_research_cancellation_checks_keep_their_claimed_job(tmp_path):
    store = WorkspaceStore(tmp_path / 'workbench.sqlite')
    try:
        actor = store.upsert_principal(
            'test', 'synthetic-researcher', 'Synthetic researcher', 'synthetic-researcher',
        ).principal_id
        matter = store.create_matter('Synthetic investigations', '', actor)
        for number in ('1', '2'):
            store.queue_research_job(
                matter.matter_id, actor, f'What happened in part {number}?',
                f'Synthetic investigation {number}', 'research-request-' + number * 32,
            )
        first_cancellation_check = None
        observations = []
        second_processed = threading.Event()

        def process(job, cancelled):
            nonlocal first_cancellation_check
            if first_cancellation_check is None:
                first_cancellation_check = cancelled
                store.cancel_research_job(matter.matter_id, actor, job.job_id)
            else:
                observations.append((first_cancellation_check(), cancelled()))
                second_processed.set()
            return {}

        coordinator = ResearchCoordinator(store, process=process)
        try:
            assert second_processed.wait(5), 'The coordinator did not process both investigations'
            assert observations == [(True, False)]
        finally:
            coordinator.close()
    finally:
        store.close()
