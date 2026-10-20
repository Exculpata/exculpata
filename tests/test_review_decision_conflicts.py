"""Synthetic shared human-validation edits retain separate machine results."""

from tests.fixture_write import fixture_write
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading

import pytest

from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore
from tests.test_research_and_full_review import ACTOR, _store

MEMBER = 'generated-review-member'


def _seed(tmp_path, *, pending=False):
    store, matter = _store(tmp_path, count=2)
    store.upsert_principal('test', MEMBER, 'Generated Member', MEMBER, preferred_principal_id=MEMBER)
    store.add_member(matter.matter_id, MEMBER, ACTOR)
    criterion, version = store.create_review_criterion(matter.matter_id, ACTOR,
        title='Generated team criterion', instructions='Include the generated topic.')
    run = store.queue_review_run(matter.matter_id, ACTOR, version.criterion_version_id, run_kind='full')
    store.claim_review_run('generated-worker')
    items = store.review_decisions_for_export(matter.matter_id, ACTOR, run.run_id)
    if not pending:
        for item in items:
            store.record_review_decision(run.run_id, item.document_id, decision='included', rationale='Generated original machine rationale')
        store.finish_review_run(run.run_id)
    return store, matter, run, store.review_decisions_for_export(matter.matter_id, ACTOR, run.run_id)


def _save(store, matter, run, shown, *, actor=ACTOR, decision='agree', note='Reviewed generated note'):
    return store.adjudicate_review_decision(matter.matter_id, actor, run.run_id, shown.document_id,
        human_decision=decision, note=note, expected_updated_at=shown.updated_at)


def _machine(item):
    return (item.machine_decision, item.rationale, item.citations, item.error_message,
            item.source_version_id, item.source_basis_digest, item.ordinal, item.validation_sample)


def test_stale_decision_preserves_teammate_work_and_original_machine_result(tmp_path):
    first, matter, run, items = _seed(tmp_path)
    second = WorkspaceStore(first.path)
    try:
        saved = _save(first, matter, run, items[0], decision='include', note='Owner current review')
        with pytest.raises(WorkspaceProblem, match='changed'):
            _save(second, matter, run, items[0], actor=MEMBER, decision='exclude', note='Older form note')
        current = first.review_decision(matter.matter_id, ACTOR, run.run_id, items[0].document_id)
        assert current.human_note == saved.human_note and current.reviewed_by == ACTOR
        assert _machine(current) == _machine(items[0])
        # Another source can be reviewed independently using its original form.
        other = _save(second, matter, run, items[1], actor=MEMBER, decision='uncertain', note='Independent source review')
        assert other.reviewed_by == MEMBER and other.human_decision == 'uncertain'
        assert _machine(other) == _machine(items[1])
        resolved = _save(second, matter, run, current, actor=MEMBER, decision='include', note='Combined reviewed note')
        assert resolved.human_note == 'Combined reviewed note' and resolved.reviewed_by == MEMBER
        metrics = first.review_validation_metrics(matter.matter_id, ACTOR, run.run_id)
        assert metrics['reviewed_total'] == 2 and metrics['uncertain_total'] == 1
    finally:
        second.close(); first.close()


def test_simultaneous_reviewers_admit_only_one_displayed_revision(tmp_path):
    first, matter, run, items = _seed(tmp_path)
    second = WorkspaceStore(first.path)
    barrier = threading.Barrier(2)
    def save(store, actor):
        barrier.wait(timeout=5)
        try:
            return _save(store, matter, run, items[0], actor=actor).reviewed_by
        except WorkspaceProblem:
            return 'conflict'
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(save, first, ACTOR), pool.submit(save, second, MEMBER)]
            outcomes = [future.result(timeout=10) for future in futures]
        assert outcomes.count('conflict') == 1
        assert first.review_decision(matter.matter_id, ACTOR, run.run_id, items[0].document_id).reviewed_by in outcomes
    finally:
        second.close(); first.close()


def test_repeated_clock_and_restored_text_do_not_restore_old_revision(tmp_path):
    store, matter, run, items = _seed(tmp_path)
    store._clock = lambda: datetime(2020, 1, 1, tzinfo=timezone.utc)
    first = _save(store, matter, run, items[0], note='Initial note')
    second = _save(store, matter, run, first, note='Intermediate note')
    third = _save(store, matter, run, second, note='Initial note')
    assert datetime.fromisoformat(third.updated_at) > datetime.fromisoformat(second.updated_at) > datetime.fromisoformat(first.updated_at)
    with pytest.raises(WorkspaceProblem, match='changed'):
        _save(store, matter, run, first)
    store.close()


@pytest.mark.parametrize('denial', ['revoked', 'inactive', 'closing', 'other_matter'])
def test_validation_rechecks_current_authority_and_exact_matter(tmp_path, denial):
    store, matter, run, items = _seed(tmp_path)
    if denial == 'other_matter':
        matter = store.create_matter('Generated other matter canary', '', MEMBER)
    else:
        with fixture_write(store):
            if denial == 'revoked':
                store.connection.execute("UPDATE workbench_matter_membership SET state='revoked' WHERE principal_id=?", (MEMBER,))
            elif denial == 'inactive':
                store.connection.execute('UPDATE workbench_principal SET active=0 WHERE principal_id=?', (MEMBER,))
            else:
                store.connection.execute("UPDATE workbench_matter_lifecycle SET state='purging' WHERE matter_id=?", (matter.matter_id,))
    with pytest.raises(KeyError):
        _save(store, matter, run, items[0], actor=MEMBER)
    store.close()


def test_pending_decision_cannot_be_validated_or_used_after_machine_completion(tmp_path):
    store, matter, run, items = _seed(tmp_path, pending=True)
    with pytest.raises(WorkspaceProblem, match='pending'):
        _save(store, matter, run, items[0])
    store.record_review_decision(run.run_id, items[0].document_id, decision='included', rationale='Generated original machine rationale')
    with pytest.raises(WorkspaceProblem, match='changed'):
        _save(store, matter, run, items[0])
    store.close()


def test_machine_completion_and_source_invalidation_advance_a_repeated_clock(tmp_path):
    store, matter, run, items = _seed(tmp_path, pending=True)
    store._clock = lambda: datetime(2020, 1, 1, tzinfo=timezone.utc)
    store.record_review_decision(run.run_id, items[0].document_id, decision='included', rationale='Original result')
    shown = store.review_decision(matter.matter_id, ACTOR, run.run_id, items[0].document_id)
    assert datetime.fromisoformat(shown.updated_at) > datetime.fromisoformat(items[0].updated_at)
    store.mark_review_decision_source_changed(run.run_id, items[0].document_id)
    changed = store.review_decision(matter.matter_id, ACTOR, run.run_id, items[0].document_id)
    assert datetime.fromisoformat(changed.updated_at) > datetime.fromisoformat(shown.updated_at)
    with pytest.raises(WorkspaceProblem, match='changed'):
        _save(store, matter, run, shown)
    assert changed.human_decision == ''
    store.close()


def test_http_retains_choice_note_attribution_and_current_machine_support(tmp_path):
    from fastapi.testclient import TestClient
    from tests.test_matter_management import OWNER, OTHER, _app, _create_matter, _csrf, _headers, _principal_id, _seed_completed_owner_exports
    app = _app(tmp_path)
    with TestClient(app, base_url='https://testserver') as owner:
        csrf = _csrf(owner.get('/matters/new', headers=_headers(OWNER)).text)
        slug = _create_matter(owner, principal=OWNER, csrf_token=csrf, name='Generated shared validation forms')
        bench = app.state.workbench
        actor = _principal_id(owner, OWNER)
        matter = bench.workspace.get_matter(slug, actor)
        _, _, run = _seed_completed_owner_exports(bench, matter, actor)
        shown = bench.workspace.review_decisions_for_export(matter.matter_id, actor, run.run_id)[0]
        saved = bench.workspace.adjudicate_review_decision(matter.matter_id, actor, run.run_id, shown.document_id,
            human_decision='include', note='Saved current review note', expected_updated_at=shown.updated_at)
        path = f'/matters/{slug}/full-review/{run.run_id}/decisions/{shown.document_id}'
        draft = dict(csrf_token=csrf, expected_updated_at=shown.updated_at, human_decision='uncertain', note='<Unsaved review note>')
        assert owner.post(path, data={**draft, 'csrf_token':'wrong'}, headers=_headers(OWNER)).status_code == 403
        peer = TestClient(app, base_url='https://testserver')
        peer_csrf = _csrf(peer.get('/matters/new', headers=_headers(OTHER)).text)
        denied = peer.post(path, data={**draft, 'csrf_token': peer_csrf}, headers=_headers(OTHER))
        peer.close()
        assert denied.status_code == 404 and 'Saved current review note' not in denied.text
        conflict = owner.post(path, data=draft, headers=_headers(OWNER))
        assert conflict.status_code == 409 and conflict.headers['cache-control'] == 'no-store'
        assert 'Saved current review note' in conflict.text and '&lt;Unsaved review note&gt;' in conflict.text
        assert 'value="uncertain" checked' in conflict.text and f'value="{saved.updated_at}"' in conflict.text
        assert shown.rationale in conflict.text and 'Saved by' in conflict.text
        missing = owner.post(path, data={**draft, 'expected_updated_at':''}, headers=_headers(OWNER))
        assert missing.status_code == 409 and '&lt;Unsaved review note&gt;' in missing.text
        invalid = owner.post(path, data={**draft, 'expected_updated_at':saved.updated_at, 'human_decision':'invalid'}, headers=_headers(OWNER))
        assert invalid.status_code == 400 and '&lt;Unsaved review note&gt;' in invalid.text
        accepted = owner.post(path, data={**draft,'expected_updated_at':saved.updated_at}, headers=_headers(OWNER), follow_redirects=False)
        assert accepted.status_code == 303
        current = bench.workspace.review_decision(matter.matter_id, actor, run.run_id, shown.document_id)
        assert current.human_note == '<Unsaved review note>' and _machine(current) == _machine(shown)
        with fixture_write(bench.workspace):
            bench.workspace.connection.execute('DELETE FROM workbench_review_decision WHERE run_id=? AND document_id=?',(run.run_id,shown.document_id))
        gone = owner.post(path, data=draft, headers=_headers(OWNER))
        assert gone.status_code == 409 and 'no longer available' in gone.text and '&lt;Unsaved review note&gt;' in gone.text
        assert 'Save validation</button>' not in gone.text


def test_accepted_human_review_and_revision_survive_reopen(tmp_path):
    store, matter, run, items = _seed(tmp_path)
    saved = _save(store, matter, run, items[0], actor=MEMBER, note='Accepted before restart')
    path = store.path
    store.close()
    reopened = WorkspaceStore(path)
    try:
        current = reopened.review_decision(matter.matter_id, ACTOR, run.run_id, items[0].document_id)
        assert current == saved and _machine(current) == _machine(items[0])
        with pytest.raises(WorkspaceProblem, match='changed'):
            _save(reopened, matter, run, items[0])
    finally:
        reopened.close()
