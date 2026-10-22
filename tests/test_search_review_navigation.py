"""Synthetic search-to-source-to-question navigation with matter-local returns."""

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import html
import io
import re
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from case_intelligence.workbench import create_workbench_app


class SyntheticAnswerGenerator:
    available = True

    def generate(self, **kwargs):
        evidence = kwargs['evidence']
        return {'answerable': bool(evidence), 'claims': [
            {'text': item.excerpt, 'evidence_ids': [item.evidence_id]}
            for item in evidence[:1]], 'limitation': None, 'missing_information': ''}


def test_first_search_page_return_detects_changed_population(tmp_path):
    app = create_workbench_app(tmp_path / 'runtime', auth_mode='test', background_ingestion=False)
    with TestClient(app) as client:
        created = client.post('/matters', data={'name': 'Synthetic search population'}, follow_redirects=False)
        slug = created.headers['location'].split('/')[2]
        bench = app.state.workbench
        matter = bench.matter(slug, 'development-taylor-morgan')
        store = bench.source_store(matter)
        store.store_stream('First.txt', 'text/plain', io.BytesIO(b'An amber bicycle arrived.'))
        results = client.get(f'/matters/{slug}/exact-search?words=amber')
        link = html.unescape(re.search(r'class="find-open-source" href="([^"]+)"', results.text)[1])
        origin = parse_qs(urlsplit(link).query)['entity_return_to'][0]
        assert parse_qs(urlsplit(origin).query)['fingerprint']
        assert client.get(link).status_code == 200
        assert client.get(origin).status_code == 200
        store.store_stream('Later.txt', 'text/plain', io.BytesIO(b'Another amber bicycle arrived.'))
        changed = client.get(origin)
        assert changed.status_code == 409
        assert 'Your sources changed' in changed.text


@pytest.mark.parametrize('origin', ['search', 'https://example.com/', '/matters/another/exact-search', '/matters/{slug}/../another'])
def test_search_inspection_and_question_return_is_matter_local(tmp_path, origin):
    app = create_workbench_app(tmp_path / 'runtime', auth_mode='test', background_ingestion=False,
                               generator=SyntheticAnswerGenerator())
    with TestClient(app) as client:
        created = client.post('/matters', data={'name': 'Synthetic navigation'}, follow_redirects=False)
        slug = created.headers['location'].split('/')[2]
        bench = app.state.workbench
        matter = bench.matter(slug, 'development-taylor-morgan')
        bench.source_store(matter).store_stream('Synthetic neutral.txt', 'text/plain', io.BytesIO(b'An amber bicycle arrived.'))
        search = f'/matters/{slug}/exact-search?words=amber&page_size=25'
        results = client.get(search)
        assert '1 source found' in results.text
        link = html.unescape(re.search(r'class="find-open-source" href="([^"]+)"', results.text)[1])
        search = parse_qs(urlsplit(link).query)['entity_return_to'][0]
        assert parse_qs(urlsplit(search).query)['fingerprint']
        assert parse_qs(urlsplit(search).query)['words'] == ['amber']
        source = client.get(link)
        assert html.escape(search, quote=True) in source.text
        action = html.unescape(re.search(r'<form method="post" action="([^"]+/ask[^\"]*)"', source.text)[1])
        path = urlsplit(action).path
        response = client.post(path, params={'entity_return_to': search if origin == 'search' else origin.format(slug=slug)}, follow_redirects=False)
        query = parse_qs(urlsplit(response.headers['location']).query)
        assert 'source_set' in query
        assert query.get('entity_return_to', []) == ([search] if origin == 'search' else [])
        conversation = client.get(response.headers['location'])
        assert 'id="conversation-heading">Ask</h2>' in conversation.text
        assert 'Only · Synthetic neutral.txt' in conversation.text
        form_action = html.unescape(re.search(r'<form class="question-composer" method="post" action="([^"]+)"', conversation.text)[1])
        expected = [search] if origin == 'search' else []
        assert parse_qs(urlsplit(form_action).query).get('entity_return_to', []) == expected
        submitted = client.post(form_action, data={'question': 'What arrived?', 'source_set': query['source_set'][0]},
                                headers={'Accept': 'application/json'})
        assert submitted.status_code == 202
        job = submitted.json()
        for key in ('status_url', 'result_url', 'workspace_url', 'cancel_url', 'retry_url', 'fragment_url', 'open_url'):
            assert parse_qs(urlsplit(job[key]).query).get('entity_return_to', []) == expected
        deadline = time.monotonic() + 10
        while job['state'] not in ('succeeded', 'failed', 'cancelled') and time.monotonic() < deadline:
            time.sleep(.05)
            job = client.get(job['status_url']).json()
        assert job['state'] == 'succeeded', job
        completed = client.get(job['result_url'], follow_redirects=False)
        assert parse_qs(urlsplit(completed.headers['location']).query).get('entity_return_to', []) == expected
        page = client.get(completed.headers['location'])
        assert ('Return to review context' in page.text) == bool(expected)
        citations = re.findall(r'class="citation[^\"]*" href="([^\"]+)"', page.text)
        assert citations
        for citation in citations:
            target = html.unescape(citation)
            assert parse_qs(urlsplit(target).query).get('entity_return_to', []) == expected
            assert ('Return to review context' in client.get(target).text) == bool(expected)
        state_action = html.unescape(re.search(r'action="([^\"]+/review-state[^\"]*)"', source.text)[1])
        for state in ('flagged', 'invalid'):
            changed = client.post(state_action, data={'state': state}, follow_redirects=False)
            assert parse_qs(urlsplit(changed.headers['location']).query)['entity_return_to'] == [search]

        form_action = html.unescape(re.search(r'<form class="question-composer" method="post" action="([^"]+)"', page.text)[1])
        bench.research.close()
        bench.research = None
        research = client.post(form_action, data={'question': 'Investigate the arrival', 'review_task': 'research',
            'conversation': job['conversation_id']}, follow_redirects=False)
        assert research.status_code == 303
        assert parse_qs(urlsplit(research.headers['location']).query).get('entity_return_to', []) == expected
        research_page = client.get(research.headers['location'])
        status_url = html.unescape(re.search(r'data-workflow-monitor data-status-url="([^"]+)"', research_page.text)[1])
        status = client.get(status_url).json()
        assert parse_qs(urlsplit(status['result_url']).query).get('entity_return_to', []) == expected
        details = client.get(status['result_url'])
        cancel = html.unescape(re.search(r'action="([^"]+/cancel[^\"]*)"', details.text)[1])
        assert parse_qs(urlsplit(cancel).query).get('entity_return_to', []) == expected
        cancelled = client.post(cancel, follow_redirects=False)
        assert cancelled.status_code == 303
        assert parse_qs(urlsplit(cancelled.headers['location']).query).get('entity_return_to', []) == expected
        details = client.get(cancelled.headers['location'])
        resume = html.unescape(re.search(r'action="([^"]+/retry[^\"]*)"', details.text)[1])
        assert parse_qs(urlsplit(resume).query).get('entity_return_to', []) == expected
        resumed = client.post(resume, follow_redirects=False)
        assert resumed.status_code == 303
        assert parse_qs(urlsplit(resumed.headers['location']).query).get('entity_return_to', []) == expected
        assert bench.workspace.research_job(matter.matter_id, matter.owner_id, status['job_id']).state == 'queued'
        # An invalid extension keeps the same origin on its error redirect.
        rejected = client.post(resume, data={"additional_passes": 999}, follow_redirects=False)
        rejected_query = parse_qs(urlsplit(rejected.headers['location']).query)
        assert rejected_query.get('entity_return_to', []) == expected
        assert rejected_query.get('error')


def test_readonly_administrator_search_hides_source_question(tmp_path):
    from tests.test_matter_management import ADMIN, OWNER, _app, _headers, _principal_id
    with TestClient(_app(tmp_path), base_url=f"https://{LEGACY_STEM}.example.test") as client:
        client.get('/auth/login', headers=_headers(OWNER))
        owner = _principal_id(client, OWNER)
        bench = client.app.state.workbench
        matter = bench.workspace.create_matter('Synthetic search access', 'Synthetic', owner)
        bench.source_store(matter).store_stream('Synthetic.txt', 'text/plain', io.BytesIO(b'An amber bicycle.'))
        path = f'/matters/{matter.slug}/exact-search?words=amber'
        assert 'Ask using this source' in client.get(path, headers=_headers(OWNER)).text
        client.cookies.clear()
        client.get('/auth/login', headers=_headers(ADMIN))
        page = client.get(path, headers=_headers(ADMIN))
        assert page.status_code == 200
        assert '1 source found' in page.text
        assert 'Ask using this source' not in page.text
