"""Synthetic contracts for bounded discovery projection reads."""
import pytest

from case_intelligence.modules import ModuleDatabase
from case_intelligence.modules.discovery_review.codes import CodeRepository
from case_intelligence.modules.discovery_review.identifiers import IdentifierRepository
from tests.test_matter_archive import archive_case as archive_case, OWNER
from tests.test_discovery_review_list import production as production, rows


@pytest.mark.parametrize("population", [301, 3001])
def test_first_page_does_not_sort_the_population(production, monkeypatch, population):
    from case_intelligence.modules.discovery_review import reader
    client, bench, matter = production
    add_population(bench.source_store(matter), population - 4)
    calls = []
    url = f"/matters/{matter.slug}/production"
    drive(production, manifest(population))
    original = reader.sort_key
    def counted(chosen, item):
        calls.append(item.document_id)
        return original(chosen, item)
    monkeypatch.setattr(reader, "sort_key", counted)
    page = client.get(url)
    assert page.status_code == 200
    assert len(rows(page.text)) == 100
    assert len(calls) <= 101


def test_identifier_document_read_matches_full_read_and_refuses_large_batches(archive_case):
    bench, matter, _ = archive_case
    repo = IdentifierRepository(ModuleDatabase(bench.workspace.connection, bench.workspace._lock), bench.workspace.membership)
    repo.assign(matter.matter_id, OWNER, [("first", "SYN001"), ("second", "SYN002")])
    mark = repo.mark(matter.matter_id)
    assert repo.read_documents(matter.matter_id, ["second", "missing"]) == {
        "second": repo.read(matter.matter_id)["second"]}
    assert repo.read_documents(matter.matter_id, []) == {}
    assert repo.read_documents("other-matter", ["second"]) == {}
    assert repo.read_documents(matter.matter_id, ["second"] * 500) == {"second": "SYN002"}
    with pytest.raises(ValueError):
        repo.read_documents(matter.matter_id, ["second"] * 501)
    assert repo.mark(matter.matter_id) == mark


def test_effective_code_seek_matches_whole_matter_read_and_is_bounded(archive_case):
    bench, matter, _ = archive_case
    repo = CodeRepository(ModuleDatabase(bench.workspace.connection, bench.workspace._lock))
    first = repo.define(matter.matter_id, OWNER, "First")
    second = repo.define(matter.matter_id, OWNER, "Second")
    for document in ("first", "second", "third"):
        repo.record(matter.matter_id, document, first.code_id, OWNER, "apply")
        repo.record(matter.matter_id, document, second.code_id, OWNER, "apply")
    repo.record(matter.matter_id, "first", second.code_id, OWNER, "remove")
    repo.retire(matter.matter_id, first.code_id)
    expected = sorted(tuple(row) for row in repo.current_rows(matter.matter_id) if row[0] != "third")
    found, after = [], None
    while True:
        page = repo.current_page(matter.matter_id, ["first", "second"], after=after, limit=1)
        assert len(page) <= 1
        if not page:
            break
        found.extend(tuple(row[:6]) for row in page)
        after = (page[-1][6], page[-1][1], page[-1][0])
    assert sorted(found) == expected
    assert repo.current_page(matter.matter_id, []) == ()
    assert repo.current_page("other-matter", ["first"]) == ()
    for documents, limit in ((["first"] * 501, 1), (["first"], 501), (["first"], 0)):
        with pytest.raises(ValueError):
            repo.current_page(matter.matter_id, documents, limit=limit)


@pytest.mark.parametrize("descending", [False, True])
def test_sort_keys_preserve_original_values(descending):
    from dataclasses import asdict
    from case_intelligence.modules.discovery_review import fields, reader
    from case_intelligence.modules.discovery_review.stored_index.keys import order_key
    definition = fields.load_definition()
    labels = ("ABC2", "ABC10", "ABC0002", "Straße2", "STRASSE02", "", " ",
              "2025-03-03", "2024-11-20", "ABC" + "9" * 2000,
              "Common prefix " * 100 + "2", "Common prefix " * 100 + "10")
    items = tuple(reader.Item(str(number), str(number), "v1", "document", "text/plain", "", False,
                              {"name": label, "path": f"folder/{number:04}"}, {}, {})
                  for number, label in enumerate(labels))
    before = [asdict(item) for item in items]
    index = reader.Index(None, 0, 0, definition, items, {}, (), frozenset(), ())
    expected = [items[position].document_id for position in index.order(definition, "name", descending)]
    actual = sorted(items, key=lambda item: order_key(definition.field("name"), item, descending))
    assert [item.document_id for item in actual] == expected
    assert [asdict(item) for item in items] == before


def add_population(store, count, *, prefix='Generated'):
    from types import SimpleNamespace
    identities = []
    for start in range(0, count, 100):
        documents = store.register_linked_sources(source_location_id='synthetic-index', sources=[
            SimpleNamespace(relative_path=f'{prefix}/record-{number}.txt', media_type='text/plain',
                            byte_size=1, stable_device=0, stable_inode=number, stable_mtime_ns=0)
            for number in range(start, min(start + 100, count))])
        identities.extend(d.document_id for d in documents)
    return identities


def manifest(population, **changes):
    return dict(R=population, F=1, L=3, H=5, A=population * 3,
                C=0, I=0, V=2 * population * 39, Z=2 * population * 39,
                P=population, **changes)


def service_for(bench):
    from case_intelligence.modules.discovery_review.stored_index.rebuild import SERVICES
    return next(service for service in tuple(SERVICES) if service.context.db.connection is bench.workspace.connection)


def drive(production, dimensions, *, params=None):
    import json
    import math
    from case_intelligence.modules.discovery_review.stored_index.rebuild import work_bound
    client, bench, matter = production
    service = service_for(bench)
    work, early, steps = work_bound(**dimensions)
    previous = -1
    for _ in range(math.ceil(steps / 128)):
        page = client.get(f'/matters/{matter.slug}/production', params=params or {})
        assert page.status_code == 200
        state = service.repository.state(matter.matter_id)
        assert state is not None
        cursor = json.loads(state['checkpoint'])
        assert not service.jobs[matter.matter_id].error
        assert cursor['sequence'] > previous or state['phase'] == 'complete'
        assert service.jobs[matter.matter_id].attempts <= 128
        assert cursor['accumulators']['work'] <= work
        assert cursor['accumulators']['early'] <= early
        previous = cursor['sequence']
        if state['phase'] == 'complete':
            assert not state['pending'] and not state['errors']
            assert all(cursor['eof'].values())
            return page
    pytest.fail('generated manifest exhausted its deterministic GET bound')


def page_link(html, direction):
    import re
    from html import unescape
    found = re.search(r'<a href="([^"]+)"[^>]*data-review-page="' + direction + '"', html)
    return unescape(found[1]) if found else ''


def tokens(html):
    import re
    return re.findall(r'<tr data-review-row data-token="([^"]+)"', html)


@pytest.mark.parametrize('descending', [False, True])
def test_seek_walk_matches_reference_in_both_directions(production, descending):
    from case_intelligence.modules.discovery_review import fields, reader
    client, bench, matter = production
    store = bench.source_store(matter)
    add_population(store, 297)
    first = drive(production, manifest(301), params={'sort': 'name', 'dir': 'desc' if descending else ''})
    definition = fields.load_definition()
    reference = reader.index_for(matter.matter_id, store, definition)
    expected = [reference.items[p].token for p in reference.order(definition, 'name', descending)]
    pages, page = [tokens(first.text)], first
    while link := page_link(page.text, 'next'):
        page = client.get(link)
        pages.append(tokens(page.text))
    assert [token for page_tokens in pages for token in page_tokens] == expected
    for previous in reversed(pages[:-1]):
        page = client.get(page_link(page.text, 'previous'))
        assert tokens(page.text) == previous


def test_substring_windows_account_for_every_candidate(production):
    from case_intelligence.modules.discovery_review import fields, presentation
    from case_intelligence.modules.discovery_review.stored_index.query import list_page
    client, bench, matter = production
    add_population(bench.source_store(matter), 297)
    drive(production, manifest(301))
    service = service_for(bench)
    view = service.current(matter.matter_id, bench.source_store(matter), fields.load_definition())
    choice = presentation.Choice(('name', 'lf:Review Tag', 'coding'), ('name',), 'user')
    for needle in ('record-299', 'Hot', '%_', 'rivera', 'record-2', 'note.eml'):
        url, all_tokens, seen = f'/matters/{matter.slug}/production', [], 0
        cursor = ''
        while True:
            result = list_page(view.definition, view, choice, base=url, q=needle, sort='name', descending=False,
                               page=1, doc='', edge='', show_all=False, cursor=cursor)
            all_tokens.extend(row['item'].token for row in result['rows'])
            assert result['coverage'] > seen
            seen = result['coverage']
            if not result['next_page']:
                assert result['filter_complete'] and seen == 301
                assert result['matches_so_far'] == len(all_tokens)
                break
            from urllib.parse import parse_qs, urlsplit
            cursor = parse_qs(urlsplit(result['next_page']).query)['cursor'][0]
        if needle in ('%_', 'rivera', 'record-299'):
            assert not all_tokens
        elif needle in ('Hot', 'note.eml'):
            assert len(all_tokens) == 1
        else:
            assert all_tokens


def test_cursors_are_scoped_and_generation_bound(production):
    from urllib.parse import parse_qs, urlsplit
    from case_intelligence.modules.discovery_review.stored_index.query import CursorInvalid, decode_cursor
    client, bench, matter = production
    add_population(bench.source_store(matter), 197)
    page = drive(production, manifest(201))
    next_url = page_link(page.text, 'next')
    assert len(tokens(client.get(next_url).text)) == 100
    cursor = parse_qs(urlsplit(next_url).query)['cursor'][0]
    with pytest.raises(CursorInvalid):
        decode_cursor(cursor, 'foreign-matter-or-view', 201)
    with pytest.raises(CursorInvalid):
        decode_cursor(cursor[:-1] + ('0' if cursor[-1] != '0' else '1'), '', 201)
    assert 'Navigation restarted' in client.get(next_url + '&q=changed').text
    add_population(bench.source_store(matter), 1, prefix='Later')
    assert 'Navigation restarted' in client.get(next_url).text
    token = tokens(page.text)[0]
    assert client.get(f'/matters/{matter.slug}/production/items/{token}?view=fields').status_code == 200


def test_identifier_post_keeps_complete_reader_population(production, monkeypatch):
    from case_intelligence.modules.discovery_review.identifiers import IdentifierRepository
    client, bench, matter = production
    add_population(bench.source_store(matter), 297)
    drive(production, manifest(301))
    service = service_for(bench)
    base = f'/matters/{matter.slug}/production'
    response = client.post(base + '/identifiers', data={'prefix': 'DEF', 'digits': 6, 'q': 'record-'}, follow_redirects=False)
    assert response.status_code == 303
    assert len(service.identifiers.read(matter.matter_id)) == 297
    original = IdentifierRepository.assign
    def no_index_write(*args, **kwargs):
        pytest.fail('stored index called authoritative identifier writer')
    monkeypatch.setattr(IdentifierRepository, 'assign', no_index_write)
    drive(production, {**manifest(301), 'I': 297})
    monkeypatch.setattr(IdentifierRepository, 'assign', original)
    response = client.post(base + '/identifiers', data={'action': 'clear', 'confirm': '1', 'q': 'record-'}, follow_redirects=False)
    assert response.status_code == 303
    assert not service.identifiers.read(matter.matter_id)


@pytest.mark.parametrize('population', [100, 1000, 10000])
def test_rebuild_step_row_budget(production, monkeypatch, population, record_property):
    from case_intelligence.modules.discovery_review.stored_index.repository import Repository, Transaction
    from case_intelligence.modules.discovery_review.stored_index.rebuild import Service
    from case_intelligence.pilot_source_changes import SourceChanges
    from case_intelligence.modules.discovery_review import load_files
    client, bench, matter = production
    add_population(bench.source_store(matter), population - 4)
    observed, actual = [], [0]
    original_step, original_read, original_write = Service.step, Transaction.read, Transaction.write
    def count_result(owner, name, count):
        original = getattr(owner, name)
        def observed_call(*args, **kwargs):
            result = original(*args, **kwargs)
            actual[0] += count(result)
            return result
        monkeypatch.setattr(owner, name, observed_call)
    count_result(Repository, 'rows', len)
    count_result(SourceChanges, 'seek', len)
    count_result(SourceChanges, 'read', len)
    count_result(SourceChanges, 'count', lambda result: 1)
    count_result(IdentifierRepository, 'read_documents', len)
    count_result(CodeRepository, 'current_page', lambda result: 2 * len(result))
    count_result(CodeRepository, 'change_token', lambda result: 2)
    count_result(load_files, 'resume_record', lambda result: result[2])
    def read(tx, *args, **kwargs):
        result = original_read(tx, *args, **kwargs)
        actual[0] += len(result) * (kwargs.get('constituents', 1) - 1)
        return result
    def write(tx, *args, **kwargs):
        result = original_write(tx, *args, **kwargs)
        actual[0] += result
        return result
    def step(service, job, budget, *args):
        actual[0] = 0
        result = original_step(service, job, budget, *args)
        assert actual[0] <= budget.touches <= 256
        assert budget.work <= 224 and budget.control <= 32
        observed.append((budget.work, budget.touches))
        return result
    monkeypatch.setattr(Transaction, 'read', read)
    monkeypatch.setattr(Transaction, 'write', write)
    monkeypatch.setattr(Service, 'step', step)
    drive(production, manifest(population))
    service_for(bench).invalidate(matter.matter_id)
    drive(production, manifest(population))
    assert observed and max(work for work, touches in observed) >= 192
    record_property('bounded_step_count', len(observed))
    record_property('maximum_step_touches', max(touches for work, touches in observed))
    record_property('total_step_touches', sum(touches for work, touches in observed))
    record_property('total_work_credits', sum(work for work, touches in observed))


def test_first_get_completes_within_row_work_envelope(production, record_property):
    import json
    from case_intelligence.modules.discovery_review.stored_index.rebuild import work_bound
    from tests.test_discovery_review_list import test_list_leads_with_load_file_values_exactly_as_produced
    dimensions = dict(R=5, F=1, L=3, H=5, I=5, C=11, Z=400, A=45, P=6, V=390)
    work, early, steps = work_bound(**dimensions)
    assert work <= 4232 and early == 76 and steps <= 101 <= 128
    test_list_leads_with_load_file_values_exactly_as_produced(production)
    client, bench, matter = production
    service = service_for(bench)
    state = service.repository.state(matter.matter_id)
    assert state['phase'] == 'complete'
    cursor = json.loads(state['checkpoint'])
    assert cursor['sequence'] <= 128 and cursor['accumulators']['work'] <= 4232
    assert cursor['accumulators']['early'] <= 76
    record_property('committed_steps', cursor['sequence'])
    record_property('work_credits', cursor['accumulators']['work'])
    record_property('early_yields', cursor['accumulators']['early'])


@pytest.mark.parametrize('raw', [
    b'BEGBATES,NATIVEPATH,Note\r\nSYN1,a.txt,"two\r\nlines"\r\n\r\nSYN2,b.txt,end\r\n',
    b'BEGBATES,NATIVEPATH\nSYN1,a.txt\nwrong,width,extra\nSYN2,b.txt\n',
    b'BEGBATES,NATIVEPATH\nSYN1,"unfinished\n',
    b'BEGBATES,NATIVEPATH\n\n,,\n',
    b'ordinary,column\n1,2\n', b'"unfinished', b'BEGBATES,\n',
])
def test_resumable_parser_matches_legacy_without_replaying_records(raw):
    from case_intelligence.modules.discovery_review import fields, load_files
    definition = fields.load_definition()
    expected = load_files.parse(raw, document_id='synthetic', name='synthetic.csv', definition=definition)
    text, state, found, offsets = raw.decode('utf-8-sig'), None, [], []
    for _ in range(len(raw) + 1):
        state, row, touched = load_files.resume_record(text, 'synthetic.csv', definition, state)
        assert 0 <= touched <= 1
        if row:
            found.append(row)
        offsets.append(state['offset'])
        if state['eof']:
            break
    assert offsets == sorted(offsets)
    if expected is None:
        assert not state['recognized'] and not found
    else:
        assert tuple(found) == expected.rows
        assert state['problem'] == expected.problem
        assert state['count'] == len(expected.rows)


def test_corrupt_cursor_restarts_instead_of_claiming_completion(production):
    import json
    from case_intelligence.modules.discovery_review.stored_index.rebuild import decode_cursor
    client, bench, matter = production
    drive(production, manifest(4))
    original = service_for(bench).repository.state(matter.matter_id)
    for key in json.loads(original['checkpoint']):
        cursor = json.loads(original['checkpoint'])
        cursor.pop(key)
        assert decode_cursor({**original, 'checkpoint': json.dumps(cursor)}) is None
    cursor = json.loads(original['checkpoint'])
    cursor['unknown'] = True
    assert decode_cursor({**original, 'checkpoint': json.dumps(cursor)}) is None
    cursor.pop('unknown')
    cursor['eof']['reports'] = False
    assert decode_cursor({**original, 'checkpoint': json.dumps(cursor)}) is None
