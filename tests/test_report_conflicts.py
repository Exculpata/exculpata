"""Synthetic concurrent Report edits, structure, and finalization."""

from tests.fixture_write import fixture_write
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading

import pytest
from fastapi.testclient import TestClient

from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore
from tests.test_matter_management import OWNER, OTHER, _app, _create_matter, _csrf, _headers, _principal_id


def _seed(tmp_path):
    store = WorkspaceStore(tmp_path / 'workspace.sqlite', clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    for actor in ('owner', 'member'):
        store.upsert_principal('test', actor, actor.title(), actor, preferred_principal_id='principal-' + actor)
    matter = store.create_matter('Generated team Reports', '', 'principal-owner')
    store.add_member(matter.matter_id, 'principal-member', 'principal-owner')
    report = store.create_report(matter.matter_id, 'principal-owner', 'Generated shared Report', 'Original purpose')
    sections = [store.add_report_section(matter.matter_id, report.report_id, 'principal-owner',
        heading='Generated ' + label, body='Original ' + label, expected_status='draft') for label in ('A', 'B', 'C')]
    return store, matter, store.report(matter.matter_id, report.report_id), sections


def _header(store, matter, report, *, actor='principal-owner', **changes):
    values = dict(title=report.title, purpose=report.purpose, status=report.status)
    values.update(changes)
    return store.update_report(matter.matter_id, report.report_id, actor,
                               expected_updated_at=report.updated_at, **values)


def _section(store, matter, report, section, *, actor='principal-owner', **changes):
    values = dict(heading=section.heading, body=section.body)
    values.update(changes)
    return store.update_report_section(matter.matter_id, report.report_id, section.section_id, actor,
        expected_updated_at=section.updated_at, expected_status=report.status, **values)


def test_header_and_section_conflicts_preserve_newer_work(tmp_path):
    first, matter, report, sections = _seed(tmp_path)
    second = WorkspaceStore(first.path)
    try:
        saved = _header(first, matter, report, purpose='Owner newer purpose')
        with pytest.raises(WorkspaceProblem, match='changed'):
            _header(second, matter, report, actor='principal-member', title='Older title edit')
        assert first.report(matter.matter_id, report.report_id).purpose == saved.purpose
        saved_section = _section(first, matter, saved, sections[0], body='Owner newer section')
        with pytest.raises(WorkspaceProblem, match='changed'):
            _section(second, matter, saved, sections[0], actor='principal-member', heading='Older heading edit')
        assert first.report_sections(matter.matter_id, report.report_id)[0].body == saved_section.body
    finally:
        second.close(); first.close()


def test_independent_sections_can_save_and_parallel_appends_keep_both(tmp_path):
    first, matter, report, sections = _seed(tmp_path)
    second = WorkspaceStore(first.path)
    try:
        _section(first, matter, report, sections[0], body='Owner section A')
        _section(second, matter, report, sections[1], actor='principal-member', body='Member section B')
        barrier = threading.Barrier(2)
        def append(store, actor):
            barrier.wait(timeout=5)
            return store.add_report_section(matter.matter_id, report.report_id, actor,
                heading=actor, body='Independent append', expected_status='draft')
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(append, first, 'principal-owner'), pool.submit(append, second, 'principal-member')]
            added = [future.result(timeout=10) for future in futures]
        current = first.report_sections(matter.matter_id, report.report_id)
        assert current[0].body == 'Owner section A' and current[1].body == 'Member section B'
        assert len(current) == 5 and [section.ordinal for section in current] == [1, 2, 3, 4, 5]
        assert len({section.section_id for section in added}) == 2
    finally:
        second.close(); first.close()


def test_finalization_checks_complete_report_and_invalidates_old_draft_actions(tmp_path):
    store, matter, report, sections = _seed(tmp_path)
    edited = _section(store, matter, report, sections[0], body='Work not seen by old finalizer')
    with pytest.raises(WorkspaceProblem, match='changed'):
        _header(store, matter, report, status='final')
    current = store.report(matter.matter_id, report.report_id)
    final = _header(store, matter, current, status='final')
    with pytest.raises(WorkspaceProblem, match='changed'):
        _section(store, matter, report, edited, body='Old Draft page edit')
    with pytest.raises(WorkspaceProblem, match='changed'):
        store.add_report_section(matter.matter_id, report.report_id, 'principal-member',
                                 heading='Old Draft append', body='No', expected_status='draft')
    # Current Final remains deliberately editable under the existing policy.
    updated = _section(store, matter, final, edited, body='Deliberate edit of current Final')
    assert updated.body == 'Deliberate edit of current Final'
    assert store.report(matter.matter_id, report.report_id).status == 'final'
    store.close()


def test_old_order_and_deletion_forms_refuse_newer_work(tmp_path):
    store, matter, report, sections = _seed(tmp_path)
    store.move_report_section(matter.matter_id, report.report_id, sections[1].section_id,
                              'principal-owner', 'up', expected_updated_at=report.updated_at)
    with pytest.raises(WorkspaceProblem, match='changed'):
        store.move_report_section(matter.matter_id, report.report_id, sections[2].section_id,
                                  'principal-member', 'up', expected_updated_at=report.updated_at)
    assert [s.section_id for s in store.report_sections(matter.matter_id, report.report_id)] == [sections[1].section_id, sections[0].section_id, sections[2].section_id]
    edited = _section(store, matter, report, sections[0], body='Newer section before old deletion')
    with pytest.raises(WorkspaceProblem, match='changed'):
        store.delete_report_section(matter.matter_id, report.report_id, sections[0].section_id,
            'principal-member', expected_updated_at=sections[0].updated_at, expected_status='draft')
    with pytest.raises(WorkspaceProblem, match='changed'):
        store.delete_report(matter.matter_id, report.report_id, 'principal-member', expected_updated_at=report.updated_at)
    store.delete_report_section(matter.matter_id, report.report_id, edited.section_id,
        'principal-member', expected_updated_at=edited.updated_at, expected_status='draft')
    assert [s.ordinal for s in store.report_sections(matter.matter_id, report.report_id)] == [1, 2]
    store.close()


def test_repeated_clock_and_away_back_edits_invalidate_older_tokens(tmp_path):
    store, matter, report, sections = _seed(tmp_path)
    changed = _section(store, matter, report, sections[0], body='Intermediate')
    returned = _section(store, matter, report, changed, body=sections[0].body)
    assert datetime.fromisoformat(returned.updated_at) > datetime.fromisoformat(changed.updated_at)
    with pytest.raises(WorkspaceProblem, match='changed'):
        _section(store, matter, report, sections[0], body='Old overwrite')
    with pytest.raises(WorkspaceProblem, match='changed'):
        _header(store, matter, report, status='final')
    store.close()


@pytest.mark.parametrize('operation', ['header', 'section', 'append', 'move', 'remove', 'delete'])
@pytest.mark.parametrize('denial', ['revoked', 'inactive', 'closing', 'other_matter'])
def test_report_mutations_recheck_matter_and_member(tmp_path, operation, denial):
    store, matter, report, sections = _seed(tmp_path)
    if denial == 'other_matter':
        matter = store.create_matter('Other matter canary', '', 'principal-member')
    else:
        with fixture_write(store):
            if denial == 'revoked':
                store.connection.execute("UPDATE workbench_matter_membership SET state='revoked' WHERE principal_id='principal-member'")
            elif denial == 'inactive':
                store.connection.execute("UPDATE workbench_principal SET active=0 WHERE principal_id='principal-member'")
            else:
                store.connection.execute("UPDATE workbench_matter_lifecycle SET state='purging' WHERE matter_id=?", (matter.matter_id,))
    with pytest.raises(KeyError):
        if operation == 'header':
            _header(store, matter, report, actor='principal-member')
        elif operation == 'section':
            _section(store, matter, report, sections[0], actor='principal-member')
        elif operation == 'append':
            store.add_report_section(matter.matter_id, report.report_id, 'principal-member', heading='Denied', body='', expected_status='draft')
        elif operation == 'move':
            store.move_report_section(matter.matter_id, report.report_id, sections[1].section_id, 'principal-member', 'up', expected_updated_at=report.updated_at)
        elif operation == 'remove':
            store.delete_report_section(matter.matter_id, report.report_id, sections[0].section_id, 'principal-member', expected_updated_at=sections[0].updated_at, expected_status='draft')
        else:
            store.delete_report(matter.matter_id, report.report_id, 'principal-member', expected_updated_at=report.updated_at)
    store.close()


def test_http_preserves_header_section_and_deleted_report_drafts(tmp_path):
    app = _app(tmp_path)
    with TestClient(app, base_url='https://testserver') as owner:
        token = _csrf(owner.get('/matters/new', headers=_headers(OWNER)).text)
        slug = _create_matter(owner, principal=OWNER, csrf_token=token, name='Generated Report forms')
        store = app.state.workbench.workspace
        actor = _principal_id(owner, OWNER)
        matter = store.get_matter(slug, actor)
        report = store.create_report(matter.matter_id, actor, 'Original Report', 'Original purpose')
        section = store.add_report_section(matter.matter_id, report.report_id, actor,
            heading='Original heading', body='Original section', expected_status='draft')
        shown = store.report(matter.matter_id, report.report_id)
        path = f'/matters/{slug}/reports/{report.report_id}'
        current = store.update_report(matter.matter_id, report.report_id, actor,
            title='Teammate title', purpose='Teammate purpose', status='draft', expected_updated_at=shown.updated_at)
        data = dict(csrf_token=token, expected_updated_at=shown.updated_at, title='<Unsaved title>', purpose='Unsaved purpose', status='final')
        denied = owner.post(path, data={**data, 'csrf_token': 'wrong'}, headers=_headers(OWNER))
        assert denied.status_code == 403
        conflict = owner.post(path, data=data, headers=_headers(OWNER))
        assert conflict.status_code == 409 and 'Teammate purpose' in conflict.text
        assert '&lt;Unsaved title&gt;' in conflict.text and 'Unsaved purpose' in conflict.text
        assert 'value="final" selected' in conflict.text and f'value="{current.updated_at}"' in conflict.text
        missing = owner.post(path, data={**data, 'expected_updated_at': ''}, headers=_headers(OWNER))
        assert missing.status_code == 409
        updated = store.update_report_section(matter.matter_id, report.report_id, section.section_id, actor,
            heading='Teammate heading', body='Teammate section', expected_updated_at=section.updated_at, expected_status='draft')
        section_path = path + '/sections/' + section.section_id
        draft = dict(csrf_token=token, expected_updated_at=section.updated_at, expected_status='draft', heading='<Unsaved heading>', body='Unsaved section')
        conflict = owner.post(section_path, data=draft, headers=_headers(OWNER))
        assert conflict.status_code == 409 and 'Teammate section' in conflict.text and 'Unsaved section' in conflict.text
        assert '&lt;Unsaved heading&gt;' in conflict.text and f'value="{updated.updated_at}"' in conflict.text
        saved = owner.post(section_path, data={**draft, 'expected_updated_at': updated.updated_at}, headers=_headers(OWNER), follow_redirects=False)
        assert saved.status_code == 303
        invalid = owner.post(path, data={**data, 'expected_updated_at': store.report(matter.matter_id, report.report_id).updated_at, 'title': '  '}, headers=_headers(OWNER))
        assert invalid.status_code == 400 and 'Unsaved purpose' in invalid.text
        store.delete_report(matter.matter_id, report.report_id, actor, expected_updated_at=store.report(matter.matter_id, report.report_id).updated_at)
        gone = owner.post(section_path, data=draft, headers=_headers(OWNER))
        assert gone.status_code == 409 and 'deleted' in gone.text and 'Unsaved section' in gone.text
        assert not store.reports(matter.matter_id, actor)
