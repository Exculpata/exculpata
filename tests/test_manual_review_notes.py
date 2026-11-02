"""Synthetic human notes retain source support and the existing security boundary."""
import uuid
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import CaseIntelligenceWorkbench, create_workbench_app
from tests.test_assistant_passage_save import ACTOR, saved_answer, seed  # noqa: F401 - pytest fixture registration
from tests.test_saved_answer_report_support import cedar  # noqa: F401 - pytest fixture registration


def endpoint(bench, matter, document):
    return f'/matters/{matter.slug}/sources/{bench.source_store(matter).action_token(document)}/notes'


def fields(document, **extra):
    return dict(body='Human observation about the synthetic gauge.',
                source_version_id=document.version_id, source_basis=CaseIntelligenceWorkbench.source_note_basis(document), unit='1',
                request_key=str(uuid.uuid4()), **extra)


def test_human_note_is_source_linked_and_repeat_is_idempotent(saved_answer):
    client, (matter, _, _, document, _) = saved_answer
    bench = client.app.state.workbench
    url, data = endpoint(bench, matter, document), fields(document)
    response = client.post(url, data=data, headers={'Accept': 'application/json'})
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    item = bench.workspace.notebook_item(matter.matter_id, ACTOR, response.json()['item_id'])
    assert item.origin == 'manual'
    assert item.status == 'needs_review'
    assert item.body == data['body']
    refs = bench.workspace.notebook_references(matter.matter_id, ACTOR, item.item_id)
    assert len(refs) == 1
    assert refs[0].source_version_id == document.version_id
    repeat = client.post(url, data=data, headers={'Accept': 'application/json'})
    assert repeat.status_code == 200
    assert repeat.json()['created'] is False
    assert repeat.json()['item_id'] == item.item_id
    assert len(bench.workspace.all_notebook_items(matter.matter_id, ACTOR)) == 1
    bench.workspace.set_notebook_item_status(matter.matter_id, ACTOR, item.item_id, 'confirmed', expected_updated_at=item.updated_at)
    reviewed = client.post(url, data=data, headers={'Accept': 'application/json'})
    assert reviewed.json()['status'] == 'confirmed'
    changed = client.post(url, data={**data, 'body': 'Changed retry body.'}, headers={'Accept': 'application/json'})
    assert changed.status_code == 409
    distinct = client.post(url, data={**data, 'request_key': str(uuid.uuid4())}, headers={'Accept': 'application/json'})
    assert distinct.status_code == 200 and distinct.json()['created'] is True
    assert distinct.json()['item_id'] != item.item_id
    assert len(bench.workspace.all_notebook_items(matter.matter_id, ACTOR)) == 2


@pytest.mark.parametrize('override', [dict(source_version_id='unavailable-version'), dict(source_basis='f' * 64), dict(unit='99999'), dict(body=''), dict(body='x' * 20001)])
def test_invalid_source_or_body_never_writes(saved_answer, override):
    client, (matter, _, _, document, _) = saved_answer
    bench = client.app.state.workbench
    response = client.post(endpoint(bench, matter, document), data={**fields(document), **override}, headers={'Accept': 'application/json'})
    assert response.status_code in (400, 404, 409, 422)
    assert not bench.workspace.all_notebook_items(matter.matter_id, ACTOR)


def test_note_access_and_matter_isolation(saved_answer):
    client, (matter, _, _, document, _) = saved_answer
    bench = client.app.state.workbench
    other = bench.create_matter('Synthetic unrelated review', 'Isolation fixture', ACTOR)
    url = endpoint(bench, matter, document)
    assert client.post(url.replace(matter.slug, other.slug), data=fields(document), headers={'Accept': 'application/json'}).status_code in (403, 404)
    bench.workspace.revoke_member(matter.matter_id, ACTOR, 'synthetic-dock-owner')
    assert client.post(url, data=fields(document), headers={'Accept': 'application/json'}).status_code in (403, 404)
    assert not bench.workspace.all_notebook_items(matter.matter_id, 'synthetic-dock-owner')
    assert not bench.workspace.all_notebook_items(other.matter_id, ACTOR)


def test_human_note_requires_session_csrf(tmp_path, monkeypatch):
    from tests.test_identity_membership_audit import _login
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    with TestClient(create_workbench_app(tmp_path / 'runtime', generator=UnavailableGenerator(), auth_mode='preview')) as client:
        _, csrf = _login(client, 'taylor-morgan')
        matter, _, _, document, _ = seed(client.app.state.workbench)
        bench = client.app.state.workbench
        url, data = endpoint(bench, matter, document), fields(document)
        for token in (None, 'invalid'):
            submitted = data if token is None else {**data, 'csrf_token': token}
            assert client.post(url, data=submitted, headers={'Accept': 'application/json'}).status_code == 403
        assert not bench.workspace.all_notebook_items(matter.matter_id, ACTOR)
        assert client.post(url, data={**data, 'csrf_token': csrf}, headers={'Accept': 'application/json'}).status_code == 200


@pytest.mark.parametrize('return_to', ['https://example.invalid/', '//example.invalid/', '/matters/other/sources'])
def test_note_fallback_cannot_leave_matter(saved_answer, return_to):
    client, (matter, _, _, document, _) = saved_answer
    response = client.post(endpoint(client.app.state.workbench, matter, document), data=fields(document, return_to=return_to), follow_redirects=False)
    assert response.status_code == 303
    destination = urlsplit(response.headers['location'])
    assert not destination.netloc
    assert destination.path.startswith(f'/matters/{matter.slug}')


def test_pdf_page_and_media_time_are_saved_as_provenance(cedar):
    client, bench, matter, documents = cedar
    for kind in ('pdf', 'transcript'):
        document = documents[kind]
        data = fields(document, start_ms='1000') if kind == 'transcript' else fields(document)
        response = client.post(endpoint(bench, matter, document), data=data, headers={'Accept': 'application/json'})
        assert response.status_code == 200, response.text
        ref = bench.workspace.notebook_references(matter.matter_id, ACTOR, response.json()['item_id'])[0]
        assert ref.source_version_id == document.version_id
        if kind == 'pdf':
            assert ref.unit_number == 1
            assert ref.location == 'Page 1'
        else:
            citation = bench.support(matter, ref.support_token)
            assert citation.start_ms <= 1000 < citation.end_ms


def test_source_scope_json_selects_open_source_without_answer_relabel(saved_answer):
    client, (matter, conversation, _, document, _) = saved_answer
    bench = client.app.state.workbench
    url = endpoint(bench, matter, document).removesuffix('/notes') + '/ask'
    assert client.get(url.removesuffix('/ask')).status_code == 200
    response = client.post(url, headers={'Accept': 'application/json'})
    assert response.status_code == 200, response.text
    assert response.json()['source_set_id']
    assert document.display_name in response.json()['name']
    history = bench.workspace.messages(matter.matter_id, conversation.conversation_id)
    assert len(history) == 1


@pytest.mark.parametrize('page', ['²', '9' * 50, 'nonnumeric', '2'])
def test_failed_non_js_note_keeps_draft_and_retry_identity(saved_answer, page):
    client, (matter, _, _, document, _) = saved_answer
    data = {**fields(document), 'source_basis': 'f' * 64, 'return_to': f'/matters/{matter.slug}/sources?page={page}'}
    response = client.post(endpoint(client.app.state.workbench, matter, document), data=data, follow_redirects=False)
    assert response.status_code == 409
    assert data['body'] in response.text
    assert data['request_key'] in response.text
    assert not client.app.state.workbench.workspace.all_notebook_items(matter.matter_id, ACTOR)


def test_same_version_transcript_correction_rejects_old_note_basis(cedar):
    client, bench, matter, documents = cedar
    document = documents['transcript']
    original_version = document.version_id
    data = fields(document, start_ms='1000')
    segment = bench.workspace.transcript_segments(matter.matter_id, document.document_id, document.version_id)[0]
    url = endpoint(bench, matter, document)
    corrected = client.post(url.removesuffix('/notes') + f'/segments/{segment.segment_id}', data={
        'expected_revision': '0', 'text': 'A corrected synthetic transcript observation.'}, follow_redirects=False)
    assert corrected.status_code == 303
    assert document.version_id == original_version
    response = client.post(url, data=data, headers={'Accept': 'application/json'})
    assert response.status_code == 409
    assert not bench.workspace.all_notebook_items(matter.matter_id, ACTOR)
