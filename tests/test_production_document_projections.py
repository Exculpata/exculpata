"""Set-based production preparation retains single-document semantics."""
import pytest

from case_intelligence.modules.discovery_review.production import ProductionService
from tests.test_discovery_review_dat_opt_storage import app_case, finish_import


@pytest.mark.parametrize('profile', ['clean', 'family', 'malformed'])
def test_document_projections_equal_single_document_projection(tmp_path, profile):
    with app_case(tmp_path, profile=profile, count=12, width=512) as case:
        finish_import(case)
        mid = case.matter.matter_id
        pairs = [(d.document_id, d.version_id) for d in case.store.documents.values()]
        for start in range(0, len(pairs), 12):
            selected = pairs[start:start + 12]
            expected = tuple(case.service.document_projection(mid, *pair) for pair in selected)
            changes = case.workspace.connection.total_changes
            assert case.service.document_projections(mid, selected) == expected
            assert case.workspace.connection.total_changes == changes
        assert case.service.document_projections(mid, []) == ()
        with pytest.raises(KeyError):
            case.service.document_projections(mid, [(pairs[0][0], 'wrong-version')])
        with pytest.raises(ValueError):
            case.service.document_projections(mid, pairs * 2)


@pytest.mark.parametrize('replacement_service', [False, True])
def test_completed_projection_survives_cancel_and_rebind(tmp_path, replacement_service):
    with app_case(tmp_path, count=4, width=128) as case:
        attempt = finish_import(case)
        mid, service = case.matter.matter_id, case.service
        pairs = [(d.document_id, d.version_id) for d in case.store.documents.values()]
        expected = tuple(service.document_projection(mid, *pair) for pair in pairs)
        assert all(row['state'] == 'complete' for row in expected)
        if replacement_service:
            service.cancel(mid)
            service = ProductionService(case.context, repository=case.repository)
            service.bind(case.matter, case.store, case.definition)
            scanning = service.reconcile_attempt(mid, attempt)
            assert scanning['status'] == 'building'
            assert scanning['dependencies']['was_complete']
            assert scanning['dependencies']['state'] == 'scan'
            pending = tuple(service.document_projection(mid, *pair) for pair in pairs)
            assert all(row['state'] == 'pending' for row in pending)
            assert service.document_projections(mid, pairs) == pending
            assert service.advance(mid, max_steps=128)['state'] == 'complete'
            attempt = case.repository.attempt(mid, attempt['attempt_id'])
            assert attempt['dependencies']['was_complete']
            assert tuple(service.document_projection(mid, *pair) for pair in pairs) == expected
        stamp = case.repository.stamp(mid)
        service.cancel(mid)
        service.bind(case.matter, case.store, case.definition)
        assert service.advance(mid, max_steps=128)['state'] == 'complete'
        assert case.repository.stamp(mid) == stamp
        attempt = case.repository.attempt(mid, attempt['attempt_id'])
        assert attempt['status'] == 'complete'
        assert attempt['dependencies']['owner'] == service.epoch
        assert service.binding(mid)['validated'] == {}
        assert service.attempt_current(mid, attempt)
        assert tuple(service.document_projection(mid, *pair) for pair in pairs) == expected
        changes = case.workspace.connection.total_changes
        assert service.document_projections(mid, pairs) == expected
        assert case.workspace.connection.total_changes == changes


@pytest.mark.parametrize('state', [
    'foreign_unvalidated', 'foreign_validated', 'foreign_stale_validation', 'paused', 'pending_revision',
])
def test_projection_dependency_owner_and_recovery_states(tmp_path, state):
    with app_case(tmp_path, count=4, width=128) as case:
        attempt = finish_import(case)
        mid, service = case.matter.matter_id, case.service
        binding = service.binding(mid)
        dependencies = dict(attempt['dependencies'])
        revision = attempt['input_revision']
        binding['validated'].clear()
        if state.startswith('foreign_'):
            dependencies['owner'] = 'synthetic-other-service'
            if state != 'foreign_unvalidated':
                binding['validated'][attempt['attempt_id']] = service.dependency_stamp(dependencies)
            if state == 'foreign_stale_validation':
                dependencies['hash'] = 'synthetic-changed-dependencies'
        elif state == 'paused':
            dependencies.update(paused=True, restarts=3, reload='synthetic-reload-token', was_complete=True)
            case.repository.update_attempt(mid, attempt['attempt_id'], status='building', phase='paused')
        else:
            revision += ':pending'
        case.repository.save_dependencies(mid, attempt['attempt_id'], dependencies, revision)
        pairs = [(d.document_id, d.version_id) for d in case.store.documents.values()]
        expected = tuple(service.document_projection(mid, *pair) for pair in pairs)
        own = expected[next(i for i, pair in enumerate(pairs) if pair[0] == attempt['document_id'])]
        if state == 'paused':
            assert own['state'] == 'paused' and own['restarts'] == 3
            assert own['reload'] == 'synthetic-reload-token'
        else:
            assert own['state'] == ('complete' if state == 'foreign_validated' else 'pending')
        changes = case.workspace.connection.total_changes
        assert service.document_projections(mid, pairs) == expected
        assert case.workspace.connection.total_changes == changes


from tests.fixture_write import fixture_write


@pytest.mark.parametrize('branch', [
    'not_discovered', 'own_building', 'own_failed', 'own_complete_failure', 'own_old_parser',
    'not_current', 'generation_mismatch', 'association_conflict', 'pending_status',
    'no_rows', 'no_rows_conflicted', 'missing_parent', 'load_file_without_attempt',
])
def test_projection_status_and_diagnostic_branches(tmp_path, branch):
    with app_case(tmp_path, profile='family', count=4, width=128) as case:
        attempt = finish_import(case)
        mid = case.matter.matter_id
        if branch == 'not_discovered':
            case.service.discovery_done.discard(mid)
        else:
            with fixture_write(case.workspace) as connection:
                if branch.startswith('own_'):
                    changes = {
                        'own_building': ('building', '', attempt['parser_revision']),
                        'own_failed': ('failed', 'Synthetic file failure', attempt['parser_revision']),
                        'own_complete_failure': ('complete', 'Synthetic retained diagnostic', attempt['parser_revision']),
                        'own_old_parser': ('complete', '', 'synthetic-older-parser'),
                    }
                    status, failure, parser = changes[branch]
                    connection.execute('UPDATE workbench_discovery_production SET status=?,file_failure=?,parser_revision=? '
                        'WHERE matter_id=? AND attempt_id=?', (status, failure, parser, mid, attempt['attempt_id']))
                elif branch == 'not_current':
                    connection.execute('UPDATE workbench_discovery_production SET input_revision=? WHERE matter_id=?',
                        ('synthetic-old-input', mid))
                elif branch == 'generation_mismatch':
                    connection.execute('UPDATE workbench_discovery_dat_record SET generation=? WHERE matter_id=?',
                        ('synthetic-other-generation', mid))
                elif branch == 'association_conflict':
                    connection.execute('UPDATE workbench_discovery_dat_record SET association_status=? WHERE matter_id=?',
                        ('conflicting_dat_rows', mid))
                elif branch == 'pending_status':
                    connection.execute('UPDATE workbench_discovery_production SET status=? WHERE matter_id=?', ('building', mid))
                elif branch in ('no_rows', 'no_rows_conflicted'):
                    connection.execute('UPDATE workbench_discovery_dat_record SET document_id=? WHERE matter_id=?', ('', mid))
                    if branch == 'no_rows_conflicted':
                        connection.execute('UPDATE workbench_discovery_production SET status=?,source_format=? WHERE matter_id=?',
                            ('failed', 'csv', mid))
                elif branch == 'load_file_without_attempt':
                    connection.execute('UPDATE workbench_discovery_production SET status=? WHERE matter_id=?', ('stale', mid))
                elif branch == 'missing_parent':
                    connection.execute('UPDATE workbench_discovery_dat_record SET family_root=? WHERE matter_id=?',
                        ('synthetic-missing-parent', mid))
        pairs = [(d.document_id, d.version_id) for d in case.store.documents.values()]
        expected = tuple(case.service.document_projection(mid, *pair) for pair in pairs)
        assert case.service.document_projections(mid, pairs) == expected
        if branch == 'own_failed':
            own = expected[next(i for i, pair in enumerate(pairs) if pair[0] == attempt['document_id'])]
            assert own['state'] == 'error' and own['diagnostics'] == ('Synthetic file failure',)


from tests.test_discovery_review_dat_opt import (
    review as review, small_page_production, upload_production, import_paths, dat_bytes, source_text,
)


def test_batch_preserves_page_reference(review):
    small_page_production(review)
    mid = review.matter.matter_id
    source = review.document('Generated/native.txt')
    expected = review.service.document_projection(mid, source.document_id, source.version_id)
    assert [reference['category'] for reference in expected['references']] == ['document', 'page']
    assert review.service.document_projections(mid, [(source.document_id, source.version_id)]) == (expected,)


def test_batch_preserves_per_document_format_priority(review):
    upload_production(review, {
        'Generated/native.txt': source_text(1),
        'Generated/load.csv': b'BEGBATES,NATIVEPATH,CSV only\nSYN0001,Generated/native.txt,legacy retained\n',
        'Generated/load.dat': dat_bytes(('BEGBATES', 'NATIVEPATH', 'DAT only'),
            [('SYN0001', 'Generated/native.txt', 'retained')]),
    })
    import_paths(review, ['Generated/load.csv', 'Generated/load.dat'])
    mid = review.matter.matter_id
    pairs = [(d.document_id, d.version_id) for d in review.store.documents.values()]
    expected = tuple(review.service.document_projection(mid, *pair) for pair in pairs)
    assert review.service.document_projections(mid, pairs) == expected
    native = expected[next(i for i, pair in enumerate(pairs) if pair[0] == review.document('Generated/native.txt').document_id)]
    assert ('DAT only', 'retained') in tuple(native['raw_fields'])


def test_provenance_ambiguity_is_limited_per_document(tmp_path, monkeypatch):
    from tests.test_discovery_review_dat_opt_storage import _receipt
    with app_case(tmp_path, profile='clean', count=4, width=128) as case:
        finish_import(case)
        documents = list(case.store.documents.values())
        _receipt(case.workspace, case.matter, documents[1:] + documents[:1])
        _receipt(case.workspace, case.matter, documents[2:] + documents[:2])
        mid = case.matter.matter_id
        pairs = [(d.document_id, d.version_id) for d in documents]
        assert all(case.repository.provenance(mid, *pair) == '' for pair in pairs)
        expected = tuple(case.service.document_projection(mid, *pair) for pair in pairs)
        reads = []
        original = case.repository.projection_rows

        def rows(statement, parameters, **bounds):
            result = original(statement, parameters, **bounds)
            if statement.startswith('WITH proofs'):
                counts = {}
                for row in result:
                    counts[row['document_id']] = counts.get(row['document_id'], 0) + 1
                assert all(count == 2 for count in counts.values())
                assert len(result) * bounds['constituents'] <= 24
                reads.extend(counts)
            return result

        monkeypatch.setattr(case.repository, 'projection_rows', rows)
        assert case.service.document_projections(mid, pairs) == expected
        assert set(reads) == {d.document_id for d in documents}


def test_generation_mismatch_in_selected_record_remains_pending(tmp_path, monkeypatch):
    with app_case(tmp_path, profile='clean', count=4, width=128) as case:
        finish_import(case)
        mid = case.matter.matter_id
        single = case.repository.document_records
        batch = case.repository.projection_rows

        def records(*args, **kwargs):
            return tuple({**row, 'generation': 'synthetic-changed-generation'} for row in single(*args, **kwargs))

        def rows(statement, parameters, **kwargs):
            found = batch(statement, parameters, **kwargs)
            if statement.startswith('SELECT * FROM (SELECT ROW_NUMBER()') and 'workbench_discovery_dat_record r' in statement:
                return tuple({**row, 'generation': 'synthetic-changed-generation'} for row in found)
            return found

        monkeypatch.setattr(case.repository, 'document_records', records)
        monkeypatch.setattr(case.repository, 'projection_rows', rows)
        pairs = [(d.document_id, d.version_id) for d in case.store.documents.values() if d.display_name.endswith('.txt')]
        expected = tuple(case.service.document_projection(mid, *pair) for pair in pairs)
        assert all(row['state'] == 'pending' and row['references'] for row in expected)
        assert case.service.document_projections(mid, pairs) == expected
