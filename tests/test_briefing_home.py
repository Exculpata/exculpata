"""Synthetic Home briefing visibility, source links and read boundaries."""
from contextlib import nullcontext
from html.parser import HTMLParser
import io
from threading import RLock
from types import SimpleNamespace
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from pathlib import Path
import pytest

from case_intelligence import briefing_home as module
from case_intelligence.briefing import Briefing
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workspace_store import WorkspaceProblem
from tests.test_matter_notebook import WEB_ACTOR, _create_matter

SLUG = 'm-' + '1' * 12


def helper(monkeypatch, *, flag='1', readiness=None, member=True):
    if flag is None:
        monkeypatch.delenv('CASE_INTELLIGENCE_BRIEFING', raising=False)
    else:
        monkeypatch.setenv('CASE_INTELLIGENCE_BRIEFING', flag)
    templates = SimpleNamespace(env=SimpleNamespace(globals={}))
    matter = SimpleNamespace(slug=SLUG, matter_id='generated-matter')
    request = Request({'type': 'http', 'method': 'GET', 'scheme': 'http', 'server': ('testserver', 80),
        'path': f'/matters/{SLUG}/home', 'query_string': b'', 'headers': []})
    def membership(*_args):
        if not member:
            raise KeyError('generated-nonmember')
    workspace = SimpleNamespace(membership=membership, _lock=RLock(), connection=None)
    service = SimpleNamespace(source_guard=nullcontext)
    bench = SimpleNamespace(workspace=workspace, entity_service=lambda _matter: service,
        source_store=lambda _matter: SimpleNamespace(get=lambda _id: None))
    readings = iter(readiness or [{}, {}])
    module.install_briefing_home(templates, bench=bench,
        auth_context=lambda _request: SimpleNamespace(principal_id='generated-actor'),
        readiness_for=lambda _matter: next(readings))
    return templates.env.globals, request, matter, bench


@pytest.mark.parametrize('flag', [None, '', 'false', '0', 'enabled'])
def test_disabled_feature_skips_auth_readiness_and_all_collaborators(monkeypatch, flag):
    globals_, request, matter, bench = helper(monkeypatch, flag=flag)
    def forbidden(*_args, **_kwargs):
        pytest.fail('Disabled briefing must not read or assemble anything')
    bench.workspace.membership = forbidden
    bench.entity_service = forbidden
    monkeypatch.setattr(module, 'build_briefing', forbidden)
    assert globals_['briefing_enabled'] is False
    assert globals_['home_briefing'](request, matter) is None


@pytest.mark.parametrize('flag', ['1', ' True ', 'yes', 'ON'])
def test_same_truthy_values_as_automatic_discovery(monkeypatch, flag):
    globals_, request, matter, _bench = helper(monkeypatch, flag=flag, readiness=[{'processing_count': 1}])
    assert globals_['briefing_enabled'] is True
    assert globals_['home_briefing'](request, matter)['state'] == 'processing'


@pytest.mark.parametrize('readiness', [
    {'state': 'preparing'}, {'processing_count': 1, 'can_query': True},
    {'overview_processing_count': 1, 'can_query': True},
    {'discovery': {'working': True}, 'can_query': True},
])
def test_each_processing_stage_suppresses_assembly_even_if_querying_is_allowed(monkeypatch, readiness):
    globals_, request, matter, _bench = helper(monkeypatch, readiness=[readiness])
    monkeypatch.setattr(module, 'build_briefing', lambda *args, **kwargs: pytest.fail('Do not assemble unfinished work'))
    assert globals_['home_briefing'](request, matter)['briefing'] is None


def test_snapshot_completion_is_rechecked_and_read_only_administrator_is_not_impersonated(monkeypatch):
    globals_, request, matter, bench = helper(monkeypatch, readiness=[{}, {'discovery': {'working': True}}])
    calls = []
    def build(*args, **kwargs):
        assert bench.workspace._lock._is_owned()
        calls.append(args)
        return Briefing(matter.matter_id, matter.slug, (), ())
    monkeypatch.setattr(module, 'build_briefing', build)
    assert globals_['home_briefing'](request, matter)['state'] == 'processing'
    assert calls[0][1] == 'generated-actor'
    globals_, request, matter, _bench = helper(monkeypatch, member=False)
    assert globals_['home_briefing'](request, matter) is None
    assert len(calls) == 1


def test_terminal_failures_and_paused_discovery_show_briefing_and_read_limit_is_linked(monkeypatch):
    readiness = {'state': 'attention', 'can_query': False, 'discovery': {'working': False, 'label': 'Paused'}}
    globals_, request, matter, _bench = helper(monkeypatch, readiness=[readiness, readiness])
    briefing = Briefing(matter.matter_id, matter.slug, (), ())
    monkeypatch.setattr(module, 'build_briefing', lambda *args, **kwargs: briefing)
    assert globals_['home_briefing'](request, matter)['briefing'] is briefing
    globals_, request, matter, _bench = helper(monkeypatch)
    def refused(*args, **kwargs):
        raise WorkspaceProblem('Synthetic complete-read limit refusal')
    monkeypatch.setattr(module, 'build_briefing', refused)
    result = globals_['home_briefing'](request, matter)
    assert result['state'] == 'unavailable' and result['briefing'] is None
    assert result['sources_url'] == f'/matters/{SLUG}/setup?view=list'
    assert 'read limit' in result['message']


@pytest.mark.parametrize('initial,fresh', [(False, True), (True, False)])
def test_suggestion_buttons_use_briefing_snapshot_after_home_readiness_race(monkeypatch, initial, fresh):
    globals_, request, matter, _bench = helper(monkeypatch,
        readiness=[{'can_query': fresh}, {'can_query': fresh}])
    question = SimpleNamespace(text='What do the records say about Alex Example?', label='Suggested person', links=())
    briefing = SimpleNamespace(sections=(), coverage=(), questions=(question,))
    monkeypatch.setattr(module, 'build_briefing', lambda *args, **kwargs: briefing)
    view = globals_['home_briefing'](request, matter)
    assert view['state'] == 'ready' and view['can_query'] is fresh
    environment = Environment(loader=FileSystemLoader(Path(module.__file__).parent / 'templates'), autoescape=True)
    html = environment.get_template('workbench_home_briefing.html').render(
        briefing_enabled=True, home_briefing=lambda *_args: view, request=request, matter=matter,
        one_box_enabled=True, administrator_view=False, matter_readiness={'can_query': initial})
    [button] = BriefingMarkup(html).questions
    assert ('disabled' not in button) is fresh
    assert button['type'] == 'button' and button['value'] == question.text


class BriefingMarkup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.sections, self.lines, self.questions, self.coverage = [], [], [], []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if 'data-briefing-section' in values:
            self.sections.append(values['data-briefing-section'])
        if 'data-briefing-line' in values:
            self.current = []
            self.lines.append(self.current)
        if tag == 'a' and self.current is not None:
            self.current.append(values['href'])
        if 'data-briefing-question' in values:
            self.questions.append(values)
        if 'data-briefing-coverage' in values:
            self.coverage.append(values)

    def handle_endtag(self, tag):
        if tag == 'li':
            self.current = None


@pytest.mark.parametrize('one_box', [False, True])
def test_home_leads_with_linked_briefing_and_native_fill_buttons(tmp_path, monkeypatch, one_box):
    monkeypatch.setenv('CASE_INTELLIGENCE_BRIEFING', '1')
    monkeypatch.setenv('CASE_INTELLIGENCE_ONE_BOX', '1' if one_box else '0')
    monkeypatch.delenv('CASE_INTELLIGENCE_AUTOMATIC_DISCOVERY', raising=False)
    app = create_workbench_app(tmp_path / 'runtime', auth_mode='test', background_ingestion=False)
    with TestClient(app) as client:
        slug = _create_matter(client, 'Synthetic briefing Home')
        bench = app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        bench.source_store(matter).store_stream('Generated memo.txt', 'text/plain',
            io.BytesIO(b'Alex Example arrived on 2024-05-06.'))
        bench.run_automatic_discovery_once()
        page = client.get(f'/matters/{slug}/home')
        assert page.status_code == 200
        assert page.text.index('data-discovery-briefing') < page.text.index('class="cockpit-hero"') < page.text.index('data-task-launcher')
        parsed = BriefingMarkup(page.text)
        assert parsed.sections == ['arrived', 'people_places', 'dates', 'unread']
        assert len(parsed.coverage) == 1 and 'open' not in parsed.coverage[0]
        assert len(parsed.questions) == 2 and all(item['type'] == 'button' for item in parsed.questions)
        assert all('value' in item and item['value'].startswith('What do the records say about ') for item in parsed.questions)
        assert all(parsed.lines)
        for href in {href for line in parsed.lines for href in line}:
            assert urlsplit(href).path.startswith(f'/matters/{slug}')
            assert client.get(href).status_code == 200
        assert ('id="one-box-question"' in page.text) is one_box
        if not one_box:
            assert 'id="assistant-question"' in page.text and 'data-assistant-expand' in page.text
        assert bench.workspace.connection.execute('SELECT count(*) FROM workbench_answer_job').fetchone()[0] == 0


def test_flag_off_preserves_home_and_adds_no_assets_markup_or_read(tmp_path, monkeypatch):
    monkeypatch.delenv('CASE_INTELLIGENCE_BRIEFING', raising=False)
    monkeypatch.delenv('CASE_INTELLIGENCE_ONE_BOX', raising=False)
    monkeypatch.setattr(module, 'build_briefing', lambda *args, **kwargs: pytest.fail('Disabled assembler called'))
    app = create_workbench_app(tmp_path / 'runtime', auth_mode='test', background_ingestion=False)
    with TestClient(app) as client:
        slug = _create_matter(client)
        page = client.get(f'/matters/{slug}/home')
        assert page.status_code == 200
        for marker in ('data-briefing-region', 'briefing.css', 'briefing.js', 'data-briefing-question'):
            assert marker not in page.text
        tasks = page.text.split('<nav aria-label="Choose a review task">', 1)[1].split('</nav>', 1)[0]
        assert tasks.count('<a href=') == 4


def test_real_home_assembler_refusal_keeps_existing_workflows_and_linked_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_BRIEFING', '1')
    def refused(*args, **kwargs):
        raise WorkspaceProblem('Synthetic changed records')
    monkeypatch.setattr(module, 'build_briefing', refused)
    app = create_workbench_app(tmp_path / 'runtime', auth_mode='test', background_ingestion=False)
    with TestClient(app) as client:
        slug = _create_matter(client)
        page = client.get(f'/matters/{slug}/home')
        assert page.status_code == 200 and 'data-briefing-unavailable' in page.text
        assert f'href="/matters/{slug}/setup?view=list"' in page.text
        assert 'data-discovery-briefing' not in page.text and 'data-task-launcher' in page.text


def test_real_home_keeps_nonmember_admin_read_only_and_outsider_unauthorized(tmp_path, monkeypatch):
    from tests.test_matter_management import ADMIN, OWNER, OTHER, _app, _csrf, _headers
    from tests.test_matter_management import _create_matter as create_authorized
    monkeypatch.setenv('CASE_INTELLIGENCE_BRIEFING', '1')
    with TestClient(_app(tmp_path), base_url='https://recordbench.example.test') as client:
        token = _csrf(client.get('/matters/new', headers=_headers(OWNER)).text)
        slug = create_authorized(client, principal=OWNER, csrf_token=token, name='Synthetic protected briefing')
        monkeypatch.setattr(module, 'build_briefing', lambda *args, **kwargs: pytest.fail('Nonmember assembler called'))
        admin = client.get(f'/matters/{slug}/home', headers=_headers(ADMIN))
        assert admin.status_code == 200 and 'Administrator view' in admin.text
        assert 'data-briefing-region' not in admin.text
        assert client.get(f'/matters/{slug}/home', headers=_headers(OTHER)).status_code == 404
