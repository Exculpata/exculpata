"""Synthetic team-note stale edit and review-action regressions."""

from tests.fixture_write import fixture_write
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading

import pytest
from fastapi.testclient import TestClient

from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore
from tests.test_matter_management import OWNER, OTHER, _app, _create_matter, _csrf, _headers, _principal_id
from tests.test_matter_notebook import _reference


def _seed(tmp_path):
    store = WorkspaceStore(tmp_path / 'workspace.sqlite', clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    for actor in ('owner', 'member'):
        store.upsert_principal('test', actor, actor.title(), actor, preferred_principal_id='principal-' + actor)
    matter = store.create_matter('Generated shared notes', '', 'principal-owner')
    store.add_member(matter.matter_id, 'principal-member', 'principal-owner')
    note, _ = store.create_notebook_item(matter.matter_id, 'principal-owner', item_type='note',
        status='needs_review', title='Generated shared note', body='Generated initial text', references=(_reference(),))
    return store, matter, note


def _edit(store, matter, note, *, actor='principal-owner', **changes):
    fields = dict(item_type=note.item_type, status=note.status, title=note.title,
                  body=note.body, date_label=note.date_label, pinned=bool(note.is_pinned))
    fields.update(changes)
    return store.update_notebook_item(matter.matter_id, actor, note.item_id,
                                     expected_updated_at=note.updated_at, **fields)


def test_stale_edit_and_review_never_restore_an_older_body(tmp_path):
    first, matter, note = _seed(tmp_path)
    second = WorkspaceStore(first.path)
    try:
        older = second.notebook_item(matter.matter_id, 'principal-member', note.item_id)
        newer = _edit(first, matter, note, title='Owner title', body='Owner newer body', date_label='Date kept')
        with pytest.raises(WorkspaceProblem, match='changed'):
            _edit(second, matter, older, actor='principal-member', title='Member older form')
        with pytest.raises(WorkspaceProblem, match='changed'):
            second.set_notebook_item_status(matter.matter_id, 'principal-member', note.item_id, 'confirmed',
                                            expected_updated_at=older.updated_at)
        current = first.notebook_item(matter.matter_id, 'principal-owner', note.item_id)
        assert current == newer
        reviewed = second.set_notebook_item_status(matter.matter_id, 'principal-member', note.item_id,
                                                   'confirmed', expected_updated_at=newer.updated_at)
        assert reviewed.status == 'confirmed' and reviewed.updated_by == 'principal-member'
        assert (reviewed.title, reviewed.body, reviewed.date_label) == (newer.title, newer.body, newer.date_label)
        assert first.notebook_references(matter.matter_id, 'principal-owner', note.item_id)[0].support_token == 'a' * 40
    finally:
        second.close()
        first.close()


def test_same_clock_and_returned_text_still_invalidate_old_forms(tmp_path):
    store, matter, note = _seed(tmp_path)
    changed = _edit(store, matter, note, body='Intermediate text')
    returned = _edit(store, matter, changed, body=note.body)
    assert datetime.fromisoformat(note.updated_at) < datetime.fromisoformat(changed.updated_at) < datetime.fromisoformat(returned.updated_at)
    with pytest.raises(WorkspaceProblem, match='changed'):
        _edit(store, matter, note, body='Stale overwrite')
    store.close()


def test_two_connections_admit_only_one_edit_from_the_same_version(tmp_path):
    first, matter, note = _seed(tmp_path)
    second = WorkspaceStore(first.path)
    barrier = threading.Barrier(2)
    def save(store, actor):
        barrier.wait(timeout=5)
        try:
            _edit(store, matter, note, actor=actor, title=actor, body=actor)
            return 'saved'
        except WorkspaceProblem:
            return 'conflict'
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(save, first, 'principal-owner'), pool.submit(save, second, 'principal-member')]
            assert sorted(f.result(timeout=10) for f in futures) == ['conflict', 'saved']
        current = first.notebook_item(matter.matter_id, 'principal-owner', note.item_id)
        assert current.title == current.body == current.updated_by
    finally:
        second.close()
        first.close()


def test_delete_refuses_a_note_changed_since_the_displayed_form(tmp_path):
    store, matter, note = _seed(tmp_path)
    newer = _edit(store, matter, note, body='Newer teammate work')
    with pytest.raises(WorkspaceProblem, match='changed'):
        store.delete_notebook_item(matter.matter_id, 'principal-member', note.item_id,
                                   expected_updated_at=note.updated_at)
    assert store.notebook_item(matter.matter_id, 'principal-owner', note.item_id) == newer
    store.delete_notebook_item(matter.matter_id, 'principal-member', note.item_id,
                               expected_updated_at=newer.updated_at)
    with pytest.raises(KeyError):
        store.notebook_item(matter.matter_id, 'principal-owner', note.item_id)
    store.close()


@pytest.mark.parametrize('operation', ['edit', 'status', 'delete'])
@pytest.mark.parametrize('denial', ['revoked', 'inactive', 'purging', 'other_matter'])
def test_write_rechecks_member_and_matter_authority(tmp_path, operation, denial):
    store, matter, note = _seed(tmp_path)
    if denial == 'other_matter':
        matter = store.create_matter('Neighbor canary', '', 'principal-member')
    else:
        with fixture_write(store):
            if denial == 'revoked':
                store.connection.execute("UPDATE workbench_matter_membership SET state='revoked' WHERE principal_id='principal-member'")
            elif denial == 'inactive':
                store.connection.execute("UPDATE workbench_principal SET active=0 WHERE principal_id='principal-member'")
            else:
                store.connection.execute("UPDATE workbench_matter_lifecycle SET state='purging' WHERE matter_id=?", (matter.matter_id,))
    with pytest.raises(KeyError):
        if operation == 'edit':
            _edit(store, matter, note, actor='principal-member', body='Denied')
        elif operation == 'status':
            store.set_notebook_item_status(matter.matter_id, 'principal-member', note.item_id, 'confirmed', expected_updated_at=note.updated_at)
        else:
            store.delete_notebook_item(matter.matter_id, 'principal-member', note.item_id, expected_updated_at=note.updated_at)
    store.close()


def test_http_conflict_preserves_every_unsaved_field_and_rechecks_on_retry(tmp_path):
    app = _app(tmp_path)
    with TestClient(app, base_url='https://testserver') as owner:
        member = TestClient(app, base_url='https://testserver')
        try:
            owner_csrf = _csrf(owner.get('/matters/new', headers=_headers(OWNER)).text)
            slug = _create_matter(owner, principal=OWNER, csrf_token=owner_csrf, name='Generated team notes')
            member_csrf = _csrf(member.get('/matters/new', headers=_headers(OTHER)).text)
            store = app.state.workbench.workspace
            owner_id, member_id = _principal_id(owner, OWNER), _principal_id(member, OTHER)
            matter = store.get_matter(slug, owner_id)
            store.add_member(matter.matter_id, member_id, owner_id)
            note, _ = store.create_notebook_item(matter.matter_id, owner_id, item_type='note', status='needs_review',
                                                 title='Initial title', body='Initial body')
            path = f'/matters/{slug}/notebook/items/{note.item_id}'
            page = member.get(f'/matters/{slug}/notebook?edit={note.item_id}', headers=_headers(OTHER))
            assert 'name="expected_updated_at"' in page.text
            data = dict(csrf_token=member_csrf, expected_updated_at=note.updated_at, item_type='event',
                        status='disputed', title='<Member title>', body='Member unsaved body',
                        date_label='Uncertain date', pinned='yes')
            assert member.post(path, data={**data, 'csrf_token': 'wrong'}, headers=_headers(OTHER)).status_code == 403
            newer = _edit(store, matter, note, actor=owner_id, title='Owner saved title', body='Owner saved body')
            response = member.post(path, data=data, headers=_headers(OTHER))
            assert response.status_code == 409
            assert 'Owner saved title' in response.text and 'Owner saved body' in response.text
            assert '&lt;Member title&gt;' in response.text and 'Member unsaved body' in response.text
            assert 'Uncertain date' in response.text and 'name="pinned" value="yes" checked' in response.text
            assert 'value="event" selected' in response.text and 'value="disputed" selected' in response.text
            assert f'value="{newer.updated_at}"' in response.text
            assert store.notebook_item(matter.matter_id, owner_id, note.item_id).body == 'Owner saved body'
            missing = member.post(path, data={**data, 'expected_updated_at': ''}, headers=_headers(OTHER))
            assert missing.status_code == 409 and 'Member unsaved body' in missing.text
            invalid = member.post(path, data={**data, 'expected_updated_at': newer.updated_at, 'title': '  '}, headers=_headers(OTHER))
            assert invalid.status_code == 400 and 'Member unsaved body' in invalid.text
            saved = member.post(path, data={**data, 'expected_updated_at': newer.updated_at}, headers=_headers(OTHER), follow_redirects=False)
            assert saved.status_code == 303
            current = store.notebook_item(matter.matter_id, owner_id, note.item_id)
            assert current.body == data['body'] and current.updated_by == member_id
            for operation in ('status', 'delete'):
                result = owner.post(path + '/' + operation, data={'csrf_token': owner_csrf, 'status': 'confirmed',
                                        'expected_updated_at': newer.updated_at}, headers=_headers(OWNER))
                assert result.status_code == 409
            store.delete_notebook_item(matter.matter_id, owner_id, note.item_id, expected_updated_at=current.updated_at)
            gone = member.post(path, data={**data, 'expected_updated_at': current.updated_at}, headers=_headers(OTHER))
            assert gone.status_code == 409 and 'Member unsaved body' in gone.text and 'deleted' in gone.text
            assert not store.all_notebook_items(matter.matter_id, owner_id)
        finally:
            member.close()
