"""Denial receipts survive concurrent work and rejected write transactions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from case_intelligence.identity import SESSION_COOKIE
from case_intelligence.matter_purpose_repository import MatterPurposeRepository
from case_intelligence.workspace_store import WorkspaceStore
from tests import test_browser_local_accounts as local_accounts
from tests.test_matter_management import _csrf
from tests.test_matter_knowledge_workflow import (
    test_notebook_rechecks_live_local_authority_after_render as check_local_authority,
)


@pytest.mark.parametrize('rollback', [False, True])
def test_denial_audit_waits_for_unrelated_transaction(tmp_path, monkeypatch, rollback):
    started, release = Event(), Event()
    original_app = local_accounts.configured_app
    transactions = []
    audit_attempts = []
    original_request = TestClient.request

    def release_after_response(self, *args, **kwargs):
        response = original_request(self, *args, **kwargs)
        if response.status_code == 404:
            release.set()
        return response

    monkeypatch.setattr(TestClient, 'request', release_after_response)

    class RejectedBackgroundWrite(Exception):
        pass

    with ThreadPoolExecutor(max_workers=1) as executor:
        def configured_app(path):
            app, accounts = original_app(path)
            bench, identity = app.state.workbench, app.state.identity
            original_resolve = identity.resolve
            original_audit = bench.workspace.append_audit_event

            def background_transaction():
                matter, = bench.workspace.all_matters()
                repository = bench.entity_service(matter).repository
                try:
                    with repository.transaction(matter.matter_id, matter.owner_id):
                        started.set()
                        assert release.wait(10), 'Denial did not release the background transaction'
                        if rollback:
                            raise RejectedBackgroundWrite
                except RejectedBackgroundWrite:
                    pass

            def resolve_then_start_transaction(token, *, read_only=False):
                current = original_resolve(token, read_only=read_only)
                if read_only and current is None:
                    assert not transactions
                    transactions.append(executor.submit(background_transaction))
                    assert started.wait(10), 'Background transaction did not start'
                return current

            def audit_after_background_transaction(**values):
                if values['action'] == 'matter.access' and values['outcome'] == 'denied':
                    audit_attempts.append(values)
                    release.set()
                return original_audit(**values)

            monkeypatch.setattr(identity, 'resolve', resolve_then_start_transaction)
            monkeypatch.setattr(bench.workspace, 'append_audit_event', audit_after_background_transaction)
            return app, accounts

        monkeypatch.setattr(local_accounts, 'configured_app', configured_app)
        try:
            check_local_authority(tmp_path, monkeypatch, 'demote', True, 'context')
        finally:
            release.set()
            for transaction in transactions:
                transaction.result(timeout=10)
    assert len(transactions) == len(audit_attempts) == 1


def test_late_denial_rolls_back_content_but_commits_denial_audit(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    app, accounts = local_accounts.configured_app(tmp_path)
    accounts.create('other.admin', 'Synthetic Administrator', local_accounts.PASSWORD,
                    administrator=True, actor='synthetic-operator')
    with TestClient(app, base_url=local_accounts.ORIGIN) as client:
        assert local_accounts.login(client).status_code == 303
        identity, bench = app.state.identity, app.state.workbench
        actor = identity.resolve(client.cookies.get(SESSION_COOKIE))
        matter = bench.create_matter('Synthetic denied write', '', actor.principal_id)
        token = _csrf(client.get('/matters/new').text)
        before = {event.event_id for event in bench.workspace.audit_events()}
        original_save = MatterPurposeRepository.save
        saved = []

        def save_then_demote(self, *args, **kwargs):
            result = original_save(self, *args, **kwargs)
            saved.append(result)
            accounts.set_administrator('alice.admin', False, actor='synthetic-operator')
            return result

        monkeypatch.setattr(MatterPurposeRepository, 'save', save_then_demote)
        response = client.put(f'/matters/{matter.slug}/purpose',
            headers={'X-CSRF-Token': token},
            json=dict(expected_revision=0, objective='Synthetic rejected objective', background=''))
        assert response.status_code == 404 and response.headers['cache-control'] == 'no-store'
        assert len(saved) == 1 and saved[0]['revision'] == 1
        assert saved[0]['objective'] not in response.text
        current = bench.workspace.matter_purpose(matter.matter_id, actor.principal_id)
        assert current['revision'] == 0 and current['objective'] == ''
        path = bench.workspace.path

    reopened = WorkspaceStore(path)
    try:
        events = [event for event in reopened.audit_events() if event.event_id not in before]
        assert not [event for event in events if event.action == 'matter.purpose_update']
        denied = [event for event in events if event.action == 'matter.access']
        assert len(denied) == 1 and denied[0].outcome == 'denied'
        assert denied[0].actor_principal_id == actor.principal_id
        assert denied[0].session_id == actor.session.session_id
        assert denied[0].request_id and denied[0].matter_id is None
    finally:
        reopened.close()
