"""Synthetic evidence-connection navigation, provenance and authority journeys."""
import html
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlparse
from xml.etree import ElementTree

from fastapi.testclient import TestClient
import pytest

from tests.test_assertion_workflow import (
    EVENT_FIELDS, ORIGINALS, _action, _app, _create_event, _detail, _entity,
    _passage, _upload, protected_assertion,
)
from tests.test_matter_notebook import WEB_ACTOR, _create_matter


class _LinkParser(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.current = None
        self.feed(markup)

    def handle_starttag(self, tag, attributes):
        if tag == 'a':
            self.current = [dict(attributes).get('href', ''), '']

    def handle_data(self, data):
        if self.current is not None:
            self.current[1] += data

    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            self.links.append(tuple(self.current))
            self.current = None


def _links(response):
    return _LinkParser(response.text).links


def _first_link(response, expected_label):
    for href, label in _links(response):
        if label == expected_label:
            return href
    raise StopIteration


def _graph_url(slug, entity_id, page=1):
    return f'/matters/{slug}/connections?' + urlencode(dict(
        entity_id=entity_id, page=page, return_to=f'/matters/{slug}/entities'))


@pytest.fixture
def connections(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    with TestClient(_app(tmp_path / 'runtime')) as client:
        slug = _create_matter(client, 'Synthetic evidence connections')
        _upload(client, slug)
        supporting, _, _ = _passage(client, slug, 'Morgan')
        competing, _, _ = _passage(client, slug, 'Riley')
        entity_id = _entity(client, slug, 'Alex Example', support=supporting)
        assertion_id = _create_event(client, slug, entity_id, supporting)
        assert _action(client, slug, assertion_id, 'attach', support=competing,
            stance='competing', attributed_to='Riley Demo, competing account').status_code == 200
        neighbor_id = _entity(client, slug, 'Cedar Depot', entity_type='place', support=supporting)
        assert _action(client, slug, assertion_id, 'add_role',
            entity_selection=f'{neighbor_id}:1', role='location').status_code == 200
        unrelated_token, _, _ = _passage(client, slug, 'museum')
        unrelated_id = _entity(client, slug, 'Alex Example', support=unrelated_token)
        unrelated_assertion = _create_event(client, slug, unrelated_id, unrelated_token,
            title='Unrelated museum activity', statement='The unrelated Alex catalogued postcards.')
        yield dict(client=client, slug=slug, entity_id=entity_id, assertion_id=assertion_id,
            neighbor_id=neighbor_id, supporting=supporting, competing=competing,
            unrelated_id=unrelated_id, unrelated_assertion=unrelated_assertion)


def test_entity_connections_preserve_accounts_dates_identity_and_original_return(connections):
    context = connections
    client, slug = context['client'], context['slug']
    prefix = f'/matters/{slug}'
    graph_url = _graph_url(slug, context['entity_id'])
    entity_page = client.get(prefix + '/entities/' + context['entity_id'])
    entry_links = [href for href, label in _links(entity_page) if 'Explore connections' in label]
    assert entry_links
    entry = urlparse(entry_links[0])
    assert entry.path == prefix + '/connections'
    assert parse_qs(entry.query)['entity_id'] == [context['entity_id']]

    graph = client.get(graph_url)
    assert graph.status_code == 200 and graph.headers['cache-control'] == 'no-store'
    assert ('#main-content', 'Skip to main content') in _links(graph)
    assert '<main id="main-content" tabindex="-1"' in graph.text
    for text in ('Evidence connections', EVENT_FIELDS['title'], EVENT_FIELDS['raw_date'],
                 EVENT_FIELDS['date_uncertainty'], 'Morgan Sample, supporting account',
                 'Riley Demo, competing account', 'Cedar Depot'):
        assert text in graph.text
    assert 'supporting' in graph.text.casefold() and 'competing' in graph.text.casefold()
    assert 'Unrelated museum activity' not in graph.text
    assert context['unrelated_assertion'] not in graph.text

    links = _links(graph)
    focuses = [parse_qs(urlparse(href).query).get('entity_id', [])
        for href, _ in links if urlparse(href).path == prefix + '/connections']
    assert [context['neighbor_id']] in focuses
    assert [context['unrelated_id']] not in focuses
    exact_queries = [parse_qs(urlparse(href).query) for href, _ in links
        if urlparse(href).path == prefix + '/exact-search']
    assert any(query.get('phrase') == ['Alex Example'] and query.get('search') == ['true']
        for query in exact_queries)

    for token, excerpt in ((context['supporting'], ORIGINALS['Generated supporting account.txt']),
                           (context['competing'], ORIGINALS['Generated competing account.txt'])):
        originals = [href for href, _ in links if urlparse(href).path == prefix
            and parse_qs(urlparse(href).query).get('support') == [token]]
        assert originals
        assert all(parse_qs(urlparse(href).query).get('entity_return_to') == [graph_url]
            for href in originals)
        opened = client.get(originals[0])
        assert opened.status_code == 200 and excerpt in opened.text
        assert any(href == graph_url and 'Return to review context' in label
            for href, label in _links(opened))
        returned = client.get(graph_url)
        assert returned.status_code == 200 and EVENT_FIELDS['title'] in returned.text

    neighbor_links = [href for href, _ in links if urlparse(href).path == prefix + '/connections'
        and parse_qs(urlparse(href).query).get('entity_id') == [context['neighbor_id']]]
    refocused = client.get(neighbor_links[0])
    assert refocused.status_code == 200 and EVENT_FIELDS['title'] in refocused.text
    assert 'Unrelated museum activity' not in refocused.text


def test_stale_original_preserves_historical_account_without_active_original_link(connections):
    context = connections
    client, slug = context['client'], context['slug']
    original = _detail(client, slug, context['assertion_id'])
    account = next(value for value in original['accounts'] if value['stance'] == 'supporting')
    bench = client.app.state.workbench
    matter = bench.matter(slug, WEB_ACTOR)
    store = bench.source_store(matter)
    with store.mutation_guard():
        document = store.get(account['document_id'])
        document.version_id = 'f' * 32
        store._save()

    graph = client.get(_graph_url(slug, context['entity_id']))
    assert graph.status_code == 200
    assert account['excerpt'] in graph.text and account['attributed_to'] in graph.text
    assert EVENT_FIELDS['raw_date'] in graph.text and EVENT_FIELDS['date_uncertainty'] in graph.text
    source_queries = [parse_qs(urlparse(href).query) for href, _ in _links(graph)]
    assert not any(query.get('support') == [context['supporting']] for query in source_queries)
    assert any(query.get('support') == [context['competing']] for query in source_queries)


def test_connection_record_and_entity_text_is_html_escaped(connections):
    context = connections
    client, slug = context['client'], context['slug']
    name = '<script>syntheticEntity()</script>'
    title = '<script>syntheticRecord()</script>'
    statement = '<iframe src="https://frames.example.test"></iframe><img src=x onerror="syntheticAccount()">'
    bench = client.app.state.workbench
    matter = bench.matter(slug, WEB_ACTOR)
    entity = bench.entity_service(matter).detail(matter.matter_id, WEB_ACTOR, context['entity_id'])[0]
    bench.entity_service(matter).update(matter.matter_id, WEB_ACTOR, context['entity_id'],
        expected_revision=entity['revision'], display_name=name)
    assert _action(client, slug, context['assertion_id'], 'update',
        **(EVENT_FIELDS | dict(title=title, statement=statement))).status_code == 200
    graph = client.get(_graph_url(slug, context['entity_id']))
    assert graph.status_code == 200
    for value in (name, title, statement):
        assert value not in graph.text
        assert value in html.unescape(graph.text)


def test_find_more_originals_preserves_operator_characters_as_literal_phrase(connections):
    context = connections
    client, slug = context['client'], context['slug']
    name = 'Alex "Example" AND vendor:(Cedar+Depot)'
    alias = 'Cedar OR NOT (Depot) + invoice:42'
    bench = client.app.state.workbench
    matter = bench.matter(slug, WEB_ACTOR)
    entity = bench.entity_service(matter).detail(matter.matter_id, WEB_ACTOR, context['entity_id'])[0]
    bench.entity_service(matter).update(matter.matter_id, WEB_ACTOR, context['entity_id'],
        expected_revision=entity['revision'], display_name=name, aliases=alias)
    graph = client.get(_graph_url(slug, context['entity_id']))
    assert graph.status_code == 200
    search_links = [href for href, _ in _links(graph)
        if urlparse(href).path == f'/matters/{slug}/exact-search']
    queries = [parse_qs(urlparse(href).query) for href in search_links]
    for phrase in (name, alias):
        assert any(query.get('phrase') == [phrase] and query.get('search') == ['true']
            and 'q' not in query for query in queries)
    for href in search_links:
        response = client.get(href)
        assert response.status_code == 200


def test_graph_identity_roundtrips_keep_a_bounded_return_context(connections):
    context = connections
    client, slug = context['client'], context['slug']
    canonical_identity = f"/matters/{slug}/entities/{context['entity_id']}"
    identity_url = canonical_identity
    graph_urls = []
    for _ in range(15):
        identity_page = client.get(identity_url)
        assert identity_page.status_code == 200
        graph_url = _first_link(identity_page, 'Explore connections')
        query = parse_qs(urlparse(graph_url).query)
        assert query['entity_id'] == [context['entity_id']]
        assert query['return_to'] == [canonical_identity]
        graph_urls.append(graph_url)
        graph = client.get(graph_url)
        assert graph.status_code == 200
        identity_url = _first_link(graph, 'Open identity')
    assert len(set(graph_urls)) == 1
    original_url = _first_link(graph, 'Open original passage')
    original = client.get(original_url)
    assert original.status_code == 200
    graph_context = parse_qs(urlparse(original_url).query)['entity_return_to'][0]
    assert any(href == graph_context and label == 'Return to review context'
               for href, label in _links(original))
    assert client.get(graph_context).status_code == 200


@pytest.mark.parametrize('record_count', (1, 8))
def test_diagram_keeps_final_omitted_roles_label_inside_viewport(connections, record_count):
    context = connections
    client, slug = context['client'], context['slug']
    records = [context['assertion_id']]
    for number in range(1, record_count):
        records.append(_create_event(client, slug, context['entity_id'], context['supporting'],
                                     title=f'Synthetic linked event {number}'))
    neighbors = [_entity(client, slug, f'Synthetic graph neighbor {number}') for number in range(5)]
    for identifier in records:
        for neighbor in neighbors:
            response = _action(client, slug, identifier, 'add_role',
                               entity_selection=f'{neighbor}:1', role='participant')
            assert response.status_code == 200
    graph = client.get(_graph_url(slug, context['entity_id']))
    assert graph.status_code == 200
    start = graph.text.index('<svg class="evidence-graph"')
    markup = graph.text[start:graph.text.index('</svg>', start) + len('</svg>')]
    diagram = ElementTree.fromstring(markup)
    height = float(diagram.attrib['viewBox'].split()[3])
    labels = [node for node in diagram.iter('text')
              if 'additional role(s)' in ''.join(node.itertext())]
    assert len(labels) == record_count
    # Leave room below the text baseline too, so SVG overflow cannot clip the
    # omission notice on either a one-record neighborhood or a full page.
    assert all(float(label.attrib['y']) + 16 <= height for label in labels)


def test_page_beyond_live_neighborhood_redirects_to_available_records(connections):
    context = connections
    client, slug = context['client'], context['slug']
    response = client.get(_graph_url(slug, context['entity_id'], page=2), follow_redirects=False)
    assert response.status_code == 303
    target = urlparse(response.headers['location'])
    assert target.path == f'/matters/{slug}/connections'
    query = parse_qs(target.query)
    assert query['entity_id'] == [context['entity_id']] and query['page'] == ['1']
    assert query['return_to'] == [f'/matters/{slug}/entities']
    current = client.get(response.headers['location'])
    assert current.status_code == 200 and EVENT_FIELDS['title'] in current.text
    assert 'Page 1 of 1' in current.text
    assert 'No saved connections for this identity yet' not in current.text


def test_cross_matter_entity_id_does_not_expose_graph(connections):
    context = connections
    client = context['client']
    other_slug = _create_matter(client, 'Separate synthetic graph scope')
    response = client.get(_graph_url(other_slug, context['entity_id']))
    assert response.status_code == 404
    for value in ('Alex Example', 'Cedar Depot', EVENT_FIELDS['title'],
                  ORIGINALS['Generated supporting account.txt']):
        assert value not in response.text


def test_revoked_member_cannot_open_connections(protected_assertion):
    context = protected_assertion
    client, bench, matter = (context[key] for key in ('client', 'bench', 'matter'))
    path = _graph_url(matter.slug, context['entity']['entity_id'])
    assert client.get(path, headers=context['headers']).status_code == 200
    bench.workspace.revoke_member(matter.matter_id, context['member_id'], context['owner_id'])
    response = client.get(path, headers=context['headers'])
    assert response.status_code == 404
    assert 'Alex Example' not in response.text and EVENT_FIELDS['title'] not in response.text


def test_midflight_membership_revocation_discards_completed_graph(protected_assertion, monkeypatch):
    from case_intelligence.evidence_graph import EvidenceGraphService

    context = protected_assertion
    client, bench, matter = (context[key] for key in ('client', 'bench', 'matter'))
    original = EvidenceGraphService.neighborhood
    revoked = []

    def project_then_revoke(service, *args, **kwargs):
        projection = original(service, *args, **kwargs)
        bench.workspace.revoke_member(matter.matter_id, context['member_id'], context['owner_id'])
        revoked.append(True)
        return projection

    monkeypatch.setattr(EvidenceGraphService, 'neighborhood', project_then_revoke)
    response = client.get(_graph_url(matter.slug, context['entity']['entity_id']),
        headers=context['headers'])
    assert revoked and response.status_code == 404
    for value in ('Alex Example', 'Morgan Sample', EVENT_FIELDS['title'],
                  ORIGINALS['Generated supporting account.txt']):
        assert value not in response.text
