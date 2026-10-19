"""Synthetic guided discovery: incomplete extraction, retry, citations and human choice."""

from tests.fixture_write import fixture_write
from urllib.parse import parse_qs

from fastapi.testclient import TestClient

from case_intelligence.entity_discovery import EntityDiscovery
from case_intelligence.entity_extractor import DeterministicEntityExtractor
from case_intelligence.full_text_review import FullTextReviewLedger
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workspace_store import WorkspaceProblem
from tests.test_matter_notebook import _create_matter, WEB_ACTOR


def test_guided_partial_discovery_retry_and_explicit_acceptance(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    with TestClient(create_workbench_app(tmp_path, auth_mode='test')) as client:
        slug = _create_matter(client)
        prefix = f'/matters/{slug}'
        empty = client.get(prefix + '/entities')
        assert 'No frozen text-review population yet' in empty.text
        assert 'opening this page starts no analysis' in empty.text
        for name in ('one', 'two'):
            client.post(prefix + '/uploads', files=[('files', (f'Generated {name}.txt',
                b'person: Alex Example\nDate: 03/04/2026', 'text/plain'))])
        bench = client.app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        bench.full_review.close()
        store = bench.workspace
        _, version = store.create_review_criterion(matter.matter_id, WEB_ACTOR,
            title='Synthetic guided review', instructions='Review synthetic people and dates.')
        run = store.queue_review_run(matter.matter_id, WEB_ACTOR, version.criterion_version_id,
            run_kind='full', review_mode='full_text')
        run = store.claim_review_run('synthetic-guided-inventory')
        decision = store.next_review_decision(run.run_id)
        document = bench.source_store(matter).get(decision.document_id)
        FullTextReviewLedger(store).inventory(run, decision, document.parsed_units(), current_source=lambda: True)
        store.fail_review_run(run.run_id, 'Synthetic partial inventory; no generation requested.')
        path = prefix + '/entity-discovery/' + run.run_id
        service = bench.entity_service(matter)
        page = client.get(path)
        assert page.status_code == 200
        assert '1 not yet inventoried' in page.text and 'Discover next 10 units' in page.text
        assert service.list(matter.matter_id, WEB_ACTOR)[1] == 0
        assert path in client.get(prefix + '/full-review/' + run.run_id + '/text').text
        # A newer criterion must not make the resume link select the wrong rule.
        store.create_review_criterion(matter.matter_id, WEB_ACTOR,
            title='Another synthetic criterion', instructions='A separate review.')
        import html
        import re
        resume_url = html.unescape(re.search(r'href="([^"]+)">Resume or start a text review', page.text)[1])
        assert 'criterion=' + run.criterion_id in resume_url
        assert client.get(resume_url).status_code == 200

        class Failing(DeterministicEntityExtractor):
            def extract(self, text):
                raise ValueError('Synthetic failure')

        original_extract = DeterministicEntityExtractor.extract
        monkeypatch.setattr(DeterministicEntityExtractor, 'extract', Failing.extract)
        context = prefix + '?mode=search&q=Alex'
        fields = dict(action='discover', run_id=run.run_id, return_to=context, q='Alex')
        failed = client.post(prefix + '/entities/actions', data=fields)
        assert failed.url.path == path
        assert 'Retry failed discovery' in failed.text
        assert service.list(matter.matter_id, WEB_ACTOR)[1] == 0
        monkeypatch.setattr(DeterministicEntityExtractor, 'extract', original_extract)
        done = client.post(prefix + '/entities/actions', data=dict(fields, action='retry_discovery'))
        assert done.url.path == path and parse_qs(done.url.query.decode())['return_to'] == [context]
        assert 'No runnable units remain' in done.text and '1 not yet inventoried' in done.text
        assert 'value="discover"' not in done.text and 'value="retry_discovery"' not in done.text
        rows, total = service.list(matter.matter_id, WEB_ACTOR)
        assert total == 2 and all(row['status'] == 'suggested' for row in rows)
        date = next(row for row in rows if row['entity_type'] == 'date')
        person = next(row for row in rows if row['entity_type'] == 'person')
        detail = client.get(prefix + '/entities/' + date['entity_id'])
        assert 'unresolved' in detail.text and 'Create event or assertion with this passage' in detail.text
        mention = service.detail(matter.matter_id, WEB_ACTOR, date['entity_id'])[1][0]
        form = client.get(prefix + '/assertions/new', params=dict(entity_id=date['entity_id'], support=mention['support_token']))
        assert 'Selected original passage' in form.text and '03/04/2026' in form.text
        assert 'No events or assertions yet' in client.get(prefix + '/chronology').text
        client.post(prefix + '/entities/actions', data=dict(action='update', entity_id=person['entity_id'],
            expected_revision=person['revision'], display_name='Alex Example', entity_type='person', status='confirmed'))
        entity, mentions, *_ = service.detail(matter.matter_id, WEB_ACTOR, person['entity_id'])
        assert entity['status'] == 'confirmed' and mentions[0]['review_status'] == 'suggested'
        client.post(prefix + '/entities/actions', data=dict(action='review_mention', entity_id=entity['entity_id'],
            expected_revision=entity['revision'], mention_id=mentions[0]['mention_id'], status='confirmed'))
        assert service.detail(matter.matter_id, WEB_ACTOR, person['entity_id'])[1][0]['review_status'] == 'confirmed'
        client.post(prefix + '/entities/actions', data=fields)
        assert service.list(matter.matter_id, WEB_ACTOR)[1] == 2

        def capacity_failure(*args, **kwargs):
            raise WorkspaceProblem('Entity discovery byte budget reached.')
        monkeypatch.setattr(EntityDiscovery, 'step', capacity_failure)
        error = client.post(prefix + '/entities/actions', data=fields)
        assert error.status_code == 400 and 'Saved progress is retained' in error.text
        assert '1 not yet inventoried' in error.text and 'operator attention' in error.text
        assert 'data-discovery-unit' in error.text
        assert client.get(prefix + '/entity-discovery/missing').status_code == 404
        # A formerly extracted source can become unavailable after processing.
        with fixture_write(store):
            store.connection.execute("UPDATE workbench_source_catalog SET source_state='unavailable' WHERE document_id=?",
                                     (document.document_id,))
        changed = client.get(path)
        assert '1 changed' in changed.text and 'No runnable units remain' in changed.text
        assert 'value="retry_discovery"' not in changed.text
        assert 'new current-source review' in changed.text
        assert service.detail(matter.matter_id, WEB_ACTOR, person['entity_id'])[0]['status'] == 'confirmed'
