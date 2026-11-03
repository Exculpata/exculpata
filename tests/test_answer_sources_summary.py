"""Synthetic regressions for saved-answer source summaries in the assistant panel."""
from __future__ import annotations

import html
import io
import json
import re

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app
from tests.fixture_write import fixture_write


@pytest.fixture
def saved_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    app = create_workbench_app(tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="test")
    with TestClient(app) as client:
        client.get("/")
        bench = app.state.workbench
        owner = "synthetic-summary-owner"
        bench.workspace.upsert_principal("test", owner, "Synthetic Owner", owner, preferred_principal_id=owner)
        matter = bench.create_matter("Synthetic source summary", "Generated evidence", owner)
        bench.workspace.add_member(matter.matter_id, "development-taylor-morgan", owner)
        citations = []
        for name, text in (
            ("Inspection note.txt", b"The synthetic gauge showed twelve units.\n" * 80),
            ("Staff recollection.txt", b"The synthetic exercise stopped after the reading.\n" * 80),
        ):
            document = bench.source_store(matter).store_stream(name, "text/plain", io.BytesIO(text))[0]
            bench._sync_source_catalog(matter, (document,))
            citations.append(bench._saved_answer_citation_payload(bench._citation(
                matter, bench._candidate(matter, document, document.parsed_units()[0], 1)
            )))
        conversation = bench.workspace.get_conversation(matter.matter_id)
        message = bench.workspace.append_message(
            matter.matter_id,
            conversation.conversation_id,
            "assistant",
            "Synthetic answer",
            {"kind": "generated", "claims": [
                {"text": "The gauge showed twelve units.", "citations": [citations[0]]},
                {"text": "The exercise then stopped.", "citations": [citations[1]]},
            ], "limitation": None},
        )
        yield client, bench, matter, conversation, message


def _save_payload(bench, message, payload):
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute(
            "UPDATE workbench_message SET payload_json=? WHERE message_id=?",
            (json.dumps(payload), message.message_id),
        )


def _assistant_answer(page: str, message_id: str) -> str:
    marker = f'/messages/{message_id}/notebook/claims/'
    position = page.index(marker)
    start = page.rindex('<article class="assistant-message assistant-exculpata-message">', 0, position)
    return page[start:page.index("</article>", position)]


def _summary(answer: str, heading: str = "Sources cited in this answer") -> str:
    match = re.search(
        rf'<div class="assistant-sources-summary">\s*<strong>{re.escape(heading)}</strong>(.*?)</div>',
        answer,
        re.S,
    )
    assert match is not None
    return match.group(1)


def test_sources_summary_deduplicates_sources_in_first_cited_order(saved_answer):
    client, bench, matter, conversation, message = saved_answer
    first, second = [dict(claim) for claim in message.payload["claims"]]
    repeated = dict(first["citations"][0], location="Page 2")
    claims = [first, second, {"text": "A later reading was recorded.", "citations": [repeated]}]
    _save_payload(bench, message, dict(message.payload, claims=claims))

    page = client.get(f"/matters/{matter.slug}/assistant", params={"conversation": conversation.conversation_id})
    answer = _assistant_answer(page.text, message.message_id)
    summary = _summary(answer)

    entries = re.findall(r"<li>(.*?)</li>", summary, re.S)
    assert len(entries) == 2
    assert "Inspection note.txt" in entries[0]
    assert "Staff recollection.txt" in entries[1]
    original_location = first["citations"][0]["location"]
    assert original_location in entries[0] and "Page 2" in entries[0]
    assert entries[0].count(original_location) == entries[0].count("Page 2") == 1
    assert "Cited in statements 1, 3" in entries[0]
    assert "Cited in statement 2" in entries[1]


def test_summary_location_links_equal_the_statement_links(saved_answer):
    client, _bench, matter, conversation, message = saved_answer
    answer = _assistant_answer(
        client.get(f"/matters/{matter.slug}/assistant", params={"conversation": conversation.conversation_id}).text,
        message.message_id,
    )
    statement_links = re.findall(r'<div class="cited-context-item">\s*<a href="([^"]+)">', answer)
    summary_links = re.findall(r'<a href="([^"]+)">[^<]+</a>', _summary(answer))
    assert [html.unescape(link) for link in summary_links] == [html.unescape(link) for link in statement_links]


def test_limitation_only_citations_have_a_separate_summary(saved_answer):
    client, bench, matter, conversation, message = saved_answer
    citation = dict(message.payload["claims"][0]["citations"][0])
    payload = dict(
        message.payload,
        claims=[{"text": "No sourced statement is included.", "citations": []}],
        limitation={"text": "The later reading was not available.", "citations": [citation]},
    )
    _save_payload(bench, message, payload)

    answer = _assistant_answer(
        client.get(f"/matters/{matter.slug}/assistant", params={"conversation": conversation.conversation_id}).text,
        message.message_id,
    )
    assert "Sources cited in this answer" not in answer
    limitation_summary = _summary(answer, "Cited in the limitation")
    assert "Inspection note.txt" in limitation_summary
    assert citation["location"] in limitation_summary


def test_answer_without_citations_has_no_sources_summary(saved_answer):
    client, bench, matter, conversation, message = saved_answer
    claims = [{"text": "No sourced statement is included.", "citations": []}]
    _save_payload(bench, message, dict(message.payload, claims=claims, limitation=None))

    answer = _assistant_answer(
        client.get(f"/matters/{matter.slug}/assistant", params={"conversation": conversation.conversation_id}).text,
        message.message_id,
    )
    assert "assistant-sources-summary" not in answer


def test_sources_summary_escapes_source_names(saved_answer):
    client, bench, matter, conversation, message = saved_answer
    claims = [dict(message.payload["claims"][0])]
    claims[0] = dict(claims[0], citations=[dict(claims[0]["citations"][0], source_name="Notes <b>draft</b>")])
    _save_payload(bench, message, dict(message.payload, claims=claims))

    answer = _assistant_answer(
        client.get(f"/matters/{matter.slug}/assistant", params={"conversation": conversation.conversation_id}).text,
        message.message_id,
    )
    summary = _summary(answer)
    assert "Notes &lt;b&gt;draft&lt;/b&gt;" in summary
    assert "Notes <b>draft</b>" not in summary
