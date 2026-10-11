"""Synthetic one-box dispatch into existing authorized review workflows."""
from html import unescape
import io
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.testclient import TestClient
import pytest

from case_intelligence import ask_router
from case_intelligence.one_box_routes import install_one_box_routes
from case_intelligence.workbench import create_workbench_app
from tests.test_matter_notebook import EchoGenerator, WEB_ACTOR, _create_matter

SLUG = 'm-' + '1' * 12
TEXTS = {
    'exact': '"red bicycle"',
    'question': 'What do the records say about the red bicycle?',
    'every_source': 'Find all records about the red bicycle',
}


def unit_app(monkeypatch, *, flag='1', ready=True, answer_response=None):
    if flag is None:
        monkeypatch.delenv('CASE_INTELLIGENCE_ONE_BOX', raising=False)
    else:
        monkeypatch.setenv('CASE_INTELLIGENCE_ONE_BOX', flag)
    calls = []
    app = FastAPI()
    templates = SimpleNamespace(env=SimpleNamespace(globals={}))
    def authorized(request, slug):
        if slug != SLUG:
            raise HTTPException(404, 'Matter not found')
        return SimpleNamespace(slug=slug, matter_id='generated-matter')
    def csrf(request: Request):
        if request.headers.get('x-csrf-token') != 'generated-csrf':
            raise HTTPException(403, 'CSRF refused')
    def ask(**kwargs):
        calls.append(kwargs)
        return answer_response if answer_response is not None else RedirectResponse(
            f'/matters/{SLUG}?conversation=generated-conversation#matter-question', status_code=303)
    install_one_box_routes(app, authorized_matter=authorized, require_csrf=csrf, templates=templates,
        ask=ask, readiness_for=lambda matter_id: SimpleNamespace(can_query=ready),
        home=lambda **kwargs: HTMLResponse(kwargs['error']))
    return app, calls, templates


def submit(client, text, *, slug=SLUG, route='', headers=None):
    return client.post(f'/matters/{slug}/one-box', data=dict(question=text, route=route),
        headers={'x-csrf-token': 'generated-csrf'} if headers is None else headers, follow_redirects=False)


@pytest.mark.parametrize('kind', tuple(TEXTS))
def test_each_classifier_kind_dispatches_without_duplicating_answer_logic(monkeypatch, kind):
    app, calls, _ = unit_app(monkeypatch)
    original = ask_router.classify
    classified = []
    def classify(text):
        classified.append(text)
        return original(text)
    monkeypatch.setattr(ask_router, 'classify', classify)
    with TestClient(app) as client:
        response = submit(client, TEXTS[kind])
    assert response.status_code == 303
    assert classified == [TEXTS[kind]]
    destination = urlsplit(response.headers['location'])
    query = parse_qs(destination.query)
    assert query['one_box_text'] == [TEXTS[kind]]
    assert query['one_box_kind'] == [kind]
    if kind == 'exact':
        assert destination.path.endswith('/exact-search') and query['q'] == [TEXTS[kind]]
    elif kind == 'every_source':
        assert destination.path.endswith('/full-review') and 'run' not in query
    else:
        assert destination.path == f'/matters/{SLUG}'
        assert query['conversation'] == ['generated-conversation'] and destination.fragment == 'matter-question'
    assert len(calls) == (kind == 'question')
    if calls:
        call = calls[0]
        assert call['question'] == TEXTS[kind]
        assert {key: value for key, value in call.items() if key not in {'request', 'slug', 'question'}} == {
            'conversation': '', 'request_key': '', 'source_set': '', 'notebook_mode': '', 'notebook_item': [],
            'use_saved_context': False, 'expected_selection_revision': None, 'review_task': 'answer'}


@pytest.mark.parametrize('value', [None, '', 'false', '0', 'enabled'])
def test_flag_off_is_404_before_csrf_and_has_no_template_feature(monkeypatch, value):
    app, calls, templates = unit_app(monkeypatch, flag=value)
    with TestClient(app) as client:
        assert submit(client, TEXTS['exact'], headers={}).status_code == 404
        assert submit(client, TEXTS['exact'], headers={'x-csrf-token': 'wrong'}).status_code == 404
    assert not calls and templates.env.globals['one_box_enabled'] is False


@pytest.mark.parametrize('value', ['1', 'true', ' yes ', 'ON', ' True '])
def test_flag_uses_existing_automatic_discovery_truthy_values(monkeypatch, value):
    app, _, templates = unit_app(monkeypatch, flag=value)
    with TestClient(app) as client:
        assert submit(client, TEXTS['exact']).status_code == 303
    assert templates.env.globals['one_box_enabled'] is True


def test_csrf_matter_authority_and_server_readiness_precede_dispatch(monkeypatch):
    app, calls, _ = unit_app(monkeypatch, ready=False)
    with TestClient(app) as client:
        assert submit(client, TEXTS['question'], headers={}).status_code == 403
        assert submit(client, TEXTS['question'], slug='m-' + '2' * 12).status_code == 404
        response = submit(client, TEXTS['question'])
    query = parse_qs(urlsplit(response.headers['location']).query)
    assert response.status_code == 303 and 'not ready' in query['error'][0]
    assert query['one_box_text'] == [TEXTS['question']] and not calls


@pytest.mark.parametrize('forced', tuple(TEXTS))
def test_explicit_switch_preserves_identical_text(monkeypatch, forced):
    app, calls, _ = unit_app(monkeypatch)
    text = '  Generated question & quoted <words>?  '
    with TestClient(app) as client:
        response = submit(client, text, route=forced)
    query = parse_qs(urlsplit(response.headers['location']).query)
    assert query['one_box_text'] == [text] and query['one_box_kind'] == [forced]
    if forced == 'question':
        assert calls[0]['question'] == text
    if forced == 'exact':
        assert query['q'] == [text]


def test_long_exact_switch_and_blank_input_recover_without_truncation(monkeypatch):
    app, calls, _ = unit_app(monkeypatch)
    with TestClient(app) as client:
        text = 'Synthetic ' * 60
        response = submit(client, text, route='exact')
        query = parse_qs(urlsplit(response.headers['location']).query)
        assert response.status_code == 303 and urlsplit(response.headers['location']).path.endswith('/home')
        assert query['one_box_text'] == [text] and '512' in query['error'][0]
        assert 'Enter a question' in parse_qs(urlsplit(submit(client, '   ').headers['location']).query)['error'][0]
        assert submit(client, 'x' * 2001).status_code == 422
        assert submit(client, 'Synthetic', route='invalid').status_code == 422
    assert not calls


@pytest.mark.parametrize('status', [202, 409])
def test_canonical_answer_json_and_status_are_preserved(monkeypatch, status):
    expected = {'generated': 'canonical answer response'}
    app, calls, _ = unit_app(monkeypatch, answer_response=JSONResponse(expected, status_code=status))
    with TestClient(app) as client:
        response = submit(client, TEXTS['question'])
    assert response.status_code == status and response.json() == expected and len(calls) == 1


def test_route_context_uses_only_bounded_valid_same_matter_destination_metadata(monkeypatch):
    _, _, templates = unit_app(monkeypatch)
    context = templates.env.globals['one_box_context']
    matter = SimpleNamespace(slug=SLUG)
    def request(path='', **params):
        from urllib.parse import urlencode
        return Request({'type': 'http', 'method': 'GET', 'scheme': 'http', 'server': ('testserver', 80),
            'path': path or f'/matters/{SLUG}/full-review', 'query_string': urlencode(params).encode(), 'headers': []})
    result = context(request(one_box_text=TEXTS['exact'], one_box_kind='every_source'), matter)
    assert result['text'] == result['prefill'] == TEXTS['exact']
    assert result['reason'].startswith(ask_router.classify(TEXTS['exact']).reason)
    assert {item['kind'] for item in result['alternatives']} == {'exact', 'question'}
    assert context(request(one_box_text='x' * 2001, one_box_kind='question'), matter) is None
    assert context(request(one_box_text='Synthetic', one_box_kind='invalid'), matter) is None
    assert context(request(path='/matters/m-000000000000', one_box_text='Synthetic', one_box_kind='question'), matter) is None


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_ONE_BOX', '1')
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    app = create_workbench_app(tmp_path / 'runtime', generator=EchoGenerator(), auth_mode='test', background_ingestion=False)
    with TestClient(app) as client:
        slug = _create_matter(client, 'Synthetic one-box matter')
        bench = app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        bench.source_store(matter).store_stream('Generated bicycle.txt', 'text/plain',
            io.BytesIO(b'The red bicycle arrived at the synthetic depot on 2024-01-02.'))
        yield client, bench, matter


def test_real_exact_answer_and_every_source_destinations_preserve_existing_flows(app_client):
    client, bench, matter = app_client
    for kind in ('exact', 'every_source', 'question'):
        response = submit(client, TEXTS[kind], slug=matter.slug)
        assert response.status_code == 303
        destination = client.get(response.headers['location'])
        assert destination.status_code == 200
        assert ask_router.classify(TEXTS[kind]).reason in destination.text
        assert 'Not what you meant?' in destination.text
        assert 'data-one-box-switch' in destination.text
        if kind == 'exact':
            assert 'Generated bicycle.txt' in destination.text
        elif kind == 'every_source':
            assert TEXTS[kind] in unescape(destination.text)
            assert not bench.workspace.review_criteria(matter.matter_id, WEB_ACTOR)
            assert not bench.workspace.review_runs(matter.matter_id, WEB_ACTOR)
    with bench.workspace._lock:
        jobs = bench.workspace.connection.execute('SELECT question FROM workbench_answer_job WHERE matter_id=?', (matter.matter_id,)).fetchall()
        assert [row['question'] for row in jobs] == [TEXTS['question']]
        assert bench.workspace.connection.execute('SELECT count(*) FROM workbench_research_job WHERE matter_id=?', (matter.matter_id,)).fetchone()[0] == 0


def test_new_full_review_prefill_does_not_overwrite_existing_criterion_and_escapes_text(app_client):
    client, bench, matter = app_client
    original, version = bench.workspace.create_review_criterion(matter.matter_id, WEB_ACTOR,
        title='Existing generated rule', instructions='Keep this saved criterion unchanged.')
    text = 'Find all records about <script>alert("synthetic")</script>'
    response = submit(client, text, slug=matter.slug, route='every_source')
    page = client.get(response.headers['location'])
    assert page.status_code == 200 and text in unescape(page.text)
    assert '<script>alert("synthetic")</script>' not in page.text
    assert 'class="workflow-scope-card"' in page.text
    assert 'class="review-launcher"' not in page.text
    assert len(bench.workspace.review_criteria(matter.matter_id, WEB_ACTOR)) == 1
    assert bench.workspace.review_criterion(matter.matter_id, original.criterion_id).title == original.title
    assert bench.workspace.review_criterion_version(matter.matter_id, version.criterion_version_id).instructions == 'Keep this saved criterion unchanged.'
    assert not bench.workspace.review_runs(matter.matter_id, WEB_ACTOR)


def test_real_csrf_membership_and_read_only_administrator_checks(tmp_path, monkeypatch):
    from tests.test_matter_management import OWNER, OTHER, ADMIN, _app, _csrf, _headers
    from tests.test_matter_management import _create_matter as create_authorized_matter
    monkeypatch.setenv('CASE_INTELLIGENCE_ONE_BOX', '1')
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    with TestClient(_app(tmp_path), base_url='https://recordbench.example.test') as client:
        csrf = _csrf(client.get('/matters/new', headers=_headers(OWNER)).text)
        slug = create_authorized_matter(client, principal=OWNER, csrf_token=csrf, name='Synthetic protected one-box matter')
        path = f'/matters/{slug}/one-box'
        assert client.post(path, headers=_headers(OWNER), data={'question': TEXTS['exact']}).status_code == 403
        assert client.post(path, headers=_headers(OWNER), data={'question': TEXTS['exact'], 'csrf_token': 'wrong'}).status_code == 403
        for principal, status in ((OTHER, 404), (ADMIN, 403)):
            token = _csrf(client.get('/matters/new', headers=_headers(principal)).text)
            denied = client.post(path, headers=_headers(principal), data={'question': TEXTS['exact'], 'csrf_token': token})
            assert denied.status_code == status
    monkeypatch.delenv('CASE_INTELLIGENCE_ONE_BOX')
    with TestClient(_app(tmp_path), base_url='https://recordbench.example.test') as client:
        assert client.get('/matters/new', headers=_headers(OWNER)).status_code == 200
        assert client.post(path, headers=_headers(OWNER), data={'question': TEXTS['exact']}, follow_redirects=False).status_code == 404


def test_flag_off_real_pages_keep_four_tasks_and_ignore_crafted_prefill(tmp_path, monkeypatch):
    monkeypatch.delenv('CASE_INTELLIGENCE_ONE_BOX', raising=False)
    app = create_workbench_app(tmp_path / 'runtime', generator=EchoGenerator(), auth_mode='test', background_ingestion=False)
    with TestClient(app) as client:
        slug = _create_matter(client, 'Synthetic unchanged home')
        bench = app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        criterion, _version = bench.workspace.create_review_criterion(matter.matter_id, WEB_ACTOR,
            title='Existing generated rule', instructions='Keep this saved criterion unchanged.')
        crafted = urlencode({'one_box_text': 'Generated disabled prefill must be ignored',
                             'one_box_kind': 'every_source'})
        home = client.get(f'/matters/{slug}/home?{crafted}')
        review = client.get(f'/matters/{slug}/full-review?{crafted}')
        assert home.status_code == review.status_code == 200
        launcher = home.text.split('<nav aria-label="Choose a review task">', 1)[1].split('</nav>', 1)[0]
        assert launcher.count('<a href=') == 4
        for page in (home, review):
            for marker in ('id="one-box-question"', 'data-one-box-form', 'one-box.css', 'one-box.js',
                           'one-box-route-reason', 'data-one-box-switch', 'Generated disabled prefill must be ignored'):
                assert marker not in page.text
        assert 'class="review-launcher"' in review.text
        assert 'review-snapshot-confirmation' in review.text
        assert criterion.title in review.text


def test_question_retry_reuses_canonical_request_key_and_saved_answer(app_client):
    client, bench, matter = app_client
    key = 'answer-request-' + 'a' * 32
    payload = dict(question=TEXTS['question'], request_key=key)
    first = client.post(f'/matters/{matter.slug}/one-box', data=payload, follow_redirects=False)
    second = client.post(f'/matters/{matter.slug}/one-box', data=payload, follow_redirects=False)
    assert first.status_code == second.status_code == 303
    assert parse_qs(urlsplit(first.headers['location']).query)['conversation'] == parse_qs(urlsplit(second.headers['location']).query)['conversation']
    with bench.workspace._lock:
        assert bench.workspace.connection.execute('SELECT count(*) FROM workbench_answer_job WHERE matter_id=?', (matter.matter_id,)).fetchone()[0] == 1


@pytest.mark.parametrize('kind', tuple(TEXTS))
@pytest.mark.parametrize('character', ['漢', '😀'])
def test_encoded_unicode_limit_retains_draft_without_redirect_or_work(app_client, kind, character):
    client, bench, matter = app_client
    text = character * (512 if kind == 'exact' else 2000)
    response = submit(client, text, slug=matter.slug, route=kind)
    assert response.status_code == 422 and 'location' not in response.headers
    assert 'Shorten it' in response.text and text in unescape(response.text)
    with bench.workspace._lock:
        assert bench.workspace.connection.execute('SELECT count(*) FROM workbench_answer_job').fetchone()[0] == 0
    assert not bench.workspace.review_criteria(matter.matter_id, WEB_ACTOR)
    assert not bench.workspace.review_runs(matter.matter_id, WEB_ACTOR)


def test_encoded_limit_accepts_short_unicode_and_long_ascii(app_client):
    from case_intelligence.one_box_routes import MAX_REDIRECT_BYTES
    client, bench, matter = app_client
    for text, kind in [('漢' * 350, 'exact'), ('😀' * 500, 'every_source'), ('x' * 2000, 'every_source')]:
        response = submit(client, text, slug=matter.slug, route=kind)
        assert response.status_code == 303
        assert len(response.headers['location'].encode('ascii')) <= MAX_REDIRECT_BYTES
        assert parse_qs(urlsplit(response.headers['location']).query)['one_box_text'] == [text]
        assert client.get(response.headers['location']).status_code == 200
