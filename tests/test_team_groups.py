"""Synthetic live group authorization and storage recovery contracts."""

from tests.fixture_write import fixture_write
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from tests.case_lifecycle_helpers import claim_synthetic_exit

from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore


def seed(path):
    store = WorkspaceStore(path)
    for name in ('principal-owner', 'principal-reviewer', 'principal-other', 'principal-administrator'):
        store.upsert_principal('test', name, name.title(), name, preferred_principal_id=name)
    a = store.create_matter('Synthetic Alpha', '', 'principal-owner')
    b = store.create_matter('Synthetic Beta', '', 'principal-owner')
    first = store.create_team_group('First team', 'principal-administrator', administrator_override=True)
    second = store.create_team_group('Second team', 'principal-administrator', administrator_override=True)
    store.set_team_group_member(first, 'principal-reviewer', 'principal-administrator', present=True, administrator_override=True)
    store.set_team_group_member(second, 'principal-other', 'principal-administrator', present=True, administrator_override=True)
    store.set_matter_group_grant(a.matter_id, first, 'principal-owner', present=True)
    store.set_matter_group_grant(b.matter_id, second, 'principal-owner', present=True)
    return store, a, b, first, second


def test_two_groups_mixed_grants_and_last_revocation(tmp_path):
    store, a, b, first, second = seed(tmp_path / 'control.sqlite')
    assert store.list_matters('principal-reviewer') == (a,)
    assert store.list_matters('principal-other') == (b,)
    assert store.list_matters('principal-administrator') == ()
    assert store.access_reasons(a.matter_id, 'principal-owner') == ('Owner',)
    assert store.access_reasons(a.matter_id, 'principal-reviewer') == ('Group: First team',)
    store.add_member(a.matter_id, 'principal-reviewer', 'principal-owner')
    store.set_team_group_member(second, 'principal-reviewer', 'principal-administrator', present=True, administrator_override=True)
    store.set_matter_group_grant(a.matter_id, second, 'principal-owner', present=True)
    assert len(store.members(a.matter_id)) == 3
    assert store.access_reasons(a.matter_id, 'principal-reviewer') == ('Direct member', 'Group: First team', 'Group: Second team')
    store.set_matter_group_grant(a.matter_id, first, 'principal-owner', present=False)
    assert store.membership(a.matter_id, 'principal-reviewer').role == 'member'
    store.revoke_member(a.matter_id, 'principal-reviewer', 'principal-owner')
    assert store.access_reasons(a.matter_id, 'principal-reviewer') == ('Group: Second team',)
    store.set_team_group_member(second, 'principal-reviewer', 'principal-administrator', present=False, administrator_override=True)
    with pytest.raises(KeyError):
        store.membership(a.matter_id, 'principal-reviewer')
    with pytest.raises(KeyError):
        store.membership(b.matter_id, 'principal-reviewer')
    assert store.membership(b.matter_id, 'principal-other')
    store.close()


def test_disabled_principal_and_live_account_eligibility(tmp_path):
    store, a, _, first, _ = seed(tmp_path / 'control.sqlite')
    with fixture_write(store):
        store.connection.execute("UPDATE workbench_principal SET active=0 WHERE principal_id='principal-reviewer'")
    assert store.list_matters('principal-reviewer') == ()
    with pytest.raises(WorkspaceProblem):
        store.set_team_group_member(first, 'principal-reviewer', 'principal-administrator', present=True, administrator_override=True)
    with fixture_write(store):
        store.connection.execute("UPDATE workbench_principal SET active=1 WHERE principal_id='principal-reviewer'")
    enabled = {'principal-reviewer', 'principal-owner', 'principal-administrator', 'principal-other'}
    store.principal_enabled = lambda provider, subject: subject in enabled
    enabled.remove('principal-reviewer')
    with pytest.raises(KeyError):
        store.source_catalog_for_export(a.matter_id, 'principal-reviewer')
    enabled.add('principal-reviewer')
    assert store.membership(a.matter_id, 'principal-reviewer')
    store.close()


def test_group_writes_authority_uniqueness_attribution_and_concurrency(tmp_path):
    path = tmp_path / 'control.sqlite'
    store, a, _, first, _ = seed(path)
    with pytest.raises(WorkspaceProblem):
        store.create_team_group('Unauthorized', 'principal-reviewer')
    with pytest.raises(WorkspaceProblem):
        store.create_team_group('FIRST TEAM', 'principal-administrator', administrator_override=True)
    with pytest.raises(WorkspaceProblem):
        store.set_matter_group_grant(a.matter_id, first, 'principal-reviewer', present=False)
    other = WorkspaceStore(path)
    def grant(connection):
        connection.set_matter_group_grant(a.matter_id, first, 'principal-owner', present=True)
    with ThreadPoolExecutor(2) as pool:
        pending = [pool.submit(grant, workspace) for workspace in (store, other)]
        for future in pending:
            future.result()
    assert len(store.matter_group_grants(a.matter_id)) == 1
    events = [e for e in store.audit_events(a.matter_id) if e.action == 'membership.group_add']
    assert len(events) == 1 and events[0].actor_principal_id == 'principal-owner' and events[0].object_id == first
    assert any(e.action == 'team_group.member_add' and e.details['principal_id'] == 'principal-reviewer'
               and e.actor_principal_id == 'principal-administrator' for e in store.audit_events())
    other.close()
    store.close()


def test_mirrored_migration_backup_clean_restore(tmp_path):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    name = 'migrations/sqlite/0031_team_groups.sql'
    assert (root / name).read_bytes() == (root / 'src/case_intelligence' / name).read_bytes()
    store, a, b, first, _ = seed(tmp_path / 'original.sqlite')
    store.add_member(a.matter_id, 'principal-reviewer', 'principal-owner')
    backup = tmp_path / 'backup.sqlite'
    with sqlite3.connect(backup) as target:
        with store._lock:
            store.connection.backup(target)
    store.set_team_group_member(first, 'principal-reviewer', 'principal-administrator', present=False, administrator_override=True)
    clean = tmp_path / 'clean'
    clean.mkdir()
    with sqlite3.connect(backup) as source, sqlite3.connect(clean / 'control.sqlite') as target:
        source.backup(target)
    restored = WorkspaceStore(clean / 'control.sqlite')
    assert restored.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert restored.connection.execute('PRAGMA foreign_key_check').fetchall() == []
    assert restored.access_reasons(a.matter_id, 'principal-reviewer') == ('Direct member', 'Group: First team')
    assert restored.list_matters('principal-other') == (b,)
    assert len(restored.audit_events()) == len(store.audit_events()) - 1
    restored.close()
    store.close()


def test_queued_answer_group_revocation(tmp_path):
    from case_intelligence.answer_jobs import AnswerJobFailure
    from case_intelligence.generation import UnavailableGenerator
    from case_intelligence.workbench import CaseIntelligenceWorkbench
    bench = CaseIntelligenceWorkbench(tmp_path / 'runtime', generator=UnavailableGenerator(), answer_workers=1)
    bench.answers.close()
    bench.answers = None
    store = bench.workspace
    for name in ('principal-owner', 'principal-reviewer'):
        store.upsert_principal('test', name, name.title(), name, preferred_principal_id=name)
    matter = bench.create_matter('Synthetic queue revocation', '', 'principal-owner')
    group = store.create_team_group('Review team', 'principal-owner', administrator_override=True)
    store.set_team_group_member(group, 'principal-reviewer', 'principal-owner', present=True, administrator_override=True)
    store.set_matter_group_grant(matter.matter_id, group, 'principal-owner', present=True)
    conversation = store.get_conversation(matter.matter_id)
    job, _ = store.queue_answer_job(matter.matter_id, conversation.conversation_id, 'principal-reviewer',
                                   'What is supported?', 'answer-request-' + 'a' * 32)
    store.set_team_group_member(group, 'principal-reviewer', 'principal-owner', present=False, administrator_override=True)
    with pytest.raises(AnswerJobFailure, match='Access to this matter was removed'):
        bench._process_answer_job(job, lambda *_: None, lambda: False)
    with pytest.raises(KeyError):
        store.source_catalog_for_export(matter.matter_id, 'principal-reviewer')
    bench.close()


def test_real_local_disable_and_removed_account_fence_store_without_browser_request(tmp_path):
    from fastapi.testclient import TestClient
    from tests.test_browser_local_accounts import configured_app, login, create, ORIGIN
    app, repo = configured_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin)
        create(admin, 'synthetic.reviewer')
        reviewer = TestClient(app, base_url=ORIGIN)
        login(reviewer, 'synthetic.reviewer')
        store = app.state.workbench.workspace
        owner = next(p for p in store.active_principals() if p.provider_subject == 'alice.admin')
        person = next(p for p in store.active_principals() if p.provider_subject == 'synthetic.reviewer')
        matter = store.create_matter('Synthetic account fence', '', owner.principal_id)
        group = store.create_team_group('Local reviewers', owner.principal_id, administrator_override=True)
        store.set_team_group_member(group, person.principal_id, owner.principal_id, present=True, administrator_override=True)
        store.set_matter_group_grant(matter.matter_id, group, owner.principal_id, present=True)
        assert store.membership(matter.matter_id, person.principal_id)
        repo.set_enabled('synthetic.reviewer', False, actor='synthetic-operator')
        with pytest.raises(KeyError):
            store.membership(matter.matter_id, person.principal_id)
        repo.set_enabled('synthetic.reviewer', True, actor='synthetic-operator')
        assert store.membership(matter.matter_id, person.principal_id)
        import json
        data = json.loads(repo.path.read_text())
        data['accounts'] = [account for account in data['accounts'] if account['username'] != 'synthetic.reviewer']
        repo.path.write_text(json.dumps(data))
        with pytest.raises(KeyError):
            store.source_catalog_for_export(matter.matter_id, person.principal_id)
        reviewer.close()


def test_group_report_lease_connections_and_late_completion_revocation(tmp_path):
    from case_intelligence.report_compilation_jobs import ReportCompilationJobs, CompilationLeaseLost
    store, a, _, first, _ = seed(tmp_path / 'control.sqlite')
    jobs = ReportCompilationJobs(store)
    job, _ = jobs.queue(a.matter_id, 'principal-reviewer', 'timeline', '', ('research:synthetic-selection',), 'compile-request-' + 'a' * 32)
    claimed = jobs.claim('synthetic-worker')
    assert claimed.job_id == job.job_id
    assert jobs.heartbeat(claimed)
    assert jobs.heartbeat_deadline(claimed) is not None
    jobs.record_input_fingerprint(claimed, 'a' * 64)
    store.set_matter_group_grant(a.matter_id, first, 'principal-owner', present=False)
    assert not jobs.heartbeat(claimed)
    assert jobs.heartbeat_deadline(claimed) is None
    called = []
    with pytest.raises(CompilationLeaseLost):
        jobs.complete(claimed, lambda: called.append(True), fingerprint='a' * 64)
    assert not called
    store.close()


def test_group_revocation_fences_media_dispatch_and_transcript_import(tmp_path):
    from case_intelligence.media_evidence import MediaCoordinator, MediaProcessorError
    store, matter, _, group, _ = seed(tmp_path / 'control.sqlite')
    job = store.queue_media_job(matter.matter_id, 'a' * 32, 'b' * 32,
                                'principal-reviewer', source_sha256='c' * 64,
                                byte_size=128, media_type='audio/wav', duration_ms=1000)
    # Use the coordinator's dispatch fence without launching a media/model worker.
    coordinator = object.__new__(MediaCoordinator)
    coordinator.workspace = store
    coordinator._require_job_access(job)
    store.set_matter_group_grant(matter.matter_id, group, 'principal-owner', present=False)
    with pytest.raises(MediaProcessorError, match='Matter access was removed'):
        coordinator._require_job_access(job)
    with pytest.raises(KeyError):
        store.import_media_transcript(job.media_job_id,
            segments=[{'start_ms': 0, 'end_ms': 1000, 'text': 'Synthetic words'}],
            warnings=[], quality={}, provenance={})
    assert store.media_transcript(matter.matter_id, job.document_id, job.source_version_id) is None
    store.close()


def test_concurrent_group_revocation_preserves_independent_direct_grant(tmp_path):
    path = tmp_path / 'control.sqlite'
    store, matter, _, group, _ = seed(path)
    other = WorkspaceStore(path)
    with ThreadPoolExecutor(2) as pool:
        direct = pool.submit(other.add_member, matter.matter_id, 'principal-reviewer', 'principal-owner')
        revoke = pool.submit(store.set_matter_group_grant, matter.matter_id, group, 'principal-owner', present=False)
        direct.result()
        revoke.result()
    assert store.access_reasons(matter.matter_id, 'principal-reviewer') == ('Direct member',)
    other.close()
    store.close()


def test_group_change_rolls_back_when_attribution_fails(tmp_path, monkeypatch):
    store, matter, _, group, _ = seed(tmp_path / 'control.sqlite')
    def unavailable(**kwargs):
        raise RuntimeError('Synthetic audit unavailable')
    monkeypatch.setattr(store, '_append_audit_event_locked', unavailable)
    with pytest.raises(RuntimeError, match='Synthetic audit unavailable'):
        store.set_matter_group_grant(matter.matter_id, group, 'principal-owner', present=False)
    assert store.access_reasons(matter.matter_id, 'principal-reviewer') == ('Group: First team',)
    with pytest.raises(RuntimeError):
        store.set_team_group_member(group, 'principal-reviewer', 'principal-administrator',
                                    present=False, administrator_override=True)
    assert store.access_reasons(matter.matter_id, 'principal-reviewer') == ('Group: First team',)
    store.close()


def test_local_eligibility_installed_before_recovery_workers(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from tests.test_browser_local_accounts import configured_app, ORIGIN
    from case_intelligence.identity import LocalAccountSettings
    from case_intelligence.workbench import create_workbench_app
    from case_intelligence.generation import UnavailableGenerator
    app, repo = configured_app(tmp_path)
    with TestClient(app, base_url=ORIGIN):
        pass
    original = WorkspaceStore.recover_running_analysis_runs
    observed = []
    def recovery(store):
        observed.append((store.principal_enabled('local', 'alice.admin'),
                         store.principal_enabled('local', 'missing.reviewer')))
        return original(store)
    monkeypatch.setattr(WorkspaceStore, 'recover_running_analysis_runs', recovery)
    restarted = create_workbench_app(tmp_path / 'runtime', generator=UnavailableGenerator(),
        auth_mode='local', secure_cookie=True, local_settings=LocalAccountSettings(repo.path),
        learned_retrieval=False, answer_workers=1)
    with TestClient(restarted, base_url=ORIGIN):
        assert observed == [(True, False)]


def test_matter_purge_removes_only_its_group_grants(tmp_path):
    store, a, b, first, _ = seed(tmp_path / 'control.sqlite')
    store.set_matter_group_grant(b.matter_id, first, 'principal-owner', present=True)
    _, lifecycle = claim_synthetic_exit(store, a, 'principal-owner')
    assert store.complete_matter_purge(a.matter_id, lifecycle.purge_id).state == 'deleted'
    assert store.matter_group_grants(a.matter_id) == ()
    assert len(store.team_groups()) == 2
    assert store.membership(b.matter_id, 'principal-reviewer')
    assert store.connection.execute('PRAGMA foreign_key_check').fetchall() == []
    store.close()


def test_disabled_person_direct_grant_can_be_removed_before_reenable(tmp_path):
    from fastapi.testclient import TestClient
    from tests.test_browser_local_accounts import configured_app, login, create, csrf, ORIGIN
    app, repo = configured_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as admin:
        login(admin)
        create(admin, 'synthetic.reviewer')
        reviewer = TestClient(app, base_url=ORIGIN)
        login(reviewer, 'synthetic.reviewer')
        store = app.state.workbench.workspace
        owner = next(p for p in store.active_principals() if p.provider_subject == 'alice.admin')
        person = next(p for p in store.active_principals() if p.provider_subject == 'synthetic.reviewer')
        matter = store.create_matter('Synthetic disabled direct grant', '', owner.principal_id)
        store.add_member(matter.matter_id, person.principal_id, owner.principal_id)
        repo.set_enabled('synthetic.reviewer', False, actor='synthetic-operator')
        page = admin.get(f'/matters/{matter.slug}/setup')
        assert page.status_code == 200
        assert 'Retained grants without active access' in page.text
        assert 'Remove retained direct grant' in page.text
        assert admin.post(f'/matters/{matter.slug}/members/{person.principal_id}/remove',
                          data={'csrf_token': csrf(admin)}, follow_redirects=False).status_code == 303
        repo.set_enabled('synthetic.reviewer', True, actor='synthetic-operator')
        with pytest.raises(KeyError):
            store.membership(matter.matter_id, person.principal_id)
        reviewer.close()


@pytest.mark.parametrize('decision', ['failed_retry', 'preflight_retry', 'preflight_continue'])
def test_current_member_can_recover_media_after_original_requester_revocation(tmp_path, decision):
    from case_intelligence.media_evidence import MediaCoordinator, MediaProcessorError
    store, matter, _, group, _ = seed(tmp_path / 'control.sqlite')
    original = store.queue_media_job(matter.matter_id, 'a' * 32, 'b' * 32,
        'principal-reviewer', source_sha256='c' * 64, byte_size=128,
        media_type='audio/wav', duration_ms=1000)
    store.claim_media_job('synthetic-worker')
    if decision == 'failed_retry':
        held = store.fail_media_job(original.media_job_id, 'Synthetic interruption')
    else:
        held = store.save_media_preflight(original.media_job_id,
            {'outcome': 'uncertain'}, hold=True, message='Synthetic recording needs review')
    store.set_matter_group_grant(matter.matter_id, group, 'principal-owner', present=False)

    def recover(actor):
        if decision == 'failed_retry':
            return store.retry_media_job(matter.matter_id, original.document_id, actor)
        return store.decide_media_preflight(matter.matter_id, original.document_id,
            original.source_version_id, actor, held.preflight['inspection_id'],
            retry=decision == 'preflight_retry', request_id='synthetic-recovery')

    with pytest.raises(KeyError):
        recover('principal-reviewer')
    unchanged = store.media_job(matter.matter_id, original.document_id)
    assert unchanged.state == held.state
    assert unchanged.requested_by == 'principal-reviewer'
    recovered = recover('principal-owner')
    assert recovered.state == 'queued'
    assert recovered.requested_by == 'principal-owner'
    for field in ('media_job_id', 'document_id', 'source_version_id', 'source_sha256',
                  'byte_size', 'media_type', 'duration_ms'):
        assert getattr(recovered, field) == getattr(original, field)
    coordinator = object.__new__(MediaCoordinator)
    coordinator.workspace = store
    with pytest.raises(MediaProcessorError, match='Matter access was removed'):
        coordinator._require_job_access(original)
    claimed = store.claim_media_job('synthetic-recovery-worker')
    coordinator._require_job_access(claimed)
    transcript = store.import_media_transcript(claimed.media_job_id,
        segments=[{'external_segment_id': 'synthetic-segment', 'start_ms': 0,
                   'end_ms': 1000, 'model_text': 'Synthetic recovery words.'}],
        warnings=[], quality={}, provenance={})
    assert transcript.imported_by == 'principal-owner'
    assert transcript.source_version_id == original.source_version_id
    with pytest.raises(KeyError):
        store.membership(matter.matter_id, 'principal-reviewer')
    store.close()
