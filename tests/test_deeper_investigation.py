"""Synthetic explicit widening of persisted model abstentions."""
from dataclasses import replace
from html.parser import HTMLParser
import io
import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from case_intelligence.deeper_investigation import install_deeper_investigation
from case_intelligence.generation import EvidenceItem, GroundedGenerationService
from case_intelligence.workbench import CaseIntelligenceWorkbench, create_workbench_app
from case_intelligence.workspace_store import WorkspaceStore
from tests.test_generation import FakeGenerator
from tests.test_matter_notebook import WEB_ACTOR, _create_matter

RAW_FALSE = {'answerable': False, 'claims': [], 'limitation': None, 'missing_information': ''}
QUESTION = 'Why did the synthetic bicycle arrive at noon?'


def unsupported():
    return GroundedGenerationService._verify(RAW_FALSE, (), 1)


class Offers(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms, self.current = [], None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'form' and 'data-deeper-investigation' in values:
            self.current = dict(action=values['action'], data={}, disabled=False)
            self.forms.append(self.current)
        elif self.current is not None:
            if tag == 'input':
                self.current['data'][values['name']] = values.get('value', '')
            if tag == 'button':
                self.current['disabled'] = 'disabled' in values

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current = None


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_DEEPER_INVESTIGATION', '1')
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    app = create_workbench_app(tmp_path / 'runtime', generator=FakeGenerator(RAW_FALSE), auth_mode='test', background_ingestion=False)
    bench = app.state.workbench
    bench.answers.close()
    bench.answers = None
    bench.research.close()
    bench.research = None
    with TestClient(app) as client:
        slug = _create_matter(client, 'Synthetic deeper investigation')
        matter = bench.matter(slug, WEB_ACTOR)
        document, _ = bench.source_store(matter).store_stream('Synthetic bicycle.txt', 'text/plain',
            io.BytesIO(b'The synthetic bicycle arrived at noon.'))
        bench.workspace.reconcile_source_organizations(matter.matter_id,
            [(document.document_id, 'upload', document.display_name)])
        yield client, bench, matter, document


def save_answer(bench, matter, *, answer=None, scope=None, payload=None, suffix='a', actor=WEB_ACTOR):
    conversation = bench.workspace.get_conversation(matter.matter_id)
    job, _ = bench.workspace.queue_answer_job(matter.matter_id, conversation.conversation_id,
        actor, QUESTION, 'answer-request-' + suffix * 32, scope)
    assert bench.workspace.claim_answer_job('synthetic-worker').job_id == job.job_id
    answer = unsupported() if answer is None else answer
    encoded = CaseIntelligenceWorkbench._answer_payload(answer, {}) if payload is None else payload
    message = bench.workspace.finish_answer_job(job.job_id, content=answer.text, payload=encoded)
    return conversation, job, message


def template_helper(bench, actor=WEB_ACTOR):
    templates = SimpleNamespace(env=SimpleNamespace(globals={}))
    install_deeper_investigation(templates, bench=bench, auth_context=lambda _: SimpleNamespace(principal_id=actor))
    return templates.env.globals


def test_raw_false_provenance_excludes_verification_and_no_evidence_abstention():
    verified = unsupported()
    assert verified.answerable is False and verified.model_called and verified.generator_answerable is False
    payload = CaseIntelligenceWorkbench._answer_payload(verified, {})
    assert payload['generator_answerable'] is False
    rejected = CaseIntelligenceWorkbench._verification_abstention()
    no_evidence = GroundedGenerationService(FakeGenerator(RAW_FALSE)).answer(QUESTION, ())
    for answer in (rejected, no_evidence):
        assert answer.answerable is False and answer.generator_answerable is None
        assert 'generator_answerable' not in CaseIntelligenceWorkbench._answer_payload(answer, {})
    supported = GroundedGenerationService._verify({'answerable': True,
        'claims': [{'text': 'The synthetic bicycle arrived at noon.', 'evidence_ids': ['S1']}],
        'limitation': None, 'missing_information': ''},
        (EvidenceItem('S1', 'Synthetic bicycle.txt', 'Line 1', 'The synthetic bicycle arrived at noon.'),), 1)
    assert supported.answerable and 'generator_answerable' not in CaseIntelligenceWorkbench._answer_payload(supported, {})


@pytest.mark.parametrize('flag', [None, '', '0', 'false', 'enabled'])
def test_flag_off_skips_reads_and_returns_no_offer(monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv('CASE_INTELLIGENCE_DEEPER_INVESTIGATION', raising=False)
    else:
        monkeypatch.setenv('CASE_INTELLIGENCE_DEEPER_INVESTIGATION', flag)
    globals_ = template_helper(None)
    assert globals_['deeper_investigation_enabled'] is False
    assert globals_['deeper_investigation_offer'](None, None, None) is None


@pytest.mark.parametrize('flag', ['1', ' True ', 'yes', 'ON'])
def test_flag_uses_existing_factory_truthies(monkeypatch, flag):
    monkeypatch.setenv('CASE_INTELLIGENCE_DEEPER_INVESTIGATION', flag)
    assert template_helper(None)['deeper_investigation_enabled'] is True


@pytest.mark.parametrize('payload', [
    {'kind': 'generated', 'answerable': True},
    {'kind': 'generated', 'answerable': True, 'generator_answerable': False},
    {'kind': 'not-supported', 'answerable': False, 'model_called': True},
    {'kind': 'not-supported', 'answerable': False, 'generator_answerable': False, 'workflow': 'research'},
    {'kind': 'not-supported', 'answerable': False, 'generator_answerable': 0},
    {'kind': 'not-supported', 'answerable': False, 'generator_answerable': False, 'model_called': False},
])
def test_other_responses_get_no_offer_without_inspecting_text_or_citations(app_client, payload):
    client, bench, matter, _document = app_client
    conversation, _job, _message = save_answer(bench, matter, payload=payload)
    for path in (f'/matters/{matter.slug}?conversation={conversation.conversation_id}',
                 f'/matters/{matter.slug}/assistant?conversation={conversation.conversation_id}'):
        assert not Offers(client.get(path).text).forms


def test_both_presentations_reuse_original_question_scope_and_stable_explicit_submission(app_client):
    client, bench, matter, document = app_client
    scope = bench.workspace.create_source_set(matter.matter_id, 'Synthetic original scope', [document.document_id], WEB_ACTOR)
    conversation, _job, message = save_answer(bench, matter, scope=scope.source_set_id)
    forms = []
    for path in (f'/matters/{matter.slug}?conversation={conversation.conversation_id}',
                 f'/matters/{matter.slug}/assistant?conversation={conversation.conversation_id}'):
        page = client.get(path)
        assert page.status_code == 200
        [form] = Offers(page.text).forms
        assert not form['disabled'] and form['action'] == f'/matters/{matter.slug}/ask'
        assert form['data']['question'] == QUESTION
        assert form['data']['conversation'] == conversation.conversation_id
        assert form['data']['source_set'] == scope.source_set_id
        assert form['data']['review_task'] == 'research'
        assert 'csrf_token' in form['data']
        forms.append(form)
    assert forms[0]['data']['request_key'] == forms[1]['data']['request_key']
    assert bench.workspace.research_jobs(matter.matter_id, WEB_ACTOR) == ()
    first = client.post(forms[0]['action'], data=forms[0]['data'], follow_redirects=False)
    second = client.post(forms[0]['action'], data=forms[0]['data'], follow_redirects=False)
    assert first.status_code == second.status_code == 303
    [job] = bench.workspace.research_jobs(matter.matter_id, WEB_ACTOR)
    assert job.question == QUESTION and job.conversation_id == conversation.conversation_id
    assert job.source_set_id == scope.source_set_id
    assert template_helper(bench)['deeper_investigation_offer'](None, matter, message)['disabled']


def test_job_binding_membership_and_missing_source_scope_cannot_broaden_request(app_client):
    _client, bench, matter, document = app_client
    scope = bench.workspace.create_source_set(matter.matter_id, 'Synthetic scope', [document.document_id], WEB_ACTOR)
    _conversation, job, message = save_answer(bench, matter, scope=scope.source_set_id)
    helper = template_helper(bench)['deeper_investigation_offer']
    assert helper(None, replace(matter, matter_id='other-matter'), message) is None
    assert template_helper(bench, 'synthetic-nonmember')['deeper_investigation_offer'](None, matter, message) is None
    with bench.workspace._lock, bench.workspace.connection:
        bench.workspace.connection.execute('DELETE FROM workbench_source_set_item WHERE source_set_id=?', (scope.source_set_id,))
    result = helper(None, matter, message)
    assert result['disabled'] and result['source_set'] == scope.source_set_id
    with bench.workspace._lock, bench.workspace.connection:
        bench.workspace.connection.execute("UPDATE workbench_message SET content='Different question' WHERE message_id=?", (job.question_message_id,))
    assert helper(None, matter, message) is None


def test_recorded_scope_without_binding_does_not_fall_back_to_all_sources(app_client):
    _client, bench, matter, document = app_client
    scope = bench.workspace.create_source_set(matter.matter_id, 'Synthetic recorded scope', [document.document_id], WEB_ACTOR)
    payload = {**CaseIntelligenceWorkbench._answer_payload(unsupported(), {}), 'source_scope': scope.name}
    _conversation, job, message = save_answer(bench, matter, scope=scope.source_set_id, payload=payload)
    with bench.workspace._lock, bench.workspace.connection:
        bench.workspace.connection.execute('DELETE FROM workbench_answer_source_scope WHERE job_id=?', (job.job_id,))
    assert template_helper(bench)['deeper_investigation_offer'](None, matter, message) is None


def test_store_basis_enforces_member_matter_and_conversation_boundaries(app_client):
    _client, bench, matter, document = app_client
    scope = bench.workspace.create_source_set(matter.matter_id, 'Synthetic scoped answer', [document.document_id], WEB_ACTOR)
    conversation, _job, message = save_answer(bench, matter, scope=scope.source_set_id)
    read = bench.workspace.answer_investigation_basis
    basis = read(matter.matter_id, WEB_ACTOR, conversation.conversation_id, message.message_id)
    assert (basis.question, basis.source_set_id, basis.scope_available, basis.can_query, basis.has_active_work) == (
        QUESTION, scope.source_set_id, True, True, False)
    assert bench.workspace.answer_source_set_id(matter.matter_id, _job.job_id) == scope.source_set_id
    other_matter = bench.create_matter('Synthetic other investigation', 'Synthetic fixture', WEB_ACTOR)
    other_conversation = bench.workspace.create_conversation(matter.matter_id)
    with pytest.raises(KeyError):
        read(matter.matter_id, 'synthetic-nonmember', conversation.conversation_id, message.message_id)
    with pytest.raises(KeyError):
        read(other_matter.matter_id, WEB_ACTOR, conversation.conversation_id, message.message_id)
    assert read(matter.matter_id, WEB_ACTOR, other_conversation.conversation_id, message.message_id) is None
    assert read(matter.matter_id, WEB_ACTOR, conversation.conversation_id, 'missing-message') is None
    assert bench.workspace.answer_source_set_id(other_matter.matter_id, _job.job_id) is None


@pytest.mark.parametrize('state', ['queued', 'running', 'failed', 'cancelled'])
def test_store_basis_requires_a_completed_job(app_client, state):
    _client, bench, matter, _document = app_client
    conversation, job, message = save_answer(bench, matter)
    with bench.workspace._lock, bench.workspace.connection:
        bench.workspace.connection.execute('UPDATE workbench_answer_job SET state=? WHERE job_id=?', (state, job.job_id))
    assert bench.workspace.answer_investigation_basis(
        matter.matter_id, WEB_ACTOR, conversation.conversation_id, message.message_id) is None


@pytest.mark.parametrize('length', [0, 2_000, 2_001])
def test_store_basis_bounds_question_without_truncating(app_client, length):
    _client, bench, matter, _document = app_client
    conversation, job, message = save_answer(bench, matter)
    question = 'x' * length
    with bench.workspace._lock, bench.workspace.connection:
        bench.workspace.connection.execute('UPDATE workbench_answer_job SET question=? WHERE job_id=?', (question, job.job_id))
        bench.workspace.connection.execute('UPDATE workbench_message SET content=? WHERE message_id=?', (question, job.question_message_id))
    basis = bench.workspace.answer_investigation_basis(matter.matter_id, WEB_ACTOR, conversation.conversation_id, message.message_id)
    if length == 2_000:
        assert basis.question == question
    else:
        assert basis is None


def test_template_offer_uses_only_public_store_basis(app_client):
    _client, bench, matter, _document = app_client
    _conversation, _job, message = save_answer(bench, matter)
    facade = SimpleNamespace(answer_investigation_basis=bench.workspace.answer_investigation_basis)
    result = template_helper(SimpleNamespace(workspace=facade, generator=bench.generator))['deeper_investigation_offer'](
        None, matter, message)
    assert result['question'] == QUESTION and not result['disabled']


def test_store_basis_does_not_create_a_conversation_when_none_is_active(app_client):
    _client, bench, matter, _document = app_client
    conversation, _job, message = save_answer(bench, matter)
    with bench.workspace._lock, bench.workspace.connection:
        bench.workspace.connection.execute(
            "UPDATE workbench_conversation_organization SET state='archived',archived_at=updated_at,archived_by=? "
            "WHERE matter_id=?", (WEB_ACTOR, matter.matter_id))
    before = bench.workspace.connection.total_changes
    with pytest.raises(KeyError):
        bench.workspace.answer_investigation_basis(matter.matter_id, WEB_ACTOR, conversation.conversation_id, message.message_id)
    assert bench.workspace.connection.total_changes == before
    assert bench.workspace.conversations(matter.matter_id, include_archived=False) == ()
    assert template_helper(bench)['deeper_investigation_offer'](None, matter, message) is None


def test_authorized_teammate_gets_own_retry_identity_and_no_optional_context_is_forwarded(app_client):
    _client, bench, matter, _document = app_client
    payload = {**CaseIntelligenceWorkbench._answer_payload(unsupported(), {}), 'notebook_context': {'mode': 'confirmed'}}
    _conversation, _job, message = save_answer(bench, matter, payload=payload)
    actor = 'synthetic-other-member'
    bench.workspace.upsert_principal('test', actor, 'Other Example', actor, preferred_principal_id=actor)
    bench.workspace.add_member(matter.matter_id, actor, WEB_ACTOR)
    owner = template_helper(bench)['deeper_investigation_offer'](None, matter, message)
    member = template_helper(bench, actor)['deeper_investigation_offer'](None, matter, message)
    assert owner['request_key'] != member['request_key'] and member['question'] == QUESTION
    assert member['has_context'] and 'use_saved_context' not in member and 'notebook_mode' not in member


def test_default_off_real_page_has_no_offer_or_asset(tmp_path, monkeypatch):
    monkeypatch.delenv('CASE_INTELLIGENCE_DEEPER_INVESTIGATION', raising=False)
    app = create_workbench_app(tmp_path / 'runtime', auth_mode='test', background_ingestion=False)
    with TestClient(app) as client:
        slug = _create_matter(client)
        page = client.get(f'/matters/{slug}')
        assert 'data-deeper-investigation' not in page.text and 'deeper-investigation.css' not in page.text


def test_sqlite_backup_clean_reopen_preserves_optional_provenance_and_question_binding(app_client, tmp_path):
    _client, bench, matter, _document = app_client
    conversation, job, message = save_answer(bench, matter)
    _conversation, legacy_job, legacy = save_answer(bench, matter,
        answer=replace(unsupported(), generator_answerable=None), suffix='b')
    restored_path = tmp_path / 'synthetic-restored.sqlite'
    with bench.workspace._lock, sqlite3.connect(restored_path) as target:
        bench.workspace.connection.backup(target)
    restored = WorkspaceStore(restored_path)
    try:
        messages = {item.message_id: item for item in restored.messages(matter.matter_id, conversation.conversation_id)}
        saved = restored.get_answer_job(matter.matter_id, WEB_ACTOR, job.job_id)
        assert saved.question == QUESTION and saved.result_message_id == message.message_id
        assert messages[saved.question_message_id].content == saved.question
        assert messages[message.message_id].payload['generator_answerable'] is False
        assert 'generator_answerable' not in messages[legacy.message_id].payload
        restored_bench = SimpleNamespace(workspace=restored, generator=SimpleNamespace(available=True))
        offer = template_helper(restored_bench)['deeper_investigation_offer']
        assert offer(None, matter, messages[message.message_id])['question'] == QUESTION
        assert offer(None, matter, messages[legacy.message_id]) is None
        assert restored.get_answer_job(matter.matter_id, WEB_ACTOR, legacy_job.job_id).result_message_id == legacy.message_id
    finally:
        restored.close()


def test_offer_submission_preserves_real_csrf_membership_and_admin_boundaries(tmp_path, monkeypatch):
    from tests.test_matter_management import ADMIN, OWNER, OTHER, _app, _csrf, _headers, _principal_id
    from tests.test_matter_management import _create_matter as create_authorized
    monkeypatch.setenv('CASE_INTELLIGENCE_DEEPER_INVESTIGATION', '1')
    app = _app(tmp_path)
    bench = app.state.workbench
    bench.answers.close()
    bench.answers = None
    bench.research.close()
    bench.research = None
    bench.generator = GroundedGenerationService(FakeGenerator(RAW_FALSE))
    with TestClient(app, base_url='https://recordbench.example.test') as client:
        csrf = _csrf(client.get('/matters/new', headers=_headers(OWNER)).text)
        slug = create_authorized(client, principal=OWNER, csrf_token=csrf, name='Synthetic protected offer')
        actor = _principal_id(client, OWNER)
        matter = bench.matter(slug, actor)
        bench.source_store(matter).store_stream('Synthetic source.txt', 'text/plain', io.BytesIO(b'Synthetic bicycle arrived at noon.'))
        conversation, _job, _message = save_answer(bench, matter, actor=actor)
        path = f'/matters/{slug}?conversation={conversation.conversation_id}'
        [form] = Offers(client.get(path, headers=_headers(OWNER)).text).forms
        without_csrf = {key: value for key, value in form['data'].items() if key != 'csrf_token'}
        assert client.post(form['action'], data=without_csrf, headers=_headers(OWNER)).status_code == 403
        for principal, expected in ((OTHER, 404), (ADMIN, 403)):
            token = _csrf(client.get('/matters/new', headers=_headers(principal)).text)
            response = client.post(form['action'], data={**form['data'], 'csrf_token': token}, headers=_headers(principal))
            assert response.status_code == expected
        admin = client.get(path, headers=_headers(ADMIN))
        assert admin.status_code == 200 and not Offers(admin.text).forms
        assert bench.workspace.research_jobs(matter.matter_id, actor) == ()
