"""Synthetic run deletion races with direct and ASGI ledger consumers."""

from tests.fixture_write import fixture_write
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import threading
import csv
import io
import json

import anyio
import pytest

from case_intelligence.full_text_review import FullTextReviewLedger, iter_text_export
from case_intelligence.pilot_uploads import PilotUnit
from case_intelligence.workspace_store import WorkspaceProblem
from tests.test_full_text_review import frozen
from tests.test_full_text_reports_integration import completed_text_run
from tests.test_full_text_response_lifecycle import asgi_download
from tests.test_report_review_basis import ACTOR, workspace


def terminal_inventory(frozen):
    store, matter, run, decision = frozen
    ledger = FullTextReviewLedger(store)
    ledger.inventory(run, decision, [PilotUnit(n, 'Synthetic text') for n in range(1, 206)],
                     current_source=lambda: True)
    with fixture_write(store):
        store.connection.execute("UPDATE workbench_review_run SET state='succeeded' WHERE run_id=?", (run.run_id,))
    return store, matter, run, ledger


def decoded_records(body, format_name):
    return json.loads(body)['records'] if format_name == 'json' else [
        json.loads(row['Saved record JSON']) for row in csv.DictReader(io.StringIO(body.decode()))]


@pytest.mark.parametrize('format_name', ['json', 'csv'])
def test_direct_export_blocks_delete_before_prefix_and_across_batches(frozen, format_name):
    store, matter, run, ledger = terminal_inventory(frozen)
    with closing(iter_text_export(store, matter.matter_id, ACTOR, run.run_id, format_name)) as stream:
        chunks = []
        for index in range(1_000):
            if index in (0, 1, 110, 220, 330):
                with pytest.raises(WorkspaceProblem, match='download'):
                    ledger.delete(matter.matter_id, ACTOR, run.run_id)
            try:
                chunks.append(next(stream))
            except StopIteration:
                break
        else:
            pytest.fail('Synthetic export did not finish')
    records = decoded_records(b''.join(chunks), format_name)
    assert sum(row['record_type'] == 'unit' for row in records) == 205
    assert sum(row['record_type'] == 'range' for row in records) == 205
    assert ledger.delete(matter.matter_id, ACTOR, run.run_id).run_id == run.run_id


@pytest.mark.parametrize('started', [False, True])
def test_direct_export_close_releases_even_before_first_next(frozen, started):
    store, matter, run, ledger = terminal_inventory(frozen)
    stream = iter_text_export(store, matter.matter_id, ACTOR, run.run_id)
    if started:
        next(stream)
    with pytest.raises(WorkspaceProblem, match='download'):
        ledger.delete(matter.matter_id, ACTOR, run.run_id)
    stream.close()
    stream.close()
    assert ledger.delete(matter.matter_id, ACTOR, run.run_id)


@pytest.mark.parametrize('format_name', ['json', 'csv'])
def test_http_delete_rejected_before_prefix_and_across_batches(workspace, format_name):
    client, bench, matter = workspace
    run, _ = completed_text_run(bench, matter, ['The amber bicycle arrived.'] * 105)
    other, _ = completed_text_run(bench, matter, ['Synthetic unrelated entry.'])
    ledger = FullTextReviewLedger(bench.workspace)
    sent_count = 0
    attempts = []
    async def send(message):
        nonlocal sent_count
        if message['type'] == 'http.response.start' or sent_count in (1, 110, 220):
            response = await anyio.to_thread.run_sync(lambda: client.post(
                f'/matters/{matter.slug}/full-review/{run.run_id}/text/delete', follow_redirects=False))
            assert response.status_code == 303
            assert 'download' in response.headers['location']
            assert ledger.enabled(run.run_id)
            attempts.append(sent_count)
            if len(attempts) == 1:
                assert ledger.delete(matter.matter_id, ACTOR, other.run_id)
        if message['type'] == 'http.response.body':
            sent_count += 1
    messages = asyncio.run(asgi_download(client.app,
        f'/matters/{matter.slug}/full-review/{run.run_id}/text/export?format={format_name}', send_hook=send))
    records = decoded_records(b''.join(message.get('body', b'') for message in messages), format_name)
    assert len(attempts) == 4
    assert sum(row['record_type'] == 'unit' for row in records) == 105
    assert sum(row['record_type'] == 'range' for row in records) == 105
    assert ledger.delete(matter.matter_id, ACTOR, run.run_id)


def test_export_admission_and_delete_share_lock(frozen, monkeypatch):
    store, matter, run, ledger = terminal_inventory(frozen)
    admitted = threading.Event()
    deleting = threading.Event()
    original = store.review_run
    def checked(*args, **kwargs):
        result = original(*args, **kwargs)
        if threading.current_thread().name.startswith('synthetic-admit'):
            admitted.set()
            assert deleting.wait(2)
        return result
    monkeypatch.setattr(store, 'review_run', checked)
    def delete():
        assert admitted.wait(2)
        deleting.set()
        with pytest.raises(WorkspaceProblem, match='download'):
            ledger.delete(matter.matter_id, ACTOR, run.run_id)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='synthetic-admit') as exporter, ThreadPoolExecutor(max_workers=1) as remover:
        reader = exporter.submit(iter_text_export, store, matter.matter_id, ACTOR, run.run_id)
        removed = remover.submit(delete)
        stream = reader.result(timeout=3)
        try:
            removed.result(timeout=3)
        finally:
            stream.close()
    assert ledger.delete(matter.matter_id, ACTOR, run.run_id)


def test_multiple_downloads_keep_lease_until_last_reader_closes(frozen):
    store, matter, run, ledger = terminal_inventory(frozen)
    first = iter_text_export(store, matter.matter_id, ACTOR, run.run_id)
    second = iter_text_export(store, matter.matter_id, ACTOR, run.run_id)
    try:
        first.close()
        with pytest.raises(WorkspaceProblem, match='download'):
            ledger.delete(matter.matter_id, ACTOR, run.run_id)
    finally:
        first.close()
        second.close()
    assert ledger.delete(matter.matter_id, ACTOR, run.run_id)


@pytest.mark.parametrize('failure', ['connect', 'consumer'])
def test_direct_consumer_failure_releases_run(frozen, monkeypatch, failure):
    from case_intelligence import full_text_review
    store, matter, run, ledger = terminal_inventory(frozen)
    def failed_connect(*args, **kwargs):
        raise OSError('Synthetic snapshot open failure')
    if failure == 'connect':
        monkeypatch.setattr(full_text_review.sqlite3, 'connect', failed_connect)
    with pytest.raises(OSError, match='Synthetic'):
        with closing(iter_text_export(store, matter.matter_id, ACTOR, run.run_id)) as stream:
            next(stream)
            raise OSError('Synthetic consuming writer failure')
    assert not store._text_review_export_leases
    assert ledger.delete(matter.matter_id, ACTOR, run.run_id)


def test_rejected_admission_creates_no_run_lease(frozen):
    store, matter, run, ledger = terminal_inventory(frozen)
    with pytest.raises(ValueError):
        iter_text_export(store, matter.matter_id, ACTOR, run.run_id, 'invalid')
    assert ledger.delete(matter.matter_id, ACTOR, run.run_id)
    with pytest.raises(KeyError):
        iter_text_export(store, matter.matter_id, ACTOR, run.run_id)
    assert not store._text_review_export_leases


def test_response_construction_failure_releases_unstarted_reader(workspace, monkeypatch):
    from case_intelligence import workbench
    client, bench, matter = workspace
    run, _ = completed_text_run(bench, matter, ['The amber bicycle arrived.'])
    def broken_response(*args, **kwargs):
        assert bench.workspace._text_review_export_leases[run.run_id] == 1
        raise RuntimeError('Synthetic response construction failure')
    monkeypatch.setattr(workbench, 'TextLedgerStreamingResponse', broken_response)
    with pytest.raises(RuntimeError, match='Synthetic response'):
        client.get(f'/matters/{matter.slug}/full-review/{run.run_id}/text/export')
    assert not bench.workspace._text_review_export_leases
    assert bench._active_matter_response_count(matter.matter_id) == 0
    assert FullTextReviewLedger(bench.workspace).delete(matter.matter_id, ACTOR, run.run_id)


def test_bundle_byte_limit_releases_ledger_reader(workspace, monkeypatch):
    from case_intelligence import workbench
    client, bench, matter = workspace
    run, _ = completed_text_run(bench, matter, ['The amber bicycle arrived.'])
    original = workbench.iter_text_export
    admitted = []
    def bounded(*args, **kwargs):
        stream = original(*args, **kwargs)
        admitted.append(stream)
        # Reach the real bundle consumer's ledger byte gate after admission.
        monkeypatch.setattr(workbench, 'MAX_BUNDLE_UNCOMPRESSED_BYTES', 1)
        return stream
    monkeypatch.setattr(workbench, 'iter_text_export', bounded)
    response = client.get(f'/matters/{matter.slug}/export')
    assert response.status_code == 409
    assert 'Download the full-text ledger separately' in response.text
    assert len(admitted) == 1 and admitted[0].closed
    assert not bench.workspace._text_review_export_leases
    assert bench._active_matter_response_count(matter.matter_id) == 0
    assert FullTextReviewLedger(bench.workspace).delete(matter.matter_id, ACTOR, run.run_id)


@pytest.mark.parametrize('abort', [False, True])
def test_bundle_snapshot_owns_connection_and_holds_lease_after_stream(frozen, abort):
    import sqlite3
    from contextlib import nullcontext
    from case_intelligence.full_text_review import review_export_snapshot
    store, matter, run, ledger = terminal_inventory(frozen)
    with pytest.raises(RuntimeError, match='Synthetic adjacent export failure') if abort else nullcontext():
        with review_export_snapshot(store, matter.matter_id, ACTOR, run.run_id) as snapshot:
            with closing(iter_text_export(store, matter.matter_id, ACTOR, run.run_id, _snapshot=snapshot)) as stream:
                assert json.loads(b''.join(stream))['records']
            assert snapshot.execute('SELECT 1').fetchone()[0] == 1
            with pytest.raises(WorkspaceProblem, match='download'):
                ledger.delete(matter.matter_id, ACTOR, run.run_id)
            if abort:
                raise RuntimeError('Synthetic adjacent export failure')
    assert not store._text_review_export_leases
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        snapshot.execute('SELECT 1')
    assert ledger.delete(matter.matter_id, ACTOR, run.run_id)


def test_bundle_snapshot_open_failure_releases_lease(frozen, monkeypatch):
    from case_intelligence import full_text_review as module
    store, matter, run, ledger = terminal_inventory(frozen)
    def unavailable(*args, **kwargs):
        raise OSError('Synthetic snapshot unavailable')
    monkeypatch.setattr(module.sqlite3, 'connect', unavailable)
    with pytest.raises(OSError, match='Synthetic snapshot'):
        with module.review_export_snapshot(store, matter.matter_id, ACTOR, run.run_id):
            pytest.fail('Unavailable snapshot was yielded')
    assert not store._text_review_export_leases
    assert ledger.delete(matter.matter_id, ACTOR, run.run_id)
