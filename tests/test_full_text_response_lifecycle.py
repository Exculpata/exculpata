from dataclasses import replace
"""Synthetic ledger download, administrator-read and response-lifecycle races."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import csv
import io
import json
import sqlite3
import threading

import anyio
import pytest
from fastapi.testclient import TestClient

from case_intelligence import full_text_review, workbench as workbench_module
from case_intelligence.full_text_review import FullTextReviewLedger, iter_text_export
from case_intelligence.pilot_uploads import PilotUnit
from tests.test_full_text_review import frozen
from tests.test_full_text_reports_integration import completed_text_run
from tests.test_report_review_basis import ACTOR, workspace
from tests.test_case_lifecycle_web import case
from tests.test_matter_management import ADMIN, OWNER, OTHER, _app, _headers, _csrf, _principal_id


@pytest.mark.parametrize("format_name", ["json", "csv"])
def test_export_next_and_close_can_switch_actual_worker_threads(frozen, format_name):
    store, matter, run, decision = frozen
    ledger = FullTextReviewLedger(store)
    ledger.inventory(run, decision, [PilotUnit(number, "Synthetic text") for number in range(1, 206)],
                     current_source=lambda: True)
    stream = iter_text_export(store, matter.matter_id, ACTOR, run.run_id, format_name)
    def advance():
        try:
            return next(stream)
        except StopIteration:
            return None
    with ThreadPoolExecutor(max_workers=1) as first, ThreadPoolExecutor(max_workers=1) as second:
        assert first.submit(threading.get_ident).result() != second.submit(threading.get_ident).result()
        chunks = []
        for index in range(1_000):
            chunk = (first if index % 2 == 0 else second).submit(advance).result(timeout=2)
            if chunk is None:
                break
            chunks.append(chunk)
        else:
            pytest.fail("Synthetic ledger did not finish")
        second.submit(stream.close).result(timeout=2)
    body = b"".join(chunks).decode()
    records = json.loads(body)["records"] if format_name == "json" else [
        json.loads(row["Saved record JSON"]) for row in csv.DictReader(io.StringIO(body))]
    assert len([item for item in records if item["record_type"] == "unit"]) == 205
    assert len([item for item in records if item["record_type"] == "range"]) == 205


def track_readonly_connections(monkeypatch):
    connections = []
    connect = sqlite3.connect
    def tracked(database, *args, **kwargs):
        result = connect(database, *args, **kwargs)
        if "?mode=ro" in str(database):
            connections.append(result)
        return result
    monkeypatch.setattr(full_text_review.sqlite3, "connect", tracked)
    return connections


def assert_closed(connections):
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


async def asgi_download(app, path, *, send_hook=None, disconnect=False, headers=()):
    sent = []
    body_started = asyncio.Event()
    received = False
    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        if disconnect:
            await body_started.wait()
            return {"type": "http.disconnect"}
        await anyio.sleep_forever()
    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            body_started.set()
        if send_hook is not None:
            await send_hook(message)
    path, _, query = path.partition("?")
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3" if disconnect else "2.4"},
             "http_version": "1.1", "method": "GET", "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": query.encode(), "root_path": "",
             "headers": [(b"host", b"testserver"), *headers], "client": ("127.0.0.1", 41000),
             "server": ("testserver", 80)}
    await app(scope, receive, send)
    return sent


def test_active_ledger_download_blocks_closure_until_final_body(case, monkeypatch):
    client, bench, matter, _, _, _, csrf = case
    run, _ = completed_text_run(bench, matter, ["The amber bicycle arrived."])
    connections = track_readonly_connections(monkeypatch)
    attempted = []
    async def send(message):
        if message["type"] != "http.response.body" or not message.get("body") or attempted:
            return
        attempted.append(True)
        assert bench._active_matter_response_count(matter.matter_id) == 1
        response = await anyio.to_thread.run_sync(lambda: client.post(f"/matters/{matter.slug}/close/begin", data={
            "csrf_token": csrf, "confirmed_name": matter.display_name, "acknowledge": "yes"}, follow_redirects=False))
        assert response.status_code == 303
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == "active"
    messages = asyncio.run(asgi_download(client.app,
        f"/matters/{matter.slug}/full-review/{run.run_id}/text/export", send_hook=send,
        headers=((b"cookie", "; ".join(f"{key}={value}" for key, value in client.cookies.items()).encode()),)))
    assert attempted
    body = b"".join(item.get("body", b"") for item in messages)
    assert any(row["record_type"] == "range" for row in json.loads(body)["records"])
    assert bench._active_matter_response_count(matter.matter_id) == 0
    assert_closed(connections)
    assert client.post(f"/matters/{matter.slug}/close/begin", data={
        "csrf_token": csrf, "confirmed_name": matter.display_name, "acknowledge": "yes"}, follow_redirects=False).status_code == 303
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == "closing"


@pytest.mark.parametrize("failure", ["start", "send", "generator", "disconnect"])
def test_failed_ledger_stream_closes_reader_and_releases_lease(workspace, monkeypatch, failure):
    client, bench, matter = workspace
    run, _ = completed_text_run(bench, matter, ["The amber bicycle arrived."])
    connections = track_readonly_connections(monkeypatch)
    if failure == "generator":
        original = workbench_module.iter_text_export
        def broken(*args, **kwargs):
            stream = original(*args, **kwargs)
            try:
                yield next(stream)
                raise RuntimeError("Synthetic ledger producer failure")
            finally:
                stream.close()
        monkeypatch.setattr(workbench_module, "iter_text_export", broken)
    async def send(message):
        if (failure == "start" and message["type"] == "http.response.start") or (
            failure == "send" and message["type"] == "http.response.body" and message.get("body")
        ):
            raise OSError("Synthetic client connection lost")
    request = asgi_download(client.app, f"/matters/{matter.slug}/full-review/{run.run_id}/text/export",
                            send_hook=send, disconnect=failure == "disconnect")
    if failure == "disconnect":
        asyncio.run(request)
    else:
        with pytest.raises(Exception):
            asyncio.run(request)
    assert bench._active_matter_response_count(matter.matter_id) == 0
    if failure == "start":
        assert not connections
    else:
        assert_closed(connections)
    assert not bench.workspace._text_review_export_leases
    assert FullTextReviewLedger(bench.workspace).delete(matter.matter_id, ACTOR, run.run_id)


def test_unknown_ledger_releases_untransferred_lease(workspace):
    client, bench, matter = workspace
    response = client.get(f"/matters/{matter.slug}/full-review/review-run-{'f' * 32}/text/export")
    assert response.status_code == 404
    assert bench._active_matter_response_count(matter.matter_id) == 0


@pytest.mark.parametrize("run_state", ["running", "cancelled"])
@pytest.mark.parametrize("owner_change", ["inactive", "revoked"])
def test_nonmember_admin_reads_ledger_as_self_after_owner_loses_access(tmp_path, monkeypatch, owner_change, run_state):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    with TestClient(_app(tmp_path), base_url="https://recordbench.example.test") as client:
        assert client.get("/auth/login", headers=_headers(OWNER), follow_redirects=False).status_code == 303
        owner_id = _principal_id(client, OWNER)
        bench = client.app.state.workbench
        matter = bench.workspace.create_matter("Synthetic administrator ledger", "Synthetic", owner_id)
        # Reuse the same synthetic processing helper with this fixture's owner.
        import tests.test_full_text_reports_integration as fixtures
        monkeypatch.setattr(fixtures, "ACTOR", owner_id)
        run, documents = completed_text_run(bench, matter, ["The amber bicycle arrived."])
        with bench.workspace._lock, bench.workspace.connection:
            if owner_change == "inactive":
                bench.workspace.connection.execute("UPDATE workbench_principal SET active=0 WHERE principal_id=?", (owner_id,))
            else:
                bench.workspace.connection.execute("UPDATE workbench_matter_membership SET state='revoked' WHERE matter_id=? AND principal_id=?", (matter.matter_id, owner_id))
        client.cookies.clear()
        assert client.get("/auth/login", headers=_headers(ADMIN), follow_redirects=False).status_code == 303
        admin_id = _principal_id(client, ADMIN)
        seen = []
        original = bench.workspace.review_run
        def checked(matter_id, actor_id, run_id, **kwargs):
            seen.append((actor_id, kwargs.get("administrator_override", False)))
            return replace(original(matter_id, actor_id, run_id, **kwargs), state=run_state)
        monkeypatch.setattr(bench.workspace, "review_run", checked)
        base = f"/matters/{matter.slug}/full-review/{run.run_id}"
        assert client.get(base + "/status", headers=_headers(ADMIN)).status_code == 200
        page = client.get(base + "/text", headers=_headers(ADMIN))
        assert page.status_code == 200 and "Text-analysis coverage" in page.text
        assert "read-only review" in page.text
        assert "New run or edit rule" not in page.text
        assert f'action="{base}/cancel"' not in page.text
        assert f'action="{base}/retry"' not in page.text
        exported = client.get(base + "/text/export", headers=_headers(ADMIN))
        assert exported.status_code == 200 and exported.json()["records"]
        assert seen and all(actor == admin_id and override for actor, override in seen)
        ledger = FullTextReviewLedger(bench.workspace)
        with pytest.raises(KeyError):
            ledger.coverage(matter.matter_id, admin_id, run.run_id)
        assert ledger.citation(matter.matter_id, admin_id, run.run_id, documents[0].document_id, 1,
                               administrator_override=True)
        events = bench.workspace.connection.execute("SELECT actor_principal_id FROM workbench_audit_event WHERE action IN ('full_review.text_open','full_review.text_export')").fetchall()
        assert events and all(row[0] == admin_id for row in events)
        # Administrator read override does not grant creator-only deletion.
        assert client.post(base + "/text/delete", headers=_headers(ADMIN),
                           data={"csrf_token": _csrf(page.text)}, follow_redirects=False).status_code == 403
        assert ledger.enabled(run.run_id)
        client.cookies.clear()
        assert client.get("/auth/login", headers=_headers(OTHER), follow_redirects=False).status_code == 303
        assert client.get(base + "/text", headers=_headers(OTHER)).status_code == 404
        assert client.get(base + "/text/export", headers=_headers(OTHER)).status_code == 404


@pytest.mark.parametrize("viewer,is_member,can_delete", [
    (OWNER, True, True),
    (OTHER, True, False),
    (ADMIN, True, False),
    (ADMIN, False, False),
])
def test_terminal_ledger_only_offers_delete_to_member_creator(
    tmp_path, monkeypatch, viewer, is_member, can_delete
):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    with TestClient(_app(tmp_path), base_url="https://recordbench.example.test") as client:
        assert client.get("/auth/login", headers=_headers(OWNER), follow_redirects=False).status_code == 303
        owner_id = _principal_id(client, OWNER)
        bench = client.app.state.workbench
        matter = bench.workspace.create_matter("Synthetic shared text ledger", "Synthetic", owner_id)
        import tests.test_full_text_reports_integration as fixtures
        monkeypatch.setattr(fixtures, "ACTOR", owner_id)
        run, _ = completed_text_run(bench, matter, ["The amber bicycle arrived."])
        client.cookies.clear()
        assert client.get("/auth/login", headers=_headers(viewer), follow_redirects=False).status_code == 303
        viewer_id = _principal_id(client, viewer)
        if is_member and viewer != OWNER:
            bench.workspace.add_member(matter.matter_id, viewer_id, owner_id)
        base = f"/matters/{matter.slug}/full-review/{run.run_id}/text"
        page = client.get(base, headers=_headers(viewer))
        assert page.status_code == 200
        assert (f'action="{base}/delete"' in page.text) is can_delete
        assert ("Delete saved review" in page.text) is can_delete
        assert "Download complete JSON" in page.text
        if can_delete:
            response = client.post(base + "/delete", headers=_headers(viewer),
                data={"csrf_token": _csrf(page.text)}, follow_redirects=False)
            assert response.status_code == 303
            assert not FullTextReviewLedger(bench.workspace).enabled(run.run_id)
