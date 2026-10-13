"""Synthetic cited-context response admission, completion and failure races."""
from __future__ import annotations

import asyncio

import anyio
import pytest
from starlette.templating import Jinja2Templates

from tests.case_lifecycle_web_helpers import claim_bench_exit
from case_intelligence import answer_cited_context
from case_intelligence.workspace_store import WorkspaceProblem
from tests.test_answer_cited_context import context_path
from tests.test_full_text_response_lifecycle import asgi_download
from tests.test_saved_answer_note_preview import ACTOR, long_answer  # noqa: F401


@pytest.mark.parametrize("format_name", ["html", "json"])
def test_cited_context_holds_closure_until_last_body_send(long_answer, format_name):
    client, bench, matter, document, _, _, _ = long_answer
    source_path = bench.source_store(matter).source_path(document.document_id)
    observed = []

    async def send(message):
        if message["type"] not in {"http.response.start", "http.response.body"}:
            return
        # Let the application's inner response finish producing its body while
        # this outer ASGI send still has not completed. Background cleanup alone
        # must not release deletion admission through the middleware buffer.
        await anyio.sleep(.01)
        assert bench._active_matter_response_count(matter.matter_id) == 1

        def close_matter():
            with pytest.raises(WorkspaceProblem, match="work-product downloads"):
                bench.begin_matter_purge(matter.slug, ACTOR, matter.display_name)

        await anyio.to_thread.run_sync(close_matter)
        assert source_path.exists()
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == "active"
        observed.append((message["type"], bool(message.get("body")), message.get("more_body", False)))

    messages = asyncio.run(asgi_download(client.app,
        context_path(long_answer) + "?format=" + format_name, send_hook=send))
    assert next(item for item in messages if item["type"] == "http.response.start")["status"] == 200
    assert any(body for kind, body, _ in observed if kind == "http.response.body")
    assert observed[-1][0] == "http.response.body" and observed[-1][2] is False
    assert bench._active_matter_response_count(matter.matter_id) == 0
    claimed, lifecycle = claim_bench_exit(bench, matter, ACTOR)
    assert bench.execute_matter_purge(claimed, lifecycle).state == "deleted"
    assert not source_path.exists()


@pytest.mark.parametrize("format_name", ["html", "json"])
@pytest.mark.parametrize("failure", ["start", "body", "final"])
def test_failed_cited_context_send_releases_lease(long_answer, format_name, failure):
    client, bench, matter, _, _, _, _ = long_answer
    attempted = []

    async def send(message):
        at_failure = (
            failure == "start" and message["type"] == "http.response.start"
            or failure == "body" and message["type"] == "http.response.body" and message.get("body")
            or failure == "final" and message["type"] == "http.response.body" and not message.get("more_body", False)
        )
        if at_failure:
            await anyio.sleep(.01)
            assert bench._active_matter_response_count(matter.matter_id) == 1
            attempted.append(True)
            raise OSError("Synthetic comparison client disconnected")

    with pytest.raises(OSError, match="Synthetic comparison client disconnected"):
        asyncio.run(asgi_download(client.app,
            context_path(long_answer) + "?format=" + format_name, send_hook=send))
    assert attempted
    assert bench._active_matter_response_count(matter.matter_id) == 0
    assert claim_bench_exit(bench, matter, ACTOR)[1].state == "purging"


@pytest.mark.parametrize("format_name", ["html", "json"])
def test_cited_context_resolution_failure_releases_admitted_lease(long_answer, monkeypatch, format_name):
    client, bench, matter, _, _, _, _ = long_answer

    def fail_resolution(*args, **kwargs):
        assert bench._active_matter_response_count(matter.matter_id) == 1
        raise RuntimeError("Synthetic comparison resolution failure")

    monkeypatch.setattr(answer_cited_context, "saved_cited_context", fail_resolution)
    with pytest.raises(RuntimeError, match="Synthetic comparison resolution failure"):
        client.get(context_path(long_answer), params={"format": format_name})
    assert bench._active_matter_response_count(matter.matter_id) == 0


def test_cited_context_render_failure_releases_admitted_lease(long_answer, monkeypatch):
    client, bench, matter, _, _, _, _ = long_answer

    def fail_render(*args, **kwargs):
        assert bench._active_matter_response_count(matter.matter_id) == 1
        raise RuntimeError("Synthetic comparison rendering failure")

    monkeypatch.setattr(Jinja2Templates, "TemplateResponse", fail_render)
    with pytest.raises(RuntimeError, match="Synthetic comparison rendering failure"):
        client.get(context_path(long_answer))
    assert bench._active_matter_response_count(matter.matter_id) == 0


@pytest.mark.parametrize("format_name", ["html", "json"])
def test_cancelled_cited_context_send_releases_lease(long_answer, format_name):
    client, bench, matter, _, _, _, _ = long_answer
    attempted = []

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            await anyio.sleep(.01)
            assert bench._active_matter_response_count(matter.matter_id) == 1
            attempted.append(True)
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(asgi_download(client.app,
            context_path(long_answer) + "?format=" + format_name, send_hook=send))
    assert attempted
    assert bench._active_matter_response_count(matter.matter_id) == 0


@pytest.mark.parametrize("format_name", ["html", "json"])
def test_invalid_cited_context_releases_untransferred_lease(long_answer, format_name):
    client, bench, matter, _, _, _, _ = long_answer
    response = client.get(context_path(long_answer, passage="claim-999"), params={"format": format_name})
    assert response.status_code == 404
    assert bench._active_matter_response_count(matter.matter_id) == 0
