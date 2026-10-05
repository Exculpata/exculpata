"""Synthetic regression: draft witness questions or discovery requests from an answer's passages."""
import copy
import dataclasses
import html
import io
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import EvidenceItem, GenerationRejected, UnavailableGenerator
from case_intelligence.question_drafting import (
    QUESTION_OMISSION_NOTICE, question_prompt, unsupported_quotes, verify_question, verify_question_draft,
)
from case_intelligence.workbench import create_workbench_app

ACTOR = 'development-taylor-morgan'
OWNER = 'synthetic-drafts-owner'
TEXT = (b'Receiving log, North Annex. The blue crate arrived on 2026-03-05 at 09:15 and was '
        b'signed for by Alex Example. The camera over bay 4 was "out of service" that morning.\n')
TOPIC = 'What happened when the blue crate arrived?'
GOOD = [
    {'text': 'Who else was present at the North Annex when the blue crate arrived on 2026-03-05?', 'evidence_ids': ['S1']},
    {'text': 'How long had the camera over bay 4 been "out of service"?', 'evidence_ids': ['S1']},
]
BAD = [
    {'text': 'Why did the driver say "it was already open"?', 'evidence_ids': ['S1']},  # quote not in the passage
    {'text': 'Who signed for the crate at 11:40?', 'evidence_ids': ['S1']},  # number not in the passage
    {'text': 'Who else saw the crate arrive?', 'evidence_ids': ['S9']},  # unknown passage
]


class ScriptedDrafts:
    """A deterministic drafting client; it never answers questions."""
    def __init__(self, questions):
        self.questions = questions
        self.calls = []

    @property
    def available(self):
        return True

    def generate(self, **_):
        raise AssertionError('answers are not generated in these tests')

    def classify_source(self, **_):
        raise AssertionError('not used')

    def draft_questions(self, *, purpose, topic, evidence):
        self.calls.append((purpose, topic, tuple(evidence)))
        return {'questions': copy.deepcopy(self.questions)}


def seed(bench, text=TEXT, *, stale=False):
    bench.workspace.upsert_principal('test', OWNER, 'Synthetic Owner', OWNER, preferred_principal_id=OWNER)
    matter = bench.create_matter('Synthetic question drafts', 'Generated evidence', OWNER)
    bench.workspace.add_member(matter.matter_id, ACTOR, OWNER)
    store = bench.source_store(matter)
    document = store.store_stream('Synthetic receiving log.txt', 'text/plain', io.BytesIO(text))[0]
    bench._sync_source_catalog(matter, (document,))
    citation = bench._saved_answer_citation_payload(
        bench._citation(matter, bench._candidate(matter, document, document.parsed_units()[0], 1)))
    if stale:
        citation['excerpt_digest'] = 'f' * 64
    conversation = bench.workspace.get_conversation(matter.matter_id)
    bench.workspace.append_message(matter.matter_id, conversation.conversation_id, 'user', TOPIC)
    message = bench.workspace.append_message(matter.matter_id, conversation.conversation_id,
        'assistant', 'Synthetic answer', {'kind': 'generated', 'claims': [
            {'text': 'Alex Example signed for the blue crate.', 'citations': [citation]},
            {'text': 'The bay 4 camera was out of service.', 'citations': [citation]}]})
    base = f'/matters/{matter.slug}/conversations/{conversation.conversation_id}/messages/{message.message_id}/questions'
    return matter, conversation, message, base


def seed_two(bench, texts, *, name='Synthetic receiving log.txt', limitation=False):
    """An answer citing one passage in each of two sources (the second in a limitation, if asked)."""
    bench.workspace.upsert_principal('test', OWNER, 'Synthetic Owner', OWNER, preferred_principal_id=OWNER)
    matter = bench.create_matter('Synthetic question drafts', 'Generated evidence', OWNER)
    bench.workspace.add_member(matter.matter_id, ACTOR, OWNER)
    store = bench.source_store(matter)
    citations = []
    for number, text in enumerate(texts, 1):
        # Separate folders, so both sources can share a display name.
        document = store.store_stream(name, 'text/plain', io.BytesIO(text),
                                      relative_path=f'box-{number}/{name}')[0]
        bench._sync_source_catalog(matter, (document,))
        citations.append(bench._saved_answer_citation_payload(
            bench._citation(matter, bench._candidate(matter, document, document.parsed_units()[0], 1))))
    conversation = bench.workspace.get_conversation(matter.matter_id)
    bench.workspace.append_message(matter.matter_id, conversation.conversation_id, 'user', TOPIC)
    claims = [{'text': 'Synthetic claim.', 'citations': [citations[0]]}]
    payload = {'kind': 'generated', 'claims': claims}
    if limitation:
        payload['source_limitation'] = {'text': 'Synthetic limitation.', 'citations': [citations[1]]}
    else:
        claims.append({'text': 'Synthetic second claim.', 'citations': [citations[1]]})
    message = bench.workspace.append_message(matter.matter_id, conversation.conversation_id,
                                             'assistant', 'Synthetic answer', payload)
    base = f'/matters/{matter.slug}/conversations/{conversation.conversation_id}/messages/{message.message_id}/questions'
    return matter, base


def app_client(tmp_path, monkeypatch, generator, auth_mode='test'):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    return TestClient(create_workbench_app(tmp_path / 'runtime', generator=generator, auth_mode=auth_mode))


@pytest.fixture
def drafting(tmp_path, monkeypatch):
    generator = ScriptedDrafts(GOOD + BAD)
    with app_client(tmp_path, monkeypatch, generator) as client:
        client.get('/')
        yield client, generator, seed(client.app.state.workbench)


def notes(bench, matter):
    return bench.workspace.all_notebook_items(matter.matter_id, OWNER)


def save_form(draft):
    return {'purpose': draft['purpose'], 'basis': draft['basis'], 'question': [q['text'] for q in draft['questions']],
            'passages': [','.join(map(str, q['passages'])) for q in draft['questions']]}


EVIDENCE = {'S1': EvidenceItem('S1', 'Synthetic log', 'Line 1', TEXT.decode()),
            'S2': EvidenceItem('S2', 'Other log', 'Line 1', 'A museum volunteer counted 12 postcards.')}


def test_a_question_must_cite_its_passages_and_keep_their_quotes_and_numbers():
    assert verify_question(GOOD[0]['text'], ['S1'], EVIDENCE).evidence_ids == ('S1',)
    assert verify_question(GOOD[1]['text'], ['S1'], EVIDENCE)  # its quotation is in the passage
    for text, ids in [(BAD[0]['text'], ['S1']), (BAD[1]['text'], ['S1']), (BAD[2]['text'], ['S9']),
                      (GOOD[0]['text'], []), (GOOD[0]['text'], ['S1', 'S1']), (GOOD[0]['text'], ['S1'] * 5),
                      ('Who counted the postcards?', ['S1']),  # shares no wording with its passage
                      ('What does [S1] say about the crate?', ['S1']), ('Why?', ['S1'])]:
        assert verify_question(text, ids, EVIDENCE) is None, text
    # Quotations of any length must be in the cited passage, in either quote style.
    witness = {'S1': EvidenceItem('S1', 'Synthetic note', 'Line 1', 'The witness saw a blue truck near the gate.')}
    for text in ('Did the witness say "red"?', "Did the witness say 'red'?", 'Did the witness say \u201cred\u201d?',
                 'Did the witness say "' + 'z' * 241 + '"?', "Did the witness say ('red')?",
                 "Did the witness say 'it's red'?", 'Did the witness say \u2018it\u2019s red\u2019?',
                 "Did the witness say ['red']?", "Did the witness say 'red', then leave?",
                 "Did the witness say:'purple' near the truck?", "Did the witness say,'purple' near the truck?",
                 "Did the witness say;'purple' near the truck?", "Did the witness say.'purple' near the truck?",
                 "Did the witness say?'purple' near the truck?", "Did the witness say!'purple' near the truck?",
                 'Did the witness say "purple near the truck?', 'Did the witness say \u201cblue truck near the gate?',
                 "Did the witness say 'blue truck near the gate?", 'Did the witness see the blue truck" near the gate?'):
        assert verify_question(text, ['S1'], witness) is None, text
    assert verify_question('Did the witness say "blue"?', ['S1'], witness)
    assert verify_question("Did the witness say 'blue truck'?", ['S1'], witness)
    assert unsupported_quotes('Was it "red" or "blue"?', [witness['S1']]) == ('red',)
    assert unsupported_quotes("Did the witness say 'it's red'?", [witness['S1']]) == ("it's red",)
    # Text that is not valid UTF-8 (a lone surrogate from a runtime's JSON) is never kept.
    assert verify_question('Who saw the blue truck near the gate \ud800?', ['S1'], witness) is None
    # An unclosed quotation cannot be checked, so it is unsupported even if its words are present.
    assert unsupported_quotes('Did the witness say "blue truck?', [witness['S1']]) == ('blue truck?',)
    assert unsupported_quotes('Did the witness say "\'blue truck\'"?', [witness['S1']]) == ()
    # A quotation must be in one passage, not assembled across two.
    split = [EvidenceItem('S1', 'Synthetic note', 'Line 1', 'The witness said the car was red'),
             EvidenceItem('S2', 'Synthetic note', 'Line 2', 'truck tires were left near the gate.')]
    assert unsupported_quotes('Who saw the "red truck" near the gate?', split) == ('red truck',)
    assert verify_question('Who saw the "red truck" near the gate?', ['S1', 'S2'],
                           {item.evidence_id: item for item in split}) is None
    assert unsupported_quotes('Was the car "red" near the "gate"?', split) == ()
    # Angle, low-9, CJK corner and fullwidth quotation marks are checked too, and an
    # unpaired one is an unclosed quotation.
    for text in ('Did the witness say \u00abpurple\u00bb near the truck?', 'Did the witness say \u2039purple\u203a near the truck?',
                 'Did the witness say \u201epurple\u201c near the truck?', 'Did the witness say \u300cpurple\u300d near the truck?',
                 'Did the witness say \uff02purple\uff02 near the truck?', 'Did the witness say \u00bbpurple\u00ab near the truck?',
                 'Did the witness say \u00abblue truck near the gate?', 'Did the witness say blue\u300d near the truck?'):
        assert verify_question(text, ['S1'], witness) is None, text
    for text in ('Did the witness say \u00abblue truck\u00bb?', 'Did the witness say \u300cblue truck\u300d?'):
        assert verify_question(text, ['S1'], witness), text
    assert unsupported_quotes('Was it \u00abred\u00bb or \u00abblue\u00bb?', [witness['S1']]) == ('red',)
    # A right-curly mark can open a quotation too, as in an apostrophe-style pair.
    for text in ('Did the witness say \u2019purple\u2019 near the truck?', 'Did the witness say (\u2019purple\u2019) near the truck?'):
        assert verify_question(text, ['S1'], witness) is None, text
    assert verify_question('Did the witness say \u2019blue truck\u2019?', ['S1'], witness)
    # Quotations containing a contraction are checked whole, in either apostrophe style;
    # unquoted possessives and contractions are not quotations.
    said = {'S1': EvidenceItem('S1', 'Synthetic note', 'Line 1', "The driver said it's blue and parked at the gate.")}
    for text in ("Did the driver say ('it's blue')?", 'Did the driver say \u2018it\u2019s blue\u2019?',
                 "Why did the driver's note say it's blue?", "Did the drivers' log mention the gate?"):
        assert verify_question(text, ['S1'], said), text
        assert unsupported_quotes(text, [said['S1']]) == (), text
    # A quotation must match whole words: "red" is not in "covered".
    covered = EvidenceItem('S1', 'Synthetic note', 'Line 1', 'The truck was covered near the gate.')
    assert unsupported_quotes('Was the truck "red"?', [covered]) == ('red',)
    assert unsupported_quotes('Was the truck "covered"?', [covered]) == ()
    # Identifiers, numbers and ordinals with digits must match whole tokens in the passage.
    log = {'S1': EvidenceItem('S1', 'Synthetic sheet', 'Row 18', 'Count recorded 48 cartons of model K7 filters at bay 4.')}
    assert verify_question('Who counted the model K7 filters at bay 4?', ['S1'], log)
    for text in ('Who counted the model K9 filters at bay 4?', 'Who counted the cartons at bay 5A?',
                 'Who counted the 4th row of model K7 filters?', 'What does S1 say about the K7 filters?',
                 'Who counted the K7 filters (S1)?', 'Who counted the K7 filters per E2?'):
        assert verify_question(text, ['S1'], log) is None, text
    # A prefix before a separator is part of the identifier: K-7 is not K-8, even
    # when a passage has an unrelated 7 elsewhere.
    hyphen = {'S1': EvidenceItem('S1', 'Synthetic sheet', 'Row 7', 'Row 7 lists model K-8 filters at the dock.')}
    assert verify_question('Who stocked the model K-8 filters at the dock?', ['S1'], hyphen)
    assert verify_question('Who stocked the model K-7 filters at the dock?', ['S1'], hyphen) is None
    # So is a prefix joined by any other separator.
    underscore = {'S1': EvidenceItem('S1', 'Synthetic sheet', 'Row 7', 'Row 7 lists model K_8 filters at the dock.')}
    assert verify_question("Who stocked the model K_8 filters at the dock?", ['S1'], underscore)
    assert verify_question("Who stocked the model K_8's filters at the dock?", ['S1'], underscore)
    for text in ('Who stocked the model K_7 filters at the dock?', 'Who stocked the model K+7 filters at the dock?'):
        assert verify_question(text, ['S1'], underscore) is None, text
    # An identifier that really is in the passage, even one shaped like an evidence ID, is allowed.
    unit = {'S1': EvidenceItem('S1', 'Synthetic roster', 'Row 2', 'Unit S2 was assigned the dash camera.')}
    assert verify_question('Who in unit S2 used the dash camera?', ['S1'], unit)
    # A leading list marker is not part of the question.
    assert verify_question('3. ' + GOOD[0]['text'], ['S1'], EVIDENCE).text == GOOD[0]['text']
    assert verify_question('Q12: ' + GOOD[0]['text'], ['S1'], EVIDENCE).text == GOOD[0]['text']
    # Possessives and contractions are not quotations.
    assert verify_question("Who checked the driver's log and the clerk's copy for the blue crate?", ['S1'], EVIDENCE)
    assert unsupported_quotes("Why was it 'out of service'?", [EVIDENCE['S1']]) == ()
    assert unsupported_quotes("Why was it 'broken' then?", [EVIDENCE['S1']]) == ('broken',)


def test_a_draft_keeps_verified_questions_drops_the_rest_and_rejects_a_malformed_reply():
    questions, omitted = verify_question_draft({'questions': GOOD + BAD + [GOOD[0], 'loose text']}, tuple(EVIDENCE.values()))
    assert [q.text for q in questions] == [GOOD[0]['text'], GOOD[1]['text']] and omitted == 4
    # A question repeated with different punctuation, case or spacing is one question.
    stem = GOOD[0]['text'][:-1]
    variants = [{'text': text, 'evidence_ids': ['S1']} for text in (stem + '?', stem + '??', stem + '?!', '  ' + stem.upper() + ' ?')]
    questions, omitted = verify_question_draft({'questions': variants}, tuple(EVIDENCE.values()))
    assert [q.text for q in questions] == [GOOD[0]['text']] and omitted == 0
    for raw in ({'questions': 'none'}, {'questions': [], 'extra': 1}, {'answer': []}, {'questions': GOOD * 5}):
        with pytest.raises(GenerationRejected):
            verify_question_draft(raw, tuple(EVIDENCE.values()))


def test_drafting_proposes_checked_questions_and_saves_nothing(drafting):
    client, generator, (matter, conversation, message, base) = drafting
    bench = client.app.state.workbench
    response = client.post(base, data={'purpose': 'witness'}, headers={'Accept': 'application/json'})
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    draft = response.json()
    assert draft['label'] == 'Questions for a witness' and draft['notice'] == QUESTION_OMISSION_NOTICE
    assert [q['text'] for q in draft['questions']] == [GOOD[0]['text'], GOOD[1]['text']]
    assert draft['questions'][0]['passages'] == [1]
    assert draft['questions'][0]['sources'] == ['Synthetic receiving log.txt · Line 1']
    assert draft['save_url'] == base + '/save'
    # The two claims cite one passage; it is sent once, with the reviewer's question as the topic.
    purpose, topic, evidence = generator.calls[0]
    assert purpose == 'witness' and topic == TOPIC and [item.evidence_id for item in evidence] == ['S1']
    assert not notes(bench, matter)
    assert client.post(base, data={'purpose': 'discovery'}, headers={'Accept': 'application/json'}).json()['label'] == 'Discovery requests'
    assert client.post(base, data={'purpose': 'essay'}, headers={'Accept': 'application/json'}).status_code == 409


def test_saving_a_draft_adds_one_suggested_note_with_its_sources(drafting):
    client, generator, (matter, conversation, message, base) = drafting
    bench = client.app.state.workbench
    draft = client.post(base, data={'purpose': 'witness'}, headers={'Accept': 'application/json'}).json()
    saved = client.post(draft['save_url'], data=save_form(draft), headers={'Accept': 'application/json'})
    assert saved.status_code == 200 and saved.json()['created'] is True
    assert saved.json()['notebook_url'] == f'/matters/{matter.slug}/notebook'
    [item] = notes(bench, matter)
    assert item.status == 'suggested' and item.item_type == 'note' and item.origin == 'answer'
    assert item.title == f'Draft witness questions: {TOPIC}'
    assert item.body.splitlines()[0] == f"1. {GOOD[0]['text']}"
    assert 'Sources: Synthetic receiving log.txt · Line 1' in item.body
    assert item.reference_count == 1 and item.source_message_id == message.message_id
    again = client.post(draft['save_url'], data=save_form(draft), headers={'Accept': 'application/json'}).json()
    assert again['created'] is False and len(notes(bench, matter)) == 1


def test_a_changed_or_altered_draft_is_refused_and_nothing_is_saved(drafting):
    client, generator, (matter, conversation, message, base) = drafting
    bench = client.app.state.workbench
    draft = client.post(base, data={'purpose': 'witness'}, headers={'Accept': 'application/json'}).json()
    for change in ({'question': ['Why did the driver say "it was already open"?']}, {'passages': ['2']},
                   {'passages': ['1,1,1,1,1']}, {'question': [], 'passages': []}, {'purpose': 'essay'}):
        form = {**save_form(draft), **change}
        if 'question' in change and change['question']:
            form['passages'] = ['1']
        response = client.post(draft['save_url'], data=form, headers={'Accept': 'application/json'})
        assert response.status_code == 409, change
    assert not notes(bench, matter)
    # An answer whose cited passage changed drafts nothing.
    stale = seed(bench, stale=True)
    response = client.post(stale[3], data={'purpose': 'witness'}, headers={'Accept': 'application/json'})
    assert response.status_code == 409 and 'changed or are unavailable' in response.json()['message']


def test_without_javascript_the_draft_is_a_page_with_save_and_dismiss(drafting):
    client, generator, (matter, conversation, message, base) = drafting
    reader = f'/matters/{matter.slug}/sources?query=crate'
    page = client.post(base, data={'purpose': 'discovery', 'return_to': reader})
    assert page.status_code == 200 and page.headers['cache-control'] == 'no-store'
    text = page.text
    assert '<h1 id="question-draft-heading">Discovery requests</h1>' in text
    assert 'nothing is saved yet' in text and QUESTION_OMISSION_NOTICE in text
    assert f'href="{html.escape(reader)}">Dismiss</a>' in text
    form = {'purpose': 'discovery', 'return_to': reader,
            'basis': re.search(r'name="basis" value="([0-9a-f]{64})"', text).group(1),
            'question': [html.unescape(v) for v in re.findall(r'name="question" value="([^"]+)"', text)],
            'passages': re.findall(r'name="passages" value="([^"]+)"', text)}
    assert len(form['question']) == 2
    saved = client.post(base + '/save', data=form, follow_redirects=False)
    assert saved.status_code == 303
    location = urlsplit(saved.headers['location'])
    assert location.path == f'/matters/{matter.slug}/sources'
    assert parse_qs(location.query)['notice'] == ['Questions saved to case notes for review.']


def test_failures_say_what_happened_and_access_is_checked(tmp_path, monkeypatch):
    with app_client(tmp_path / 'a', monkeypatch, UnavailableGenerator()) as client:
        client.get('/')
        matter, conversation, message, base = seed(client.app.state.workbench)
        response = client.post(base, data={'purpose': 'witness'}, headers={'Accept': 'application/json'})
        assert response.status_code == 503 and 'temporarily unavailable' in response.json()['message']
    with app_client(tmp_path / 'b', monkeypatch, ScriptedDrafts(BAD)) as client:
        client.get('/')
        bench = client.app.state.workbench
        matter, conversation, message, base = seed(bench)
        response = client.post(base, data={'purpose': 'witness'}, headers={'Accept': 'application/json'})
        assert response.status_code == 409 and 'No drafted question passed' in response.json()['message']
        legacy = client.post(base, data={'purpose': 'witness'}, follow_redirects=False)
        assert legacy.status_code == 303 and 'error' in parse_qs(urlsplit(legacy.headers['location']).query)
        assert client.post(base.replace(message.message_id, 'missing-message'), data={'purpose': 'witness'},
                           headers={'Accept': 'application/json'}).status_code == 404
        other = bench.create_matter('Other synthetic matter', 'Generated isolation', ACTOR)
        assert client.post(base.replace(matter.slug, other.slug), data={'purpose': 'witness'},
                           headers={'Accept': 'application/json'}).status_code == 404
        bench.workspace.revoke_member(matter.matter_id, ACTOR, OWNER)
        assert client.post(base, data={'purpose': 'witness'}, headers={'Accept': 'application/json'}).status_code in (403, 404)



class RevokingDrafts(ScriptedDrafts):
    """Access is revoked while the model is drafting."""
    def draft_questions(self, *, purpose, topic, evidence):
        bench, matter = self.revoke
        bench.workspace.revoke_member(matter.matter_id, ACTOR, OWNER)
        return super().draft_questions(purpose=purpose, topic=topic, evidence=evidence)


class ChangingDrafts(ScriptedDrafts):
    """A cited source is removed while the model is drafting."""
    def draft_questions(self, *, purpose, topic, evidence):
        bench, matter = self.change
        store = bench.source_store(matter)
        for document_id in list(store.documents):
            store.remove(document_id)
        return super().draft_questions(purpose=purpose, topic=topic, evidence=evidence)


def test_a_passage_changed_during_drafting_returns_no_questions(tmp_path, monkeypatch):
    generator = ChangingDrafts(GOOD)
    with app_client(tmp_path, monkeypatch, generator) as client:
        client.get('/')
        bench = client.app.state.workbench
        matter, conversation, message, base = seed(bench)
        generator.change = (bench, matter)
        response = client.post(base, data={'purpose': 'witness'}, headers={'Accept': 'application/json'})
        assert generator.calls and response.status_code == 409
        assert 'nothing was drafted' in response.json()['message'] and 'questions' not in response.json()
        assert not notes(bench, matter)


def test_access_revoked_during_drafting_returns_no_questions(tmp_path, monkeypatch):
    generator = RevokingDrafts(GOOD)
    with app_client(tmp_path, monkeypatch, generator) as client:
        client.get('/')
        bench = client.app.state.workbench
        matter, conversation, message, base = seed(bench)
        generator.revoke = (bench, matter)
        response = client.post(base, data={'purpose': 'witness'}, headers={'Accept': 'application/json'})
        assert generator.calls and response.status_code in (403, 404)
        assert 'Who else was present' not in response.text and not notes(bench, matter)
def test_drafting_and_saving_require_the_session_csrf_token(tmp_path, monkeypatch):
    from tests.test_identity_membership_audit import _login
    with app_client(tmp_path, monkeypatch, ScriptedDrafts(GOOD), auth_mode='preview') as client:
        _, csrf = _login(client, 'taylor-morgan')
        matter, conversation, message, base = seed(client.app.state.workbench)
        assert client.post(base, data={'purpose': 'witness'}, headers={'Accept': 'application/json'}).status_code == 403
        draft = client.post(base, data={'purpose': 'witness', 'csrf_token': csrf}, headers={'Accept': 'application/json'}).json()
        assert client.post(draft['save_url'], data=save_form(draft), headers={'Accept': 'application/json'}).status_code == 403
        assert not notes(client.app.state.workbench, matter)


def test_the_actions_appear_on_answers_in_the_dock_and_the_full_conversation(drafting):
    client, generator, (matter, conversation, message, base) = drafting
    dock = client.get(f'/matters/{matter.slug}/assistant', params={'conversation': conversation.conversation_id}).text
    form = re.search(r'<form class="question-draft-form" method="post" action="' + re.escape(base) + r'" data-assistant-draft-questions role="group" aria-labelledby="dock-question-draft-[^"]+">(.*?)</form>', dock, re.S)
    assert form
    assert '<button type="submit" name="purpose" value="witness">Questions for a witness</button>' in form[1]
    assert '<button type="submit" name="purpose" value="discovery">Discovery requests</button>' in form[1]
    assert 'data-question-draft-status role="status"' in form[1]
    full = client.get(f'/matters/{matter.slug}', params={'conversation': conversation.conversation_id}).text
    assert full.count(f'action="{base}"') == 2
    assert '<input type="hidden" name="purpose" value="witness">' in full
    identifiers = re.findall(r'\bid="([^"]+)"', full)
    assert len(identifiers) == len(set(identifiers))


def test_transcript_passages_are_labelled_with_a_caution():
    transcript = EvidenceItem('S1', 'Synthetic interview.wav', '00:01:05', 'Speaker 1: the gate was open.', 'transcript')
    document = EvidenceItem('S2', 'Synthetic log', 'Line 1', 'The gate was logged as open.')
    system, user = question_prompt('witness', TOPIC, [transcript, document])
    assert '[S1] Synthetic interview.wav · 00:01:05 (machine transcript)' in user
    assert '[S2] Synthetic log · Line 1\n' in user and 'fallible representation' in system and 'Speaker 1' in system
    plain_system, plain_user = question_prompt('witness', TOPIC, [document])
    assert 'machine transcript' not in plain_system + plain_user


def test_a_source_backed_limitation_is_drafted_from_too(tmp_path, monkeypatch):
    generator = ScriptedDrafts(GOOD)
    with app_client(tmp_path, monkeypatch, generator) as client:
        client.get('/')
        matter, base = seed_two(client.app.state.workbench, [TEXT, b'The North Annex gate log for the blue crate is incomplete.\n'],
                                name='Synthetic note.txt', limitation=True)
        assert client.post(base, data={'purpose': 'witness'}, headers={'Accept': 'application/json'}).status_code == 200
        purpose, topic, evidence = generator.calls[0]
        assert [item.evidence_id for item in evidence] == ['S1', 'S2'] and 'incomplete' in evidence[1].excerpt


def test_the_same_question_saved_against_two_like_named_passages_keeps_both(tmp_path, monkeypatch):
    question = 'Who else saw the blue crate arrive at the North Annex?'
    with app_client(tmp_path, monkeypatch, ScriptedDrafts([{'text': question, 'evidence_ids': ['S1']}])) as client:
        client.get('/')
        bench = client.app.state.workbench
        matter, base = seed_two(bench, [b'The blue crate arrived at the North Annex.\n',
                                        b'The blue crate arrived at the North Annex dock.\n'])
        # Two distinct passages that display identically (for example two versions of a source).
        passages = bench._answer_passages
        monkeypatch.setattr(bench, '_answer_passages', lambda *args: (lambda topic, items: (topic, [
            (dataclasses.replace(item, source_name='Synthetic receiving log.txt'), reference)
            for item, reference in items]))(*passages(*args)))
        basis = client.post(base, data={'purpose': 'witness'}, headers={'Accept': 'application/json'}).json()['basis']
        first = client.post(base + '/save', data={'purpose': 'witness', 'basis': basis, 'question': [question],
                                                  'passages': ['1']}, headers={'Accept': 'application/json'}).json()
        second = client.post(base + '/save', data={'purpose': 'witness', 'basis': basis, 'question': [question],
                                                   'passages': ['2']}, headers={'Accept': 'application/json'}).json()
        assert first['created'] is True and second['created'] is True
        saved = notes(bench, matter)
        assert len(saved) == 2 and saved[0].body == saved[1].body


def test_a_save_is_bound_to_the_passages_the_draft_was_made_from(drafting, monkeypatch):
    """A corrected passage that keeps its identity still refuses a draft made from the old text."""
    client, generator, (matter, conversation, message, base) = drafting
    bench = client.app.state.workbench
    draft = client.post(base, data={'purpose': 'witness'}, headers={'Accept': 'application/json'}).json()
    assert len(draft['basis']) == 64
    for basis in ('', '0' * 64):
        refused = client.post(draft['save_url'], data={**save_form(draft), 'basis': basis},
                              headers={'Accept': 'application/json'})
        assert refused.status_code == 409 and 'changed since this draft' in refused.json()['message']
    passages = bench._answer_passages
    monkeypatch.setattr(bench, '_answer_passages', lambda *args: (lambda topic, items: (topic, [
        (dataclasses.replace(item, excerpt=item.excerpt + ' Corrected: the camera was working.'), reference)
        for item, reference in items]))(*passages(*args)))
    refused = client.post(draft['save_url'], data=save_form(draft), headers={'Accept': 'application/json'})
    assert refused.status_code == 409 and 'changed since this draft' in refused.json()['message']
    assert not notes(bench, matter)
