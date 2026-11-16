"""Synthetic production-list currentness and bounded recovery regressions."""
import inspect

from tests.test_discovery_review_dat_opt_storage import app_case
from tests.test_discovery_review_index import service_for


PENDING = 'Indexing, reload shortly.'
PAUSED = 'Indexing paused: the sources keep changing. Reload to resume.'


def test_building_attempt_list_is_pending_then_converges(tmp_path, monkeypatch):
    with app_case(tmp_path, count=20, width=512) as case:
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
        page = case.client.get(url, params=params)
        assert page.status_code == 200
        attempt = production.repository.current_attempt(case.matter.matter_id, source.document_id)
        assert attempt['status'] == 'complete'
        assert production.attempt_current(case.matter.matter_id, attempt)
        assert 'data-production-records ' in page.text


def request_list(case, **extra):
    source = case.store.documents['00000000000000000000000000000001']
    page = case.client.get(f'/matters/{case.matter.slug}/production', params={
        'doc': case.store.action_token(source), 'view': 'fields', **extra})
    assert page.status_code == 200
    attempt = current_attempt(case)
    if attempt and attempt['status'] == 'building':
        assert 'data-production-records ' not in page.text
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
    seen = {}

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
        key = (matter_id, source.document_id)
        previous = seen.get(key, prior['attempt_id'] if prior else result)
        if result != previous:
            observed['restarts'] += 1
        seen[key] = result
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


def test_stale_record_cursor_still_refuses_previous_attempt(tmp_path, monkeypatch):
    with app_case(tmp_path, count=3) as case:
        from case_intelligence.modules.discovery_review.production.service import source_from_attempt
        attempt = converge(case)
        production = service_for(case.bench).production
        native = case.store.get('00000000000000000000000000000002')
        production.repository.restart_matching(case.matter.matter_id, attempt['attempt_id'], attempt['target'])
        assert production.repository.attempt(case.matter.matter_id, attempt['attempt_id'])['generation'] != attempt['generation']
        with monkeypatch.context() as held:
            held.setattr(production, 'match_step', lambda *args: None)
            page = case.client.get(f'/matters/{case.matter.slug}/production', params={
                'doc': case.store.action_token(native), 'view': 'fields',
                'record': f"dat:{attempt['attempt_id']}:1"})
            assert page.status_code == 409
            assert 'These production records changed. Reload the list.' in page.text
            assert 'data-production-records ' not in page.text
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


def test_legacy_revision_revalidates_once(tmp_path, monkeypatch):
    with app_case(tmp_path, count=3) as case:
        attempt = converge(case)
        production = service_for(case.bench).production
        observed = observe_work(case, monkeypatch)
        old_revision = f"{case.store.source_changes.epoch}:{case.store.revision}:{production.binding(case.matter.matter_id)['human']}"
        production.repository.update_attempt(case.matter.matter_id, attempt['attempt_id'],
            status='complete', phase='complete', input_revision=old_revision)
        refreshed = converge(case)
        assert refreshed['attempt_id'] == attempt['attempt_id']
        assert refreshed['generation'] != attempt['generation']
        assert observed['restarts'] == 1
        converge(case)
        assert observed['restarts'] == 1


def test_source_version_replacement_keeps_the_lineage_limit(tmp_path, monkeypatch):
    from dataclasses import replace
    from tests.fixture_write import fixture_write
    with app_case(tmp_path, count=80) as case:
        observed = observe_work(case, monkeypatch)
        request_list(case)
        source_id = '00000000000000000000000000000001'
        for number in range(10):
            with case.store.mutation_guard():
                source = case.store.get(source_id)
                version = f'{number + 1000:032x}'
                with fixture_write(case.workspace) as connection:
                    connection.execute('UPDATE workbench_intake_transfer SET source_version_id=? '
                        'WHERE matter_id=? AND source_version_id=?',
                        (version, case.matter.matter_id, source.version_id))
                case.store.documents[source_id] = replace(source, version_id=version)
                case.store._save((source_id,))
            page = request_list(case)
            assert observed['restarts'] <= 3
        assert observed['restarts'] == 3
        assert PAUSED in page.text
        assert 'data-production-restarts="3"' in page.text
        assert case.client.get(reload_link(page)).status_code == 200
        converge(case)


def test_other_production_root_does_not_consume_restarts(tmp_path, monkeypatch):
    from dataclasses import replace
    from tests.fixture_write import fixture_write
    with app_case(tmp_path, count=80) as case:
        document_id = '00000000000000000000000000000051'
        source = case.store.get(document_id)
        with case.store.mutation_guard():
            with fixture_write(case.workspace) as connection:
                connection.execute('UPDATE workbench_intake_item SET relative_path=? '
                    'WHERE matter_id=? AND relative_path=?',
                    ('Other/unrelated.txt', case.matter.matter_id, source.relative_path))
                connection.execute('UPDATE workbench_upload_item SET relative_path=? '
                    'WHERE matter_id=? AND document_id=?',
                    ('Other/unrelated.txt', case.matter.matter_id, document_id))
            case.store.documents[document_id] = replace(source, relative_path='Other/unrelated.txt')
            case.store._save((document_id,))
        observed = observe_work(case, monkeypatch)
        request_list(case)
        original = current_attempt(case)
        for number in range(10):
            with case.store.mutation_guard():
                source = case.store.get(document_id)
                case.store.documents[document_id] = replace(source, display_name=f'Generated unrelated {number}.txt')
                case.store._save((document_id,))
            page = request_list(case)
            assert PAUSED not in page.text
            assert current_attempt(case)['dependencies']['restarts'] == 0
        completed = converge(case)
        assert completed['generation'] == original['generation']
        assert observed['restarts'] == 0


def test_failed_source_keeps_its_failure_during_input_changes(tmp_path):
    with app_case(tmp_path, count=3) as case:
        attempt = converge(case)
        service_for(case.bench).production.repository.update_attempt(case.matter.matter_id, attempt['attempt_id'],
            status='failed', phase='error', failure='Generated incomplete source')
        failed = current_attempt(case)
        assert failed['status'] == 'failed'
        assert failed['file_failure']
        for number in range(6):
            change_input(case, number)
            page = request_list(case)
            assert PAUSED not in page.text
            assert current_attempt(case)['status'] == 'failed'
            assert current_attempt(case)['file_failure'] == failed['file_failure']


def test_changes_interrupting_revalidation_of_complete_attempt_are_bounded(tmp_path, monkeypatch):
    with app_case(tmp_path, count=20) as case:
        converge(case)
        production = service_for(case.bench).production
        reconcile = production.reconcile_attempt
        interruptions, scans = [], []
        armed = False

        def interrupted(matter_id, attempt):
            nonlocal armed
            result = reconcile(matter_id, attempt)
            before, after = attempt['dependencies'], result['dependencies']
            if after.get('state') == 'scan' and after.get('revision') != before.get('revision'):
                scans.append(result['attempt_id'])
            if armed and after.get('state') == 'scan' and not after.get('paused'):
                armed = False
                interruptions.append(result['attempt_id'])
                change_input(case, len(interruptions))
            return result

        monkeypatch.setattr(production, 'reconcile_attempt', interrupted)
        change_input(case, 0)
        for _ in range(10):
            armed = True
            page = request_list(case)
            assert len(scans) <= 3
        assert len(scans) == 3
        assert PAUSED in page.text
        assert 'data-production-restarts="3"' in page.text
