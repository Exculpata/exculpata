"""Synthetic owner/admin rename, stale forms, and stable matter identity."""
import io

import pytest

from tests.case_lifecycle_helpers import prepare_purge_confirmation
from fastapi.testclient import TestClient

from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore
from tests.test_matter_management import (
    ADMIN, OTHER, OWNER, _app, _create_matter, _csrf, _headers, _principal_id,
)


def _store(tmp_path):
    store = WorkspaceStore(tmp_path / 'workspace.sqlite')
    for actor in ('owner', 'member', 'admin'):
        store.upsert_principal('test', actor, actor.title(), actor,
                               preferred_principal_id=f'principal-{actor}')
    matter = store.create_matter('Generated original', '', 'principal-owner')
    store.add_member(matter.matter_id, 'principal-member', 'principal-owner')
    return store, matter


def _rename(store, matter, name, **kwargs):
    return store.rename_matter(
        matter.slug, kwargs.pop('actor', 'principal-owner'), name,
        expected_name=kwargs.pop('expected_name', matter.display_name),
        request_id='request-synthetic-rename', **kwargs,
    )


def test_store_rename_is_atomic_attributed_and_does_not_change_identity(tmp_path, monkeypatch):
    store, matter = _store(tmp_path)
    neighbor = store.create_matter('Generated neighbor', '', 'principal-owner')
    before = store.connection.execute('SELECT * FROM workbench_matter_membership').fetchall()
    renamed = _rename(store, matter, '  Generated revised  ')
    assert renamed.display_name == 'Generated revised'
    for key in ('matter_id', 'slug', 'descriptor', 'owner_id', 'created_at'):
        assert getattr(renamed, key) == getattr(matter, key)
    assert store.get_matter(neighbor.slug, 'principal-owner') == neighbor
    assert store.connection.execute('SELECT * FROM workbench_matter_membership').fetchall() == before
    events = store.audit_events(matter.matter_id)
    assert len(events) == 1 and events[0].action == 'matter.rename'
    assert events[0].actor_principal_id == 'principal-owner'
    assert 'Generated' not in str(events)
    def fail_audit(**kwargs):
        raise RuntimeError('synthetic audit failure')
    monkeypatch.setattr(store, '_append_audit_event_locked', fail_audit)
    with pytest.raises(RuntimeError, match='audit failure'):
        _rename(store, renamed, 'Must roll back')
    assert store.get_matter(matter.slug, 'principal-owner') == renamed
    store.close()


def test_two_connections_reject_stale_rename(tmp_path):
    first, original = _store(tmp_path)
    second = WorkspaceStore(first.path)
    try:
        saved = _rename(first, original, 'Saved by owner')
        with pytest.raises(WorkspaceProblem, match='changed'):
            _rename(second, original, 'Older admin form', actor='principal-admin',
                    administrator_override=True)
        assert second.get_active_matter(original.slug).display_name == saved.display_name
        assert len(second.audit_events(original.matter_id)) == 1
    finally:
        second.close()
        first.close()


@pytest.mark.parametrize('name', ['', ' ' * 4, 'x' * 141, 'bad\x00name', 'bad\u202ename'])
def test_invalid_name_is_not_saved(tmp_path, name):
    store, matter = _store(tmp_path)
    with pytest.raises(WorkspaceProblem):
        _rename(store, matter, name)
    assert store.get_active_matter(matter.slug) == matter
    assert not store.audit_events(matter.matter_id)
    store.close()


@pytest.mark.parametrize('actor,override,state', [
    ('principal-member', False, 'active'),
    ('principal-admin', False, 'active'),
    ('principal-owner', False, 'purging'),
    ('principal-admin', True, 'purge_failed'),
    ('principal-admin', True, 'deleted'),
])
def test_store_revalidates_authority_and_lifecycle(tmp_path, actor, override, state):
    store, matter = _store(tmp_path)
    with store.connection:
        store.connection.execute('UPDATE workbench_matter_lifecycle SET state=? WHERE matter_id=?',
                                 (state, matter.matter_id))
    with pytest.raises(KeyError):
        _rename(store, matter, 'Denied', actor=actor, administrator_override=override)
    assert not store.audit_events(matter.matter_id)
    store.close()


def test_inactive_administrator_cannot_rename(tmp_path):
    store, matter = _store(tmp_path)
    with store.connection:
        store.connection.execute("UPDATE workbench_principal SET active=0 WHERE principal_id='principal-admin'")
    with pytest.raises(KeyError):
        _rename(store, matter, 'Denied', actor='principal-admin', administrator_override=True)
    store.close()


def test_browser_routes_permissions_recovery_and_source_identity(tmp_path):
    app = _app(tmp_path)
    with TestClient(app, base_url='https://testserver') as owner, \
         TestClient(app, base_url='https://testserver') as admin, \
         TestClient(app, base_url='https://testserver') as member:
        owner_token = _csrf(owner.get('/matters/new', headers=_headers(OWNER)).text)
        slug = _create_matter(owner, principal=OWNER, csrf_token=owner_token, name='Generated original')
        admin_token = _csrf(admin.get('/matters/new', headers=_headers(ADMIN)).text)
        member_token = _csrf(member.get('/matters/new', headers=_headers(OTHER)).text)
        bench = app.state.workbench
        owner_id = _principal_id(owner, OWNER)
        matter = bench.workspace.get_matter(slug, owner_id)
        source_store = bench.source_store(matter)
        document, _ = source_store.store_stream('generated-source.txt', 'text/plain',
                                               io.BytesIO(b'Synthetic original evidence.'))
        documents_before = dict(source_store.documents)
        path = f'/matters/{slug}/rename'
        data = {'csrf_token': owner_token, 'name': 'Generated new', 'expected_name': matter.display_name}
        assert owner.post(path, data={**data, 'csrf_token': 'wrong'}, headers=_headers(OWNER)).status_code == 403
        assert member.post(path, data={**data, 'csrf_token': member_token}, headers=_headers(OTHER)).status_code == 404
        bench.workspace.add_member(matter.matter_id, _principal_id(member, OTHER), owner_id)
        assert 'id="rename-matter-form"' not in member.get(f'/matters/{slug}/settings', headers=_headers(OTHER)).text
        assert member.post(path, data={**data, 'csrf_token': member_token}, headers=_headers(OTHER)).status_code == 403
        page = owner.get(f'/matters/{slug}/settings', headers=_headers(OWNER))
        assert 'id="rename-matter-form"' in page.text
        invalid = owner.post(path, data={**data, 'name': 'x' * 141}, headers=_headers(OWNER))
        assert invalid.status_code == 400 and 'x' * 141 in invalid.text
        renamed = owner.post(path, data=data, headers=_headers(OWNER))
        assert renamed.status_code == 200 and 'Matter renamed.' in renamed.text
        stale = admin.post(path, data={**data, 'csrf_token': admin_token, 'name': '<Older admin form>'}, headers=_headers(ADMIN))
        assert stale.status_code == 409 and 'changed' in stale.text
        assert '&lt;Older admin form&gt;' in stale.text
        assert 'value="Generated new"' in stale.text
        saved = admin.post(path, data={**data, 'csrf_token': admin_token, 'expected_name': 'Generated new',
                                     'name': 'Administrator renamed'}, headers=_headers(ADMIN))
        assert saved.status_code == 200 and 'Matter renamed.' in saved.text
        current = bench.workspace.get_matter(slug, owner_id)
        assert current.matter_id == matter.matter_id
        assert bench.source_store(current) is source_store
        assert source_store.documents == documents_before
        assert source_store.documents[document.document_id].version_id == document.version_id
        for route in ('/matters/manage', f'/matters/{slug}/home', f'/matters/{slug}/settings', f'/matters/{slug}/close'):
            assert 'Administrator renamed' in owner.get(route, headers=_headers(OWNER)).text
        renames = [e for e in bench.workspace.audit_events(matter.matter_id) if e.action == 'matter.rename' and e.outcome == 'success']
        assert len(renames) == 2
        assert renames[-1].actor_principal_id == _principal_id(admin, ADMIN)


def test_close_confirmation_serializes_with_rename(tmp_path, monkeypatch):
    import threading
    store, matter = _store(tmp_path)
    second = WorkspaceStore(store.path)
    started = threading.Event()
    outcomes = []
    counts = store._active_matter_work_counts_locked
    def attempt_rename():
        started.set()
        try:
            _rename(second, matter, 'Concurrent rename')
            outcomes.append('renamed')
        except KeyError:
            outcomes.append('closed')
    writer = threading.Thread(target=attempt_rename)
    def pause_after_confirmation(matter_id):
        writer.start()
        assert started.wait(2)
        # A second connection must not rename across the atomic closure freeze.
        writer.join(timeout=0.1)
        return counts(matter_id)
    monkeypatch.setattr(store, '_active_matter_work_counts_locked', pause_after_confirmation)
    try:
        store.case_lifecycle.begin_exit(matter.slug, 'principal-owner')
        writer.join(timeout=5)
        assert not writer.is_alive()
        assert outcomes == ['closed']
        monkeypatch.setattr(store, '_active_matter_work_counts_locked', counts)
        confirmation = prepare_purge_confirmation(store, matter, 'principal-owner')
        _, lifecycle = store.begin_matter_purge(matter.slug, 'principal-owner', matter.display_name,
            source_count=0, **confirmation)
        assert lifecycle.state == 'purging'
    finally:
        writer.join(timeout=5)
        second.close()
        store.close()
