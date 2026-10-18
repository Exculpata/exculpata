"""Separate deliberate imports retain context; same-item retries remain idempotent."""

from tests.fixture_write import fixture_write
import time

import pytest

from case_intelligence.pilot_uploads import PilotStore
from case_intelligence.intake_receipts import IntakeReceipts
from case_intelligence.workspace_store import WorkspaceProblem
from tests.test_intake_receipt_http import ACTOR, descriptor, selection, upload, put, workspace  # noqa: F401


def _settled(bench, matter):
    deadline = time.monotonic() + 10
    while any(bench.workspace.active_matter_work_counts(matter.matter_id).values()):
        assert time.monotonic() < deadline, 'Generated upload did not finish'
        time.sleep(.02)


@pytest.mark.parametrize('second_body', [
    b'Synthetic repeated production passage.\n',
    b'Synthetic distinct second production passage.\n',
])
def test_separate_confirmations_preserve_same_path_occurrences(workspace, second_body):
    client, bench, matter = workspace
    bodies = [b'Synthetic repeated production passage.\n', second_body]
    receipts = []
    sessions = []
    for index, body in enumerate(bodies):
        files = [descriptor('Records/report.txt', len(body))]
        title = f'Generated production {index + 1}'
        receipt = selection(client, matter.slug, files, [0], key=str(index + 1) * 32, title=title)
        session = upload(client, matter.slug, receipt, files, [0], collection_name=title)
        item = session['items'][0]
        put(client, item, body)
        finalized = client.post(item['finalize_url'])
        assert finalized.status_code == 200, finalized.text
        _settled(bench, matter)
        receipts.append(receipt)
        sessions.append(session)
    store = bench.source_store(matter)
    assert len(store.documents) == 2
    assert len({d.version_id for d in store.documents.values()}) == 2
    for session, body, receipt in zip(sessions, bodies, receipts):
        library = bench.source_library(matter, collection_id=session['collection_id'])
        assert library.total == 1
        document = store.get(library.items[0].document_id)
        assert document.relative_path == 'Records/report.txt'
        assert store.source_path(document.document_id).read_bytes() == body
        status = client.get(receipt['receipt_url'])
        assert status.status_code == 200 and 'Open source' in status.text
        row = IntakeReceipts(bench.workspace).snapshot(matter.matter_id, ACTOR, receipt['receipt_id'])['items'][0]
        assert (row['catalog_document_id'], row['version_id']) == (document.document_id, document.version_id)
        repeated = client.post(session['items'][0]['finalize_url'])
        assert repeated.status_code == 200
        assert len(store.documents) == 2


def test_interrupted_control_commit_reuses_the_same_source_occurrence(workspace, monkeypatch):
    client, bench, matter = workspace
    body = b'Synthetic interrupted occurrence commit.\n'
    files = [descriptor('Records/report.txt', len(body))]
    receipt = selection(client, matter.slug, files, [0])
    session = upload(client, matter.slug, receipt, files, [0])
    item = session['items'][0]
    put(client, item, body)
    original = bench.workspace.finish_upload_item
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkspaceProblem('Synthetic control commit interruption')
        return original(*args, **kwargs)

    monkeypatch.setattr(bench.workspace, 'finish_upload_item', interrupted)
    assert client.post(item['finalize_url']).status_code == 409
    before = next(iter(bench.source_store(matter).documents.values()))
    assert client.post(item['finalize_url']).status_code == 200
    _settled(bench, matter)
    assert len(bench.source_store(matter).documents) == 1
    after = next(iter(bench.source_store(matter).documents.values()))
    assert (after.document_id, after.version_id) == (before.document_id, before.version_id)


@pytest.mark.parametrize('copy_fallback', [False, True])
def test_store_restart_preserves_item_identity_and_distinct_imports(tmp_path, monkeypatch, copy_fallback):
    import errno
    import case_intelligence.pilot_uploads as module

    if copy_fallback:
        def unavailable_link(*_args, **_kwargs):
            raise OSError(errno.EXDEV, 'Synthetic link unavailable')
        monkeypatch.setattr(module.os, 'link', unavailable_link)
    root = tmp_path / 'store'
    body = b'Synthetic resumable occurrence bytes.\n'
    store = PilotStore(root)
    first_item = 'upload-item-' + '1' * 32
    second_item = 'upload-item-' + '2' * 32

    def finish(current, item):
        return current.finalize_resumable_upload(item, display_name='report.txt',
            relative_path='Records/report.txt', content_type='text/plain', expected_size=len(body))

    store.append_resumable_chunk(first_item, expected_size=len(body), offset=0, chunk=body)
    first = finish(store, first_item)
    store.close()
    restored = PilotStore(root)
    retried = finish(restored, first_item)
    assert (retried.document_id, retried.version_id) == (first.document_id, first.version_id)
    restored.append_resumable_chunk(second_item, expected_size=len(body), offset=0, chunk=body)
    second = finish(restored, second_item)
    assert second.document_id != first.document_id
    assert len(restored.documents) == 2
    assert all(restored.source_path(d.document_id).read_bytes() == body for d in restored.documents.values())
    restored.close()



def test_removed_upload_stays_unavailable_without_blocking_remaining_search(workspace):
    client, bench, matter = workspace
    bodies = [b'Synthetic copper retained passage.\n', b'Synthetic amber removable passage.\n']
    files = [descriptor(f'Records/{name}.txt', len(body)) for name, body in zip(('keep', 'remove'), bodies)]
    receipt = selection(client, matter.slug, files, [0, 1])
    session = upload(client, matter.slug, receipt, files, [0, 1])
    for item, body in zip(session['items'], bodies):
        put(client, item, body)
        assert client.post(item['finalize_url']).status_code == 200
    _settled(bench, matter)
    store = bench.source_store(matter)
    removable = next(d for d in store.documents.values() if d.relative_path.endswith('remove.txt'))
    removed = client.post(f'/matters/{matter.slug}/sources/{store.action_token(removable)}/remove')
    assert removed.status_code == 200
    readiness = bench.workspace.matter_readiness(matter.matter_id)
    assert readiness.processing_count == 0 and readiness.attention_count == 1 and readiness.searchable_count == 1
    searched = client.get(f'/matters/{matter.slug}', params={'mode': 'search', 'q': 'copper retained'})
    assert 'open-support-link' in searched.text and 'Synthetic copper retained passage.' in searched.text
    counts = IntakeReceipts(bench.workspace).get(matter.matter_id, ACTOR, receipt['receipt_id'])['counts']
    assert counts['received'] == 2 and counts['unavailable'] == 1 and counts['searchable'] == 1


@pytest.mark.parametrize('job_state', ['queued', 'running', 'succeeded', 'failed', 'cancelled', None])
def test_uncataloged_received_source_waits_only_for_active_work(tmp_path, job_state):
    from tests.test_intake_receipts import seed
    w, m, _ = seed(tmp_path)
    from tests.test_intake_receipts import OWNER
    try:
        session, items = w.create_upload_session(m.matter_id, OWNER, 'Generated pending source',
            [{'display_name': 'source.txt', 'relative_path': 'source.txt', 'media_type': 'text/plain', 'expected_size': 7}])
        item = items[0]
        w.set_upload_item_offset(m.matter_id, OWNER, session.upload_session_id, item.upload_item_id, 0, 7)
        w.finish_upload_item(m.matter_id, OWNER, session.upload_session_id, item.upload_item_id, 'f' * 32)
        with fixture_write(w):
            if job_state is None:
                w.connection.execute('DELETE FROM workbench_ingest_job WHERE matter_id=?', (m.matter_id,))
            else:
                w.connection.execute('UPDATE workbench_ingest_job SET state=? WHERE matter_id=?', (job_state, m.matter_id))
        state = w.matter_readiness(m.matter_id)
        active = job_state in {'queued', 'running'}
        assert state.processing_count == int(active)
        assert state.attention_count == int(not active)
        assert state.searchable_count == 0
    finally:
        w.close()


@pytest.mark.parametrize('stored_link_exists', [True, False])
def test_legacy_pending_occurrence_requires_the_existing_stored_hard_link(tmp_path, stored_link_exists):
    store = PilotStore(tmp_path / 'store')
    body = b'Synthetic legacy pending occurrence.\n'
    item = 'upload-item-' + 'a' * 32
    store.append_resumable_chunk(item, expected_size=len(body), offset=0, chunk=body)
    arguments = dict(display_name='record.txt', relative_path='Records/record.txt',
        content_type='text/plain', expected_size=len(body))
    first = store.finalize_resumable_upload(item, **arguments)
    # The prior reader persisted the path key before the control-store commit.
    first.name_key = store.validate_relative_upload_path('Records/record.txt')[3]
    store._save((first.document_id,))
    if not stored_link_exists:
        store.source_path(first.document_id).unlink()
    current = store.finalize_resumable_upload(item, **arguments)
    assert (current.document_id == first.document_id) is stored_link_exists
    assert store.source_path(current.document_id).read_bytes() == body
    store.close()


def test_failed_copy_finalization_keeps_saved_upload_for_restart_retry(tmp_path, monkeypatch):
    import errno
    import case_intelligence.pilot_uploads as module

    def unavailable_link(*_args, **_kwargs):
        raise OSError(errno.EXDEV, 'Synthetic link unavailable')

    monkeypatch.setattr(module.os, 'link', unavailable_link)
    root = tmp_path / 'store'
    store = PilotStore(root)
    body = b'Synthetic retained upload after interrupted copy.\n'
    item = 'upload-item-' + 'a' * 32
    store.append_resumable_chunk(item, offset=0, expected_size=len(body), chunk=body)
    arguments = dict(display_name='record.txt', relative_path='Records/record.txt',
        content_type='text/plain', expected_size=len(body))

    def interrupted_copy(source, destination, expected_size, expected_digest):
        with destination.open('xb') as stream:
            stream.write(body[:7])
        raise OSError('Synthetic destination interruption')

    monkeypatch.setattr(store, '_copy_resumable_source', interrupted_copy)
    with pytest.raises(OSError, match='interruption'):
        store.finalize_resumable_upload(item, **arguments)
    assert not store.documents and list(store.files.iterdir()) == []
    assert (store.incoming / store._resumable_name(item)).read_bytes() == body
    store.close()
    reopened = PilotStore(root)
    try:
        received = reopened.finalize_resumable_upload(item, **arguments)
        assert reopened.source_path(received.document_id).read_bytes() == body
        retried = reopened.finalize_resumable_upload(item, **arguments)
        assert (retried.document_id, retried.version_id) == (received.document_id, received.version_id)
        assert len(reopened.documents) == 1
    finally:
        reopened.close()
