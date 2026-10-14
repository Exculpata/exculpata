"""Synthetic regression: suggest people, things and dates from an answer's cited passages."""
import copy
import html
import io
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from case_intelligence.entity_discovery import DiscoveryBudgetFull, EntityDiscovery
from case_intelligence.entity_repository import ANSWER_HISTORY_ACTION
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app

ACTOR = 'development-taylor-morgan'
OWNER = 'synthetic-suggest-owner'
TEXT = b'witness: Alex Example\nThe crate was signed for on 2026-03-05.\nbadge: AB-1234\n'
JSON = {'Accept': 'application/json'}


def seed(bench, text=TEXT, *, extra=None):
    """A matter with one synthetic answer whose claim cites the first unit of each source."""
    bench.workspace.upsert_principal('test', OWNER, 'Synthetic Owner', OWNER, preferred_principal_id=OWNER)
    matter = bench.create_matter('Synthetic answer suggestions', 'Generated evidence', OWNER)
    bench.workspace.add_member(matter.matter_id, ACTOR, OWNER)
    store = bench.source_store(matter)
    citations = []
    for index, body in enumerate((text, *((extra,) if extra else ()))):
        document = store.store_stream(f'Synthetic receiving log {index + 1}.txt', 'text/plain', io.BytesIO(body))[0]
        bench._sync_source_catalog(matter, (document,))
        citations.append(bench._saved_answer_citation_payload(
            bench._citation(matter, bench._candidate(matter, document, document.parsed_units()[0], 1))))
    conversation = bench.workspace.get_conversation(matter.matter_id)
    message = bench.workspace.append_message(matter.matter_id, conversation.conversation_id,
        'assistant', 'Synthetic answer', {'kind': 'generated', 'claims': [
            {'text': 'Alex Example signed for the crate.', 'citations': citations}]})
    url = (f'/matters/{matter.slug}/conversations/{conversation.conversation_id}'
           f'/messages/{message.message_id}/suggestions/claims/0')
    return matter, conversation, message, url


@pytest.fixture
def answer(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    monkeypatch.delenv('CASE_INTELLIGENCE_AUTOMATIC_DISCOVERY', raising=False)
    with TestClient(create_workbench_app(tmp_path / 'runtime',
            generator=UnavailableGenerator(), auth_mode='test')) as client:
        client.get('/')  # establish the synthetic development principal
        yield client, seed(client.app.state.workbench)


def identities(bench, matter):
    service = bench.entity_service(matter)
    rows, _total = service.list(matter.matter_id, ACTOR)
    return {row['display_name']: row for row in rows}


def preview_and_add(client, url):
    preview = client.post(url + '/preview', headers=JSON).json()
    return preview, client.post(preview['save_url'], data={'basis': preview['basis']}, headers=JSON)


def test_a_preview_shows_what_would_be_added_and_writes_nothing(answer):
    client, (matter, conversation, message, url) = answer
    bench = client.app.state.workbench
    response = client.post(url + '/preview', headers=JSON)
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    preview = response.json()
    assert preview['items'] == [
        {'label': 'Alex Example', 'kind': 'person', 'count': 1, 'kind_label': 'Person'},
        {'label': '2026-03-05', 'kind': 'date', 'count': 1, 'kind_label': 'Date'},
        {'label': 'AB-1234', 'kind': 'identifier', 'count': 1, 'kind_label': 'Identifier'},
    ]
    assert preview['count'] == 3 and preview['passages'] == ['Synthetic receiving log 1.txt · Lines 1–3']
    assert preview['message'] == '3 new suggestions found. Nothing is added until you choose Add.'
    assert re.fullmatch(r'[0-9a-f]{64}', preview['basis']) and preview['save_url'] == url
    assert not identities(bench, matter)


def test_adding_the_preview_creates_suggestions_that_record_they_came_from_an_answer(answer):
    client, (matter, conversation, message, url) = answer
    bench = client.app.state.workbench
    preview, first = preview_and_add(client, url)
    assert first.status_code == 200 and first.headers['cache-control'] == 'no-store'
    body = first.json()
    assert body['added'] == 3 and body['dates'] == 1
    assert body['message'] == 'Added 3 suggestions for review, including 1 date.'
    assert body['inbox_url'] == f'/matters/{matter.slug}/entities#suggestions'
    assert body['timeline_url'] == f'/matters/{matter.slug}/chronology#found-dates-heading'
    rows = identities(bench, matter)
    assert set(rows) == {'Alex Example', '2026-03-05', 'AB-1234'}
    assert {row['status'] for row in rows.values()} == {'suggested'}
    # Created by the reviewer, with the answer recorded as the first history entry.
    with bench.workspace._lock:
        history = bench.workspace.connection.execute(
            'SELECT h.action, e.created_by FROM workbench_entity_history h JOIN workbench_entity e '
            'ON e.entity_id=h.entity_id WHERE e.matter_id=? AND h.revision=1', (matter.matter_id,)).fetchall()
    assert {(row[0], row[1]) for row in history} == {(ANSWER_HISTORY_ACTION, ACTOR)}
    inbox = client.get(f'/matters/{matter.slug}/entities').text
    assert inbox.count('suggestion-from-answer">From an answer</span>') == 3
    again = client.post(url + '/preview', headers=JSON).json()
    assert again['items'] == [] and again['message'] == (
        'No new people, things or dates: they are already suggested, or none were found.')
    # The old proposal no longer matches what is left to add, so it is refused.
    assert client.post(url, data={'basis': preview['basis']}, headers=JSON).status_code == 409
    repeat = client.post(url, data={'basis': again['basis']}, headers=JSON).json()
    assert repeat['added'] == 0 and repeat['timeline_url'] == ''
    assert len(identities(bench, matter)) == 3


def test_the_preview_counts_every_occurrence_that_adding_creates(answer):
    client, _seeded = answer
    bench = client.app.state.workbench
    # The same name in two cited passages is two occurrences, so two suggestions.
    matter, conversation, message, url = seed(bench, extra=b'witness: Alex Example\n')
    preview = client.post(url + '/preview', headers=JSON).json()
    alex = next(item for item in preview['items'] if item['label'] == 'Alex Example')
    assert alex['count'] == 2 and preview['count'] == sum(item['count'] for item in preview['items'])
    assert preview['passages'] == ['Synthetic receiving log 1.txt · Lines 1–3', 'Synthetic receiving log 2.txt · Line 1']
    added = client.post(url, data={'basis': preview['basis']}, headers=JSON).json()
    assert added['added'] == preview['count']
    page = client.post(url + '/preview', data={}, follow_redirects=False)
    assert page.status_code == 303  # nothing new is left to add


def test_adding_needs_the_preview_basis_of_the_current_passages(answer):
    client, (matter, conversation, message, url) = answer
    bench = client.app.state.workbench
    for form in ({}, {'basis': 'not-a-basis'}, {'basis': 'f' * 64}):
        response = client.post(url, data=form, headers=JSON)
        assert response.status_code == 409, form
    assert 'changed since the preview' in client.post(url, data={'basis': 'f' * 64}, headers=JSON).json()['message']
    assert not identities(bench, matter)


def test_answer_suggestions_and_discovery_never_duplicate_an_occurrence(answer):
    client, (matter, conversation, message, url) = answer
    bench = client.app.state.workbench
    assert preview_and_add(client, url)[1].json()['added'] == 3
    bench.run_automatic_discovery_once()
    assert len(identities(bench, matter)) == 3
    # The other way round: what discovery already found is not suggested again from an answer.
    other = seed(bench, b'witness: Morgan Ellis\nA second synthetic log dated 2026-04-01.\n')
    bench.run_automatic_discovery_once()
    preview, response = preview_and_add(client, other[3])
    assert preview['items'] == [] and response.json()['added'] == 0
    assert 'From an answer' not in client.get(f'/matters/{other[0].slug}/entities').text


def test_a_budget_refusal_on_a_later_passage_leaves_nothing_written(answer, monkeypatch):
    client, _seeded = answer
    bench = client.app.state.workbench
    matter, conversation, message, url = seed(bench, extra=b'witness: Riley Demo\nbadge: CD-5678\n')
    preview = client.post(url + '/preview', headers=JSON).json()
    assert {item['label'] for item in preview['items']} >= {'Alex Example', 'Riley Demo'}
    real = EntityDiscovery._process_unit
    calls = []

    def second_passage_over_budget(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise DiscoveryBudgetFull('Entity discovery byte budget reached. Saved work is retained; this unit remains unprocessed.')
        return real(self, *args, **kwargs)
    monkeypatch.setattr(EntityDiscovery, '_process_unit', second_passage_over_budget)
    response = client.post(url, data={'basis': preview['basis']}, headers=JSON)
    assert response.status_code == 409 and len(calls) == 2
    assert not identities(bench, matter)
    with bench.workspace._lock:
        receipts = bench.workspace.connection.execute(
            'SELECT COUNT(*) FROM workbench_entity_discovery_seen WHERE matter_id=?', (matter.matter_id,)).fetchone()[0]
    assert receipts == 0


def test_a_long_current_passage_is_used_whole(answer):
    client, _seeded = answer
    bench = client.app.state.workbench
    long_text = ('witness: Alex Example ' + 'The crate log continues with routine words. ' * 170
                 + 'badge: ZZ-9999').encode()
    matter, conversation, message, url = seed(bench, long_text)
    preview = client.post(url + '/preview', headers=JSON)
    assert preview.status_code == 200
    # Found beyond the 6,000-character display limit, so the whole unit was read.
    assert 'ZZ-9999' in {item['label'] for item in preview.json()['items']}


def test_a_changed_citation_lost_access_and_missing_claims_suggest_nothing(answer):
    client, (matter, conversation, message, url) = answer
    bench = client.app.state.workbench
    payload = copy.deepcopy(message.payload)
    payload['claims'][0]['citations'][0]['excerpt_digest'] = 'f' * 64
    stale = bench.workspace.append_message(matter.matter_id, conversation.conversation_id,
        'assistant', 'Synthetic stale answer', payload)
    stale_url = url.replace(message.message_id, stale.message_id)
    for target in (stale_url + '/preview', stale_url):
        response = client.post(target, data={'basis': 'a' * 64}, headers=JSON)
        assert response.status_code == 409 and 'changed or are unavailable' in response.json()['message']
    legacy = client.post(stale_url + '/preview', follow_redirects=False)
    assert legacy.status_code == 303 and 'error' in parse_qs(urlsplit(legacy.headers['location']).query)
    # A changed passage among current ones refuses the whole claim, as saving a passage does.
    two = seed(bench, extra=b'witness: Riley Demo\n')
    mixed_payload = copy.deepcopy(two[2].payload)
    mixed_payload['claims'][0]['citations'][1]['excerpt_digest'] = 'f' * 64
    mixed = bench.workspace.append_message(two[0].matter_id, two[1].conversation_id,
        'assistant', 'Synthetic mixed answer', mixed_payload)
    assert client.post(two[3].replace(two[2].message_id, mixed.message_id) + '/preview', headers=JSON).status_code == 409
    assert client.post(two[3] + '/preview', headers=JSON).status_code == 200
    assert client.post(url[:-1] + '9/preview', headers=JSON).status_code == 404
    other = bench.create_matter('Other synthetic matter', 'Generated isolation', ACTOR)
    assert client.post(url.replace(matter.slug, other.slug) + '/preview', headers=JSON).status_code == 404
    assert not identities(bench, matter)
    bench.workspace.revoke_member(matter.matter_id, ACTOR, OWNER)
    assert client.post(url + '/preview', headers=JSON).status_code in (403, 404)


def test_without_javascript_the_preview_is_a_page_with_add_and_dismiss(answer):
    client, (matter, conversation, message, url) = answer
    bench = client.app.state.workbench
    reader = f'/matters/{matter.slug}/sources?query=crate#source-section-1'
    page = client.post(url + '/preview', data={'return_to': reader})
    assert page.status_code == 200 and page.headers['cache-control'] == 'no-store'
    assert '<h1 id="suggestion-preview-heading">Suggestions from this passage</h1>' in page.text
    assert '<strong>Alex Example</strong> <span class="suggestion-type">Person</span>' in page.text
    assert '<li>Synthetic receiving log 1.txt · Lines 1–3</li>' in page.text
    assert '>Add 3 to suggestions</button>' in page.text
    assert f'href="{html.escape(reader)}">Dismiss</a>' in page.text
    assert not identities(bench, matter)
    basis = re.search(r'name="basis" value="([0-9a-f]{64})"', page.text)[1]
    response = client.post(url, data={'basis': basis, 'return_to': reader}, follow_redirects=False)
    assert response.status_code == 303
    parsed = urlsplit(response.headers['location'])
    assert parsed.path == f'/matters/{matter.slug}/sources' and parsed.fragment == 'source-section-1'
    assert parse_qs(parsed.query)['notice'] == ['Added 3 suggestions for review, including 1 date.']
    # With nothing new to add, the reviewer returns to the same place with a notice.
    nothing = client.post(url + '/preview', data={'return_to': reader}, follow_redirects=False)
    assert nothing.status_code == 303
    assert parse_qs(urlsplit(nothing.headers['location']).query)['notice'][0].startswith('No new people')
    outside = client.post(url, data={'basis': basis, 'return_to': 'https://example.invalid/'}, follow_redirects=False)
    assert urlsplit(outside.headers['location']).path == f'/matters/{matter.slug}'


def test_suggesting_requires_the_session_csrf_token(tmp_path, monkeypatch):
    from tests.test_identity_membership_audit import _login
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    with TestClient(create_workbench_app(tmp_path / 'runtime',
            generator=UnavailableGenerator(), auth_mode='preview')) as client:
        _, csrf = _login(client, 'taylor-morgan')
        bench = client.app.state.workbench
        matter, conversation, message, url = seed(bench)
        assert client.post(url + '/preview', headers=JSON).status_code == 403
        preview = client.post(url + '/preview', data={'csrf_token': csrf}, headers=JSON).json()
        assert client.post(url, data={'basis': preview['basis']}, headers=JSON).status_code == 403
        assert client.post(url, data={'basis': preview['basis'], 'csrf_token': 'invalid'}, headers=JSON).status_code == 403
        assert not identities(bench, matter)
        assert client.post(url, data={'basis': preview['basis'], 'csrf_token': csrf}, headers=JSON).json()['added'] == 3


def test_the_action_appears_on_each_claim_in_the_dock_and_the_full_conversation(answer):
    client, (matter, conversation, message, url) = answer
    dock = client.get(f'/matters/{matter.slug}/assistant', params={'conversation': conversation.conversation_id}).text
    form = re.search(r'<form class="save-claim-form" method="post" action="' + re.escape(url + '/preview') + r'" data-assistant-suggest-passage>(.*?)</form>\s*<div class="suggestion-preview-card" data-suggest-preview-card hidden></div>', dock, re.S)
    assert form and 'Suggest people, things and dates from this passage' in form[1]
    assert 'data-suggest-passage-status role="status"' in form[1]
    assert re.search(r'data-suggest-inbox hidden>Review suggestions</a>', form[1])
    assert re.search(r'data-suggest-timeline hidden>See dates in the timeline draft</a>', form[1])
    full = client.get(f'/matters/{matter.slug}', params={'conversation': conversation.conversation_id}).text
    assert f'action="{url}/preview"' in full
    anchor = f'/matters/{matter.slug}?conversation={conversation.conversation_id}#answer-support-{message.message_id}'
    assert f'name="return_to" value="{html.escape(anchor)}"' in full
    assert f'id="answer-support-{message.message_id}"' in full
    back = client.post(url + '/preview', data={'return_to': anchor}, follow_redirects=False)
    assert back.status_code == 200 and f'href="{html.escape(anchor)}">Dismiss</a>' in back.text


def test_a_proposal_is_refused_when_discovery_took_its_occurrences_after_the_preview(answer):
    client, (matter, conversation, message, url) = answer
    bench = client.app.state.workbench
    preview = client.post(url + '/preview', headers=JSON).json()
    # Automatic discovery suggests the same occurrences between preview and Add.
    bench.run_automatic_discovery_once()
    before = len(identities(bench, matter))
    response = client.post(url, data={'basis': preview['basis']}, headers=JSON)
    assert response.status_code == 409 and 'changed since the preview' in response.json()['message']
    assert len(identities(bench, matter)) == before
    assert client.post(url + '/preview', headers=JSON).json()['items'] == []


def test_a_group_mixing_answer_and_automatic_suggestions_says_so(answer):
    client, (matter, conversation, message, url) = answer
    bench = client.app.state.workbench
    assert preview_and_add(client, url)[1].json()['added'] == 3
    store = bench.source_store(matter)
    document = store.store_stream('Synthetic second log.txt', 'text/plain', io.BytesIO(b'witness: Alex Example\n'))[0]
    bench._sync_source_catalog(matter, (document,))
    bench.run_automatic_discovery_once()
    inbox = client.get(f'/matters/{matter.slug}/entities').text
    assert 'suggestion-from-answer">1 of 2 from an answer</span>' in inbox
    assert inbox.count('suggestion-from-answer">From an answer</span>') == 2


def test_the_audit_event_commits_with_the_suggestions_or_neither_is_saved(answer, monkeypatch):
    client, (matter, conversation, message, url) = answer
    bench = client.app.state.workbench
    preview = client.post(url + '/preview', headers=JSON).json()
    real = bench.workspace._append_audit_event_locked

    def failing(**kwargs):
        if kwargs['action'] == 'entity.suggest_from_answer':
            raise RuntimeError('synthetic audit storage failure')
        return real(**kwargs)
    monkeypatch.setattr(bench.workspace, '_append_audit_event_locked', failing)
    with pytest.raises(RuntimeError):
        client.post(url, data={'basis': preview['basis']}, headers=JSON)
    assert not identities(bench, matter)
    with bench.workspace._lock:
        receipts = bench.workspace.connection.execute(
            'SELECT COUNT(*) FROM workbench_entity_discovery_seen WHERE matter_id=?', (matter.matter_id,)).fetchone()[0]
    assert receipts == 0
    monkeypatch.setattr(bench.workspace, '_append_audit_event_locked', real)
    assert client.post(url, data={'basis': preview['basis']}, headers=JSON).json()['added'] == 3
    events = [event for event in bench.workspace.audit_events(matter.matter_id) if event.action == 'entity.suggest_from_answer']
    assert len(events) == 1 and events[0].object_id == message.message_id


def test_a_proposal_is_refused_when_a_revised_extractor_would_create_something_else(answer, monkeypatch):
    import dataclasses
    from case_intelligence.entity_extractor import DeterministicEntityExtractor
    client, (matter, conversation, message, url) = answer
    bench = client.app.state.workbench
    preview = client.post(url + '/preview', headers=JSON).json()
    assert {'label': 'Alex Example', 'kind': 'person', 'count': 1, 'kind_label': 'Person'} in preview['items']
    real = DeterministicEntityExtractor.extract

    def revised(self, text):
        # Same source offsets, different classification, as a revised extractor might produce.
        for occurrence in real(self, text):
            yield dataclasses.replace(occurrence, kind='organization') if occurrence.kind == 'person' else occurrence
    monkeypatch.setattr(DeterministicEntityExtractor, 'extract', revised)
    response = client.post(url, data={'basis': preview['basis']}, headers=JSON)
    assert response.status_code == 409 and not identities(bench, matter)
    fresh = client.post(url + '/preview', headers=JSON).json()
    assert {'label': 'Alex Example', 'kind': 'organization', 'count': 1, 'kind_label': 'Organization'} in fresh['items']
