"""Synthetic reconstruction at a fully written, unfinished order checkpoint."""
import json

import pytest

from case_intelligence.modules.discovery_review import fields
from case_intelligence.modules.discovery_review.stored_index.rebuild import Service, work_bound
from tests.test_discovery_review_index import add_population, manifest
from tests.test_discovery_review_index_updates import pending_job
from tests.test_discovery_review_list import production as production


def test_full_value_checkpoint_reconstructs_and_finalizes_once(production, monkeypatch):
    client, bench, matter = production
    store = bench.source_store(matter)
    add_population(store, 28)
    service, job = pending_job(production, monkeypatch)
    _, _, limit = work_bound(**manifest(32))
    for _ in range(limit):
        service.advance(job, 1)
        assert not job.error, getattr(service, 'failure', '')
        assert job.last_budget.touches <= 256
        state = service.repository.state(job.matter_id)
        cursor = json.loads(state['checkpoint'])
        active = cursor['active']
        value_count = 2 * (1 + len(job.definition.fields) + len(json.loads(state['summaries'])['headers']))
        if cursor['phase'] == 'order' and active and active['value_eof'] and active['value_position'] == value_count:
            break
        if cursor['phase'] == 'complete':
            pytest.fail('no durable full-value continuation was published')
    else:
        pytest.fail('full-value continuation exhausted the generated manifest')

    document_id = active['document_id']
    value_query = ('SELECT field_key,direction,leading_key,sort_key,value,present '
                   'FROM workbench_discovery_index_value WHERE matter_id=? AND document_id=? '
                   'ORDER BY field_key,direction')
    values = service.repository.rows(value_query, (job.matter_id, document_id))
    assert len(values) == value_count
    assert service.repository.document_read(job.matter_id, document_id)[4] == 'pending'
    context, identifiers, codes = service.context, service.identifiers, service.codes
    service.cancel(job.matter_id)
    restored = Service(context, identifiers, codes)
    assert restored.repository.state(job.matter_id) == state
    finalized = []
    finish_order = Service.finish_order

    def counted_finish(owner, tx, current_job, cursor, prepared, cache, item, fingerprint):
        finish_order(owner, tx, current_job, cursor, prepared, cache, item, fingerprint)
        finalized.append(item.document_id)

    monkeypatch.setattr(Service, 'finish_order', counted_finish)
    view = restored.current(job.matter_id, store, fields.load_definition())
    assert view.complete and view.state['indexed'] == 32 and not view.state['errors']
    assert finalized.count(document_id) == 1
    assert restored.repository.document_read(job.matter_id, document_id)[4] == 'complete'
    assert restored.repository.rows(value_query, (job.matter_id, document_id)) == values
    completed = restored.repository.state(job.matter_id)
    finalized_before = tuple(finalized)
    assert restored.current(job.matter_id, store, fields.load_definition()).complete
    assert restored.repository.state(job.matter_id) == completed
    assert tuple(finalized) == finalized_before
