"""Synthetic complete-selection accounting, resumable metadata and receipt boundaries."""

from tests.case_lifecycle_helpers import lifecycle_session
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading

import pytest

from tests.case_lifecycle_helpers import claim_synthetic_exit

from case_intelligence.intake_receipts import IntakeReceipts
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore

OWNER = 'generated-intake-owner'
MEMBER = 'generated-intake-member'


def seed(tmp_path):
    workspace = WorkspaceStore(tmp_path / 'workspace.sqlite')
    for who in (OWNER, MEMBER):
        workspace.upsert_principal('test', who, who.title(), who, preferred_principal_id=who)
    matter = workspace.create_matter('Generated selected record', 'Synthetic fixture', OWNER)
    workspace.add_member(matter.matter_id, MEMBER, OWNER)
    return workspace, matter, IntakeReceipts(workspace)


def descriptor(path, size=48):
    return {'name': path.rsplit('/', 1)[-1], 'relative_path': path, 'size': size, 'media_type': 'text/plain'}


def create(receipts, matter, count=4, eligible=(0, 1, 2), key='a' * 32):
    return receipts.create(matter.matter_id, OWNER, selection_key=key,
        selection_fingerprint='b' * 64, selected_count=count,
        eligible_indexes=list(eligible), collection_name='Generated folder')


def append(receipts, matter, receipt, files, start=0, states=None):
    return receipts.append(matter.matter_id, OWNER, receipt['receipt_id'], start=start,
        files=files, reviewed_states=states or ['valid'] * len(files),
        document_limit=1024, media_limit=2048, malware_scan_mode='off', scanner_ready=True)


def ready(receipts, matter):
    receipt = create(receipts, matter)
    files = [descriptor('North/report.txt'), descriptor('South/report.txt'),
             descriptor('Copies/copy.txt'), descriptor('Unsupported/opaque.bin', 27)]
    append(receipts, matter, receipt, files[:2])
    append(receipts, matter, receipt, files[2:], start=2, states=['valid', 'unsupported'])
    return receipts.seal(matter.matter_id, OWNER, receipt['receipt_id'])


def test_receipt_retains_complete_selection_across_restart_and_shared_read(tmp_path):
    w, m, receipts = seed(tmp_path)
    receipt = ready(receipts, m)
    assert receipt['state'] == 'ready'
    w.close()
    reopened = WorkspaceStore(tmp_path / 'workspace.sqlite')
    try:
        receipt = IntakeReceipts(reopened).get(m.matter_id, MEMBER, receipt['receipt_id'])
        assert receipt['selected_count'] == 4 and receipt['recorded_count'] == 4
        rows = IntakeReceipts(reopened).items(m.matter_id, MEMBER, receipt['receipt_id'])
        assert [r['relative_path'] for r in rows] == [
            'North/report.txt', 'South/report.txt', 'Copies/copy.txt', 'Unsupported/opaque.bin']
        assert rows[-1]['selection_state'] == 'skipped' and rows[-1]['preflight_state'] == 'unsupported'
        assert all(r['transfer_state'] == 'not_received' for r in rows)
        assert receipt['counts']['selected'] == receipt['counts']['included'] + receipt['counts']['skipped'] == 4
        assert receipt['counts']['received'] == 0
    finally:
        reopened.close()


def test_metadata_retry_is_idempotent_and_seal_refuses_missing_or_changed_rows(tmp_path):
    w, m, receipts = seed(tmp_path)
    try:
        receipt = create(receipts, m, 2, (0,))
        assert create(receipts, m, 2, (0,))['receipt_id'] == receipt['receipt_id']
        with pytest.raises(WorkspaceProblem):
            create(receipts, m, 3, (0,))
        files = [descriptor('Folder/first.txt')]
        append(receipts, m, receipt, files)
        append(receipts, m, receipt, files)
        with pytest.raises(WorkspaceProblem):
            receipts.seal(m.matter_id, OWNER, receipt['receipt_id'])
        with pytest.raises(WorkspaceProblem):
            append(receipts, m, receipt, [descriptor('Folder/changed.txt')])
        append(receipts, m, receipt, [descriptor('Folder/opaque.bin')], 1, ['unsupported'])
        receipts.seal(m.matter_id, OWNER, receipt['receipt_id'])
        append(receipts, m, receipt, files)  # Lost successful response is safe after sealing.
        assert receipts.get(m.matter_id, OWNER, receipt['receipt_id'])['recorded_count'] == 2
    finally:
        w.close()


def test_unsafe_empty_unsupported_and_capacity_exclusions_remain_separate(tmp_path):
    w, m, receipts = seed(tmp_path)
    try:
        receipt = create(receipts, m, 5, ())
        append(receipts, m, receipt, [descriptor('../outside-canary.txt'), descriptor('Folder/empty.txt', 0),
            descriptor('Folder/opaque.bin'), descriptor('Folder/large.txt', 1025), descriptor('Folder/left-out.txt')],
            states=['failed', 'failed', 'unsupported', 'over_limit', 'over_limit'])
        receipts.seal(m.matter_id, OWNER, receipt['receipt_id'])
        rows = receipts.items(m.matter_id, OWNER, receipt['receipt_id'])
        assert rows[0]['relative_path'] == '' and 'outside-canary' not in json.dumps(rows)
        assert rows[0]['display_name'] == 'Selected file 1'
        assert rows[1]['expected_size'] == 0
        assert [r['preflight_state'] for r in rows] == ['failed', 'failed', 'unsupported', 'over_limit', 'valid']
        assert all(r['selection_state'] == 'skipped' for r in rows)
        assert rows[-1]['reviewed_state'] == 'over_limit'
        assert 'capacity' in rows[-1]['reviewed_reason']
        summary = receipts.get(m.matter_id, OWNER, receipt['receipt_id'])
        assert summary['counts']['skipped'] == 5 and summary['counts']['received'] == 0
    finally:
        w.close()


def test_seal_rejects_duplicate_included_paths_across_metadata_batches(tmp_path):
    w, m, receipts = seed(tmp_path)
    try:
        receipt = create(receipts, m, 2, (0, 1))
        append(receipts, m, receipt, [descriptor('Folder/report.txt')])
        append(receipts, m, receipt, [descriptor('folder/REPORT.txt')], 1)
        with pytest.raises(WorkspaceProblem, match='repeat'):
            receipts.seal(m.matter_id, OWNER, receipt['receipt_id'])
    finally:
        w.close()


@pytest.mark.parametrize('kind', ['revoked', 'inactive', 'closing', 'other_matter', 'other_writer'])
def test_receipt_authority_is_rechecked_for_reads_and_mutations(tmp_path, kind):
    w, m, receipts = seed(tmp_path)
    try:
        receipt = create(receipts, m, 1, (0,))
        if kind == 'revoked':
            receipt = receipts.create(m.matter_id, MEMBER, selection_key='c' * 32, selection_fingerprint='d' * 64, selected_count=1, eligible_indexes=[0], collection_name='Member selection')
        target = m.matter_id
        actor = OWNER
        if kind == 'other_matter':
            target = w.create_matter('Other generated matter', '', OWNER).matter_id
        elif kind == 'other_writer':
            actor = MEMBER
        elif kind == 'revoked':
            w.revoke_member(m.matter_id, MEMBER, OWNER)
            actor = MEMBER
        elif kind == 'inactive':
            with w.connection:
                w.connection.execute('UPDATE workbench_principal SET active=0 WHERE principal_id=?', (OWNER,))
        elif kind == 'closing':
            w.case_lifecycle.begin_exit(m.slug, OWNER, session_id=lifecycle_session(w, OWNER, administrator_override=False).session_id)
        with pytest.raises(KeyError):
            receipts.append(target, actor, receipt['receipt_id'], start=0, files=[descriptor('one.txt')],
                reviewed_states=['valid'], document_limit=1024, media_limit=2048, malware_scan_mode='off', scanner_ready=True)
        if kind != 'other_writer':
            with pytest.raises(KeyError):
                receipts.get(target, actor, receipt['receipt_id'])
    finally:
        w.close()


def test_atomic_upload_binding_is_idempotent_and_preserves_selected_skips(tmp_path):
    w, m, receipts = seed(tmp_path)
    try:
        receipt = ready(receipts, m)
        files = [{'display_name': 'report.txt', 'relative_path': 'North/report.txt', 'media_type': 'text/plain', 'expected_size': 48}]
        kwargs = {'intake_receipt_id': receipt['receipt_id'], 'intake_ordinals': [0]}
        session, items = w.create_upload_session(m.matter_id, OWNER, 'Generated folder', files, **kwargs)
        repeated, again = w.create_upload_session(m.matter_id, OWNER, 'Generated folder', files, **kwargs)
        assert repeated.upload_session_id == session.upload_session_id and again == items
        with pytest.raises(WorkspaceProblem):
            w.create_upload_session(m.matter_id, OWNER, 'Generated folder', files, intake_receipt_id=receipt['receipt_id'], intake_ordinals=[3])
        w.set_upload_item_offset(m.matter_id, OWNER, session.upload_session_id, items[0].upload_item_id, 0, 48)
        current = receipts.get(m.matter_id, OWNER, receipt['receipt_id'])
        assert current['counts']['selected'] == 4 and current['counts']['received'] == 1
        rows = receipts.items(m.matter_id, OWNER, receipt['receipt_id'])
        assert rows[0]['transfer_state'] == 'received' and rows[-1]['selection_state'] == 'skipped'
    finally:
        w.close()


def test_concurrent_metadata_creation_and_upload_binding_make_one_record(tmp_path):
    w, m, receipts = seed(tmp_path)
    peer = WorkspaceStore(w.path)
    barrier = threading.Barrier(2)
    def begin(store):
        barrier.wait(timeout=5)
        return create(IntakeReceipts(store), m, 1, (0,))['receipt_id']
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(begin, [w, peer]))
        assert ids[0] == ids[1]
        receipt = receipts.get(m.matter_id, OWNER, ids[0])
        append(receipts, m, receipt, [descriptor('one.txt')])
        receipts.seal(m.matter_id, OWNER, ids[0])
        def bind(store):
            barrier.wait(timeout=5)
            return store.create_upload_session(m.matter_id, OWNER, 'Generated folder',
                [{'display_name': 'one.txt', 'relative_path': 'one.txt', 'media_type': 'text/plain', 'expected_size': 48}],
                intake_receipt_id=ids[0], intake_ordinals=[0])[0].upload_session_id
        with ThreadPoolExecutor(max_workers=2) as pool:
            sessions = list(pool.map(bind, [w, peer]))
        assert sessions[0] == sessions[1]
    finally:
        peer.close(); w.close()


def test_incomplete_receipt_export_is_truthful_and_owner_purge_removes_inventory(tmp_path):
    w, m, receipts = seed(tmp_path)
    try:
        receipt = create(receipts, m, 3, ())
        append(receipts, m, receipt, [descriptor('Generated/opaque.bin')], states=['unsupported'])
        exported = receipts.export(m.matter_id, OWNER)
        assert exported[0]['selected_count'] == 3 and exported[0]['recorded_count'] == 1
        assert exported[0]['state'] == 'recording' and exported[0]['counts']['unrecorded'] == 2
        assert 'Generated/opaque.bin' in json.dumps(exported)
        _, purge = claim_synthetic_exit(w, m, OWNER)
        w.complete_matter_purge(m.matter_id, purge.purge_id)
        for table in ['workbench_intake_receipt', 'workbench_intake_item', 'workbench_intake_transfer']:
            assert w.connection.execute(f'SELECT count(*) FROM {table} WHERE matter_id=?', (m.matter_id,)).fetchone()[0] == 0
    finally:
        w.close()


@pytest.mark.parametrize('ordinals', [[{}], [[]], [True], [-1], [10_000]])
def test_invalid_binding_ordinals_fail_without_partial_sessions(tmp_path, ordinals):
    w, m, receipts = seed(tmp_path)
    try:
        receipt = ready(receipts, m)
        files = [{'display_name': 'report.txt', 'relative_path': 'North/report.txt', 'media_type': 'text/plain', 'expected_size': 48}]
        with pytest.raises(WorkspaceProblem):
            w.create_upload_session(m.matter_id, OWNER, 'Generated folder', files,
                intake_receipt_id=receipt['receipt_id'], intake_ordinals=ordinals)
        assert w.connection.execute('SELECT count(*) FROM workbench_upload_session').fetchone()[0] == 0
    finally:
        w.close()


def test_receipt_uses_the_same_canonical_path_as_upload_admission(tmp_path):
    w, m, receipts = seed(tmp_path)
    try:
        receipt = create(receipts, m, 1, (0,))
        append(receipts, m, receipt, [descriptor('Folder//report.txt')])
        receipts.seal(m.matter_id, OWNER, receipt['receipt_id'])
        files = [{'display_name': 'report.txt', 'relative_path': 'Folder/report.txt', 'media_type': 'text/plain', 'expected_size': 48}]
        session, items = w.create_upload_session(m.matter_id, OWNER, 'Generated folder', files,
            intake_receipt_id=receipt['receipt_id'], intake_ordinals=[0])
        assert items[0].relative_path == receipts.items(m.matter_id, OWNER, receipt['receipt_id'])[0]['relative_path']
    finally:
        w.close()


def test_recording_receipt_counts_as_matter_activity_without_blocking_close(tmp_path, monkeypatch):
    w, m, receipts = seed(tmp_path)
    try:
        later = '2099-01-01T00:00:00+00:00'
        monkeypatch.setattr(w, '_now', lambda: later)
        create(receipts, m, 1, ())
        assert w.matter_activity_at(m.matter_id) == later
        assert not any(w.active_matter_work_counts(m.matter_id).values())
    finally:
        w.close()


def test_legacy_resume_adopts_prior_batches_and_offsets_once(tmp_path):
    w, m, receipts = seed(tmp_path)
    try:
        files = [{'display_name': 'report.txt', 'relative_path': 'North/report.txt', 'media_type': 'text/plain', 'expected_size': 48}]
        first, first_items = w.create_upload_session(m.matter_id, OWNER, 'Generated folder', files)
        second_files = [{**files[0], 'relative_path': 'South/report.txt'}]
        second, second_items = w.create_upload_session(m.matter_id, OWNER, 'Generated folder', second_files, collection_id=first.collection_id)
        w.set_upload_item_offset(m.matter_id, OWNER, first.upload_session_id, first_items[0].upload_item_id, 0, 48)
        w.set_upload_item_offset(m.matter_id, OWNER, second.upload_session_id, second_items[0].upload_item_id, 0, 17)
        receipt = ready(receipts, m)
        for _ in range(2):
            adopted = receipts.adopt_upload(m.matter_id, OWNER, receipt['receipt_id'], [1], second_files,
                second.upload_session_id, collection_id=first.collection_id)
            assert adopted == second.upload_session_id
        current = receipts.get(m.matter_id, OWNER, receipt['receipt_id'])
        assert current['counts']['received'] == 1 and current['counts']['partial'] == 1
        assert [r['received_size'] for r in receipts.items(m.matter_id, OWNER, receipt['receipt_id'])] == [48, 17, 0, 0]
        assert w.connection.execute('SELECT count(*) FROM workbench_intake_transfer').fetchone()[0] == 2
        other = create(receipts, m, key='f' * 32)
        append(receipts, m, other, [descriptor('North/report.txt'), descriptor('South/report.txt'), descriptor('Copies/copy.txt'), descriptor('Unsupported/opaque.bin')], states=['valid']*3 + ['unsupported'])
        receipts.seal(m.matter_id, OWNER, other['receipt_id'])
        with pytest.raises(WorkspaceProblem):
            receipts.adopt_upload(m.matter_id, OWNER, other['receipt_id'], [1], second_files, second.upload_session_id)
    finally:
        w.close()


def test_receipt_export_and_totals_use_one_snapshot_across_connections(tmp_path, monkeypatch):
    w, m, receipts = seed(tmp_path)
    peer = WorkspaceStore(w.path)
    try:
        receipt = ready(receipts, m)
        session, items = w.create_upload_session(m.matter_id, OWNER, 'Generated folder',
            [{'display_name': 'report.txt', 'relative_path': 'North/report.txt', 'media_type': 'text/plain', 'expected_size': 48}],
            intake_receipt_id=receipt['receipt_id'], intake_ordinals=[0])
        original = receipts.items
        def during_read(*args, **kwargs):
            peer.set_upload_item_offset(m.matter_id, OWNER, session.upload_session_id, items[0].upload_item_id, 0, 48)
            return original(*args, **kwargs)
        monkeypatch.setattr(receipts, 'items', during_read)
        snapshot = receipts.snapshot(m.matter_id, OWNER, receipt['receipt_id'])
        assert snapshot['counts']['received'] == 0
        assert snapshot['items'][0]['transfer_state'] == 'not_received'
        assert receipts.get(m.matter_id, OWNER, receipt['receipt_id'])['counts']['received'] == 1
    finally:
        peer.close(); w.close()


def test_ten_thousand_selection_rows_survive_batched_recording_and_export(tmp_path):
    w, m, receipts = seed(tmp_path)
    try:
        receipt = create(receipts, m, 10_000, ())
        for start in range(0, 10_000, 2_000):
            files = [descriptor(f'Generated/row-{i:05d}.bin') for i in range(start, start + 2_000)]
            append(receipts, m, receipt, files, start, ['unsupported'] * len(files))
        receipts.seal(m.matter_id, OWNER, receipt['receipt_id'])
        snapshot = receipts.snapshot(m.matter_id, OWNER, receipt['receipt_id'])
        assert snapshot['counts']['skipped'] == len(snapshot['items']) == 10_000
        assert snapshot['items'][-1]['relative_path'] == 'Generated/row-09999.bin'
        exported = receipts.export(m.matter_id, OWNER)
        assert len(exported[0]['items']) == 10_000
    finally:
        w.close()


@pytest.mark.parametrize('file', [None, {'name': []}, {'name': 'record.txt', 'size': True},
    {'name': 'record.txt', 'size': 2**53}, {'name': 'record.txt', 'extra': {'nested': 'invalid'}}])
def test_malformed_metadata_never_partly_records_a_batch(tmp_path, file):
    w, m, receipts = seed(tmp_path)
    try:
        receipt = create(receipts, m, 2, ())
        with pytest.raises(WorkspaceProblem):
            append(receipts, m, receipt, [descriptor('Generated/okay.bin'), file], states=['unsupported', 'failed'])
        assert receipts.get(m.matter_id, OWNER, receipt['receipt_id'])['recorded_count'] == 0
    finally:
        w.close()


def test_oversized_metadata_is_rejected_before_writes(tmp_path):
    w, m, receipts = seed(tmp_path)
    try:
        receipt = create(receipts, m, 1, ())
        with pytest.raises(WorkspaceProblem, match='too large'):
            append(receipts, m, receipt, [descriptor('x' * (6 * 1024 * 1024))], states=['failed'])
        assert receipts.get(m.matter_id, OWNER, receipt['receipt_id'])['recorded_count'] == 0
    finally:
        w.close()



def test_empty_incomplete_receipt_exports_do_not_silently_omit_selected_count(tmp_path):
    from case_intelligence.intake_receipt_exports import export_intake_receipt
    import csv
    import io
    w, m, receipts = seed(tmp_path)
    try:
        receipt = create(receipts, m, 3, ())
        snapshot = receipts.snapshot(m.matter_id, OWNER, receipt['receipt_id'])
        rows = list(csv.reader(io.StringIO(export_intake_receipt(snapshot, 'csv').body.decode('utf-8-sig'))))
        assert rows[1][1:5] == ['3', '0', '3', '']
        assert 'No file rows were recorded' in rows[1][rows[0].index('Reason')]
        assert '3 not yet recorded' in export_intake_receipt(snapshot, 'markdown').body.decode()
    finally:
        w.close()


def test_receipt_bundle_limits_refuse_complete_claim_without_omitting_rows(tmp_path, monkeypatch):
    w, m, receipts = seed(tmp_path)
    try:
        ready(receipts, m)
        monkeypatch.setattr('case_intelligence.work_product_exports.MAX_BUNDLE_UNCOMPRESSED_BYTES', 20)
        with pytest.raises(WorkspaceProblem, match='No complete bundle'):
            receipts.export(m.matter_id, OWNER)
        assert len(receipts.recent(m.matter_id, OWNER)) == 1
    finally:
        w.close()


def test_metadata_batch_limit_counts_utf8_bytes_for_valid_unicode_paths(tmp_path):
    from case_intelligence.intake_receipts import MAX_METADATA_BATCH_BYTES
    w, m, receipts = seed(tmp_path)
    try:
        # Legal short path components, with a full batch that fits the browser's
        # UTF-8 body limit but exceeds it when unnecessarily ASCII-escaped.
        folder = '/'.join(['界' * 70] * 8)
        files = [descriptor(f'{folder}/record-{index}.txt') for index in range(2000)]
        states = ['valid'] * len(files)
        payload = {'start': 0, 'files': files, 'reviewed_states': states}
        assert len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) < MAX_METADATA_BATCH_BYTES
        assert len(json.dumps(payload, ensure_ascii=True).encode('utf-8')) > MAX_METADATA_BATCH_BYTES
        receipt = create(receipts, m, len(files), range(len(files)))
        append(receipts, m, receipt, files, states=states)
        saved = receipts.seal(m.matter_id, OWNER, receipt['receipt_id'])
        assert saved['state'] == 'ready' and saved['recorded_count'] == len(files)
        rows = receipts.items(m.matter_id, OWNER, receipt['receipt_id'], offset=1900, limit=100)
        assert rows[-1]['relative_path'] == files[-1]['relative_path']
    finally:
        w.close()


def test_capacity_exclusion_survives_recheck_restart_and_all_exports(tmp_path):
    from case_intelligence.intake_receipt_exports import export_intake_receipt
    w, m, receipts = seed(tmp_path)
    try:
        receipt = create(receipts, m, 2, (0,))
        append(receipts, m, receipt, [descriptor('Generated/first.txt', 48),
            descriptor('Generated/later.txt', 48)], states=['valid', 'over_limit'])
        receipts.seal(m.matter_id, OWNER, receipt['receipt_id'])
    finally:
        w.close()
    reopened = WorkspaceStore(tmp_path / 'workspace.sqlite')
    try:
        snapshot = IntakeReceipts(reopened).snapshot(m.matter_id, OWNER, receipt['receipt_id'])
        skipped = snapshot['items'][1]
        assert skipped['selection_state'] == 'skipped' and skipped['preflight_state'] == 'valid'
        assert skipped['reviewed_state'] == 'over_limit'
        assert 'capacity' in skipped['reviewed_reason']
        assert snapshot['counts']['included'] == snapshot['counts']['skipped'] == 1
        for format_name in ('json', 'markdown', 'csv'):
            exported = export_intake_receipt(snapshot, format_name).body.decode('utf-8-sig')
            assert 'capacity' in exported and 'later.txt' in exported
    finally:
        reopened.close()
