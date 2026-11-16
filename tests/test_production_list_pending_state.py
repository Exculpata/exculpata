"""Synthetic production-list currentness and bounded recovery regressions."""
import inspect

from tests.test_discovery_review_dat_opt_storage import app_case
from tests.test_discovery_review_index import service_for


PENDING = 'Indexing, reload shortly.'
PAUSED = 'Indexing paused: the sources keep changing. Reload to resume.'


def test_building_attempt_list_is_pending_then_converges(tmp_path, monkeypatch):
    with app_case(tmp_path, count=40, width=512) as case:
        production = service_for(case.bench).production
        original = production.attempt_current
        forced = []

        def building(matter_id, attempt):
            if attempt['status'] == 'building' and any(
                    frame.function == 'production_panel' for frame in inspect.stack()):
                forced.append(attempt['attempt_id'])
                return False
            return original(matter_id, attempt)

        monkeypatch.setattr(production, 'attempt_current', building)
        source = case.store.documents['00000000000000000000000000000001']
        url = f'/matters/{case.matter.slug}/production'
        params = {'doc': case.store.action_token(source), 'view': 'fields'}
        page = case.client.get(url, params=params)
        assert forced, 'The building-state intervention must execute'
        assert page.status_code == 200
        assert PENDING in page.text
        assert 'data-production-records ' not in page.text
        monkeypatch.setattr(production, 'attempt_current', original)
        for _ in range(8):
            page = case.client.get(url, params=params)
            assert page.status_code == 200
            attempt = production.repository.current_attempt(case.matter.matter_id, source.document_id)
            if attempt['status'] == 'complete':
                break
        assert attempt['status'] == 'complete'
        assert production.attempt_current(case.matter.matter_id, attempt)
        assert 'data-production-records ' in page.text


def request_list(case, **extra):
    source = case.store.documents['00000000000000000000000000000001']
    page = case.client.get(f'/matters/{case.matter.slug}/production', params={
        'doc': case.store.action_token(source), 'view': 'fields', **extra})
    assert page.status_code == 200
    return page


def current_attempt(case):
    return service_for(case.bench).production.repository.current_attempt(
        case.matter.matter_id, '00000000000000000000000000000001')


def converge(case):
    for _ in range(8):
        page = request_list(case)
        attempt = current_attempt(case)
        if attempt['status'] == 'complete':
            assert service_for(case.bench).production.attempt_current(case.matter.matter_id, attempt)
            assert 'data-production-records ' in page.text
            return attempt
    raise AssertionError('The production attempt did not converge within eight requests')


def observe_work(case, monkeypatch):
    production = service_for(case.bench).production
    observed = {'steps': 0, 'restarts': 0}
    advance = production.advance
    restart = production.repository.restart_matching
    begin = production.repository.begin_attempt

    def advancing(*args, **kwargs):
        result = advance(*args, **kwargs)
        observed['steps'] += result['steps']
        return result

    def restarting(*args, **kwargs):
        observed['restarts'] += 1
        return restart(*args, **kwargs)

    def beginning(matter_id, source, **kwargs):
        prior = production.repository.current_attempt(matter_id, source.document_id)
        result = begin(matter_id, source, **kwargs)
        if prior is not None and result != prior['attempt_id']:
            observed['restarts'] += 1
        return result

    monkeypatch.setattr(production, 'advance', advancing)
    monkeypatch.setattr(production.repository, 'restart_matching', restarting)
    monkeypatch.setattr(production.repository, 'begin_attempt', beginning)
    return observed


def change_input(case, number):
    from dataclasses import replace
    document_id = '00000000000000000000000000000002'
    with case.store.mutation_guard():
        document = case.store.get(document_id)
        case.store.documents[document_id] = replace(document, display_name=f'Generated changed {number}.txt')
        case.store._save((document_id,))


def reload_link(page):
    import html
    import re
    return html.unescape(re.search(r'href="([^"]+)">Reload</a>', page.text)[1])


def test_unrelated_commits_do_not_restart_or_expand_work(tmp_path, monkeypatch):
    from tests.test_discovery_review_index import add_population
    with app_case(tmp_path / 'undisturbed', count=80) as case:
        baseline = observe_work(case, monkeypatch)
        converge(case)
    with app_case(tmp_path / 'changing', count=80) as case:
        observed = observe_work(case, monkeypatch)
        request_list(case)
        revision = current_attempt(case)['input_revision']
        for number in range(10):
            add_population(case.store, 1, prefix=f'Unrelated generated {number}')
            request_list(case)
            attempt = current_attempt(case)
            assert attempt['input_revision'] == revision
            if attempt['status'] != 'complete':
                revision = attempt['input_revision']
        converge(case)
        assert observed['restarts'] == 0
        assert observed['steps'] <= 3 * baseline['steps'] + 128


def test_relevant_changes_pause_and_reload_resumes(tmp_path, monkeypatch):
    with app_case(tmp_path, count=80) as case:
        observed = observe_work(case, monkeypatch)
        request_list(case)
        for number in range(10):
            change_input(case, number)
            page = request_list(case)
        assert observed['restarts'] == 3
        assert PAUSED in page.text
        assert 'data-production-restarts="3"' in page.text
        assert 'data-production-records ' not in page.text
        service_for(case.bench).production.cancel(case.matter.matter_id)
        for _ in range(3):
            page = request_list(case)
            assert PAUSED in page.text
            assert 'data-production-restarts="3"' in page.text
        link = reload_link(page)
        resumed = case.client.get(link)
        assert resumed.status_code == 200
        assert PAUSED not in resumed.text
        converge(case)
        assert current_attempt(case)['dependencies']['restarts'] == 0


def test_stale_record_cursor_still_refuses_previous_attempt(tmp_path):
    with app_case(tmp_path, count=3) as case:
        from case_intelligence.modules.discovery_review.production.service import source_from_attempt
        attempt = converge(case)
        production = service_for(case.bench).production
        production.repository.begin_attempt(case.matter.matter_id, source_from_attempt(attempt),
            parser_revision=attempt['parser_revision'], definition_digest=attempt['definition_digest'],
            target=attempt['target'] + '-replacement', input_revision=attempt['input_revision'])
        source = case.store.get(attempt['document_id'])
        page = case.client.get(f'/matters/{case.matter.slug}/production', params={
            'doc': case.store.action_token(source), 'view': 'fields',
            'record': f"dat:{attempt['attempt_id']}:1"})
        assert page.status_code == 409
        assert 'These production records changed. Reload the list.' in page.text
        assert 'data-production-records ' not in page.text


def test_regular_input_cycle_cannot_resume_without_reload(tmp_path, monkeypatch):
    with app_case(tmp_path, count=80) as case:
        observed = observe_work(case, monkeypatch)
        request_list(case)
        paused = False
        for number in range(32):
            if number % 2 == 0:
                change_input(case, number)
            page = request_list(case)
            if PAUSED in page.text:
                paused = True
            if paused:
                assert PAUSED in page.text
                assert 'data-production-restarts="3"' in page.text
                assert 'data-production-records ' not in page.text
            assert observed['restarts'] <= 3
        assert paused
        assert observed['restarts'] == 3
        link = reload_link(page)
        assert case.client.get(link).status_code == 200
        before = observed['restarts']
        for number in range(32, 64):
            if number % 2 == 0:
                change_input(case, number)
            page = request_list(case)
            assert observed['restarts'] - before <= 3
        assert PAUSED in page.text
        assert 'data-production-restarts="3"' in page.text
        replay = case.client.get(link)
        assert replay.status_code == 200
        assert PAUSED in replay.text
