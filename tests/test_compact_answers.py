"""Synthetic regressions: answers read first, cite with numbers, and caveat once."""
from __future__ import annotations

import html
import re

from case_intelligence.answer_presentation import GENERATED_REVIEW_NOTICE
from tests.test_saved_answer_report_support import cedar, save_answer  # noqa: F401


def _answer_html(page: str, message_id: str) -> str:
    start = page.index(f'id="answer-support-{message_id}"')
    article = page.rindex("<article", 0, start)
    return page[article:page.index("</article>", start)]


def test_claims_carry_numbered_links_to_each_cited_passage(cedar, monkeypatch):  # noqa: F811
    client, _bench, matter, _documents = cedar
    conversation, message, _citations = save_answer(cedar, monkeypatch, ("pdf", "transcript"))
    page = client.get(f"/matters/{matter.slug}", params={"conversation": conversation.conversation_id})
    assert page.status_code == 200
    answer = _answer_html(page.text, message.message_id)

    refs = re.findall(r'<a class="citation-ref" href="([^"]+)" aria-label="Source (\d+): ([^"]+)">(\d+)</a>', answer)
    assert [(number, label) for _, number, label, _ in refs] == [
        ("1", "Inspection note.pdf · Page 1"),
        ("2", "Staff recollection recording.wav · 00:00–00:02"),
    ]
    assert all(number == text for _, number, _, text in refs)
    full = re.findall(r'<a class="citation(?: media-citation)?" href="([^"]+)">', answer)
    assert [html.unescape(href) for href, *_ in refs] == [html.unescape(href) for href in full]
    assert re.findall(r'<span class="citation-number"><span class="visually-hidden">Source </span>(\d+)</span>', answer) == ["1", "2"]
    assert answer.count("Save passage to case notes") == 2
    assert answer.count("data-cited-context") == 2


def test_each_answer_states_its_caveats_once_inside_about(cedar, monkeypatch):  # noqa: F811
    client, _bench, matter, _documents = cedar
    conversation, message, _citations = save_answer(cedar, monkeypatch, ("pdf", "transcript"))
    page = client.get(f"/matters/{matter.slug}", params={"conversation": conversation.conversation_id})
    answer = _answer_html(page.text, message.message_id)
    about = re.search(r'<details class="answer-about" data-answer-about>(.*?)</details>', answer, re.S)
    assert about is not None
    assert "<summary>About this answer<small>Check each statement against its sources" in about.group(1)
    assert answer.count(html.escape(GENERATED_REVIEW_NOTICE, quote=False)) == 1
    assert html.escape(GENERATED_REVIEW_NOTICE, quote=False) in about.group(1)
    assert "Machine transcript" in about.group(1) and answer.count("Machine transcript") == 1
    summary = re.search(r"<summary>About this answer<small>([^<]*)</small></summary>", about.group(1))[1]
    assert summary == "Check each statement against its sources · includes machine transcript · searchable text was partial"
    assert answer.count("data-answer-about") == 1


def test_same_named_passages_from_different_sources_keep_separate_numbers(cedar, monkeypatch):  # noqa: F811
    import json
    client, bench, matter, _documents = cedar
    conversation, message, _citations = save_answer(cedar, monkeypatch, ("pdf", "transcript"))
    payload = dict(message.payload)
    claims = [dict(claim, citations=[dict(citation) for citation in claim["citations"]]) for claim in payload["claims"]]
    first, second = claims[0]["citations"][0], claims[1]["citations"][0]
    assert first["support_token"] != second["support_token"]
    # Two sources can share a display name and location yet cite different passages.
    second.update(source_name=first["source_name"], location=first["location"])
    connection = bench.workspace.connection
    with bench.workspace._lock, connection:  # Background work shares this connection.
        connection.execute("UPDATE workbench_message SET payload_json=? WHERE message_id=?",
                           (json.dumps(dict(payload, claims=claims)), message.message_id))
    page = client.get(f"/matters/{matter.slug}", params={"conversation": conversation.conversation_id})
    answer = _answer_html(page.text, message.message_id)
    refs = re.findall(r'<a class="citation-ref" href="([^"]+)" aria-label="Source (\d+): [^"]+">\d+</a>', answer)
    assert [number for _, number in refs] == ["1", "2"]
    assert html.unescape(refs[0][0]) != html.unescape(refs[1][0])
    assert re.findall(r'<span class="citation-number"><span class="visually-hidden">Source </span>(\d+)</span>', answer) == ["1", "2"]


def test_a_passage_cited_twice_in_one_statement_is_numbered_once(cedar, monkeypatch):  # noqa: F811
    import json
    client, bench, matter, _documents = cedar
    conversation, message, _citations = save_answer(cedar, monkeypatch, ("pdf", "transcript"))
    payload = dict(message.payload)
    claims = [dict(claim, citations=[dict(citation) for citation in claim["citations"]]) for claim in payload["claims"]]
    claims[0]["citations"].append(dict(claims[0]["citations"][0]))
    connection = bench.workspace.connection
    with bench.workspace._lock, connection:  # Background work shares this connection.
        connection.execute("UPDATE workbench_message SET payload_json=? WHERE message_id=?",
                           (json.dumps(dict(payload, claims=claims)), message.message_id))
    page = client.get(f"/matters/{matter.slug}", params={"conversation": conversation.conversation_id})
    answer = _answer_html(page.text, message.message_id)
    assert re.findall(r'<a class="citation-ref" [^>]*>(\d+)</a>', answer) == ["1", "2"]
    assert re.findall(r'<span class="citation-number"><span class="visually-hidden">Source </span>(\d+)</span>', answer) == ["1", "1", "2"]


def test_full_citations_announce_their_number(cedar, monkeypatch):  # noqa: F811
    client, _bench, matter, _documents = cedar
    conversation, message, _citations = save_answer(cedar, monkeypatch, ("pdf", "transcript"))
    answer = _answer_html(client.get(f"/matters/{matter.slug}",
                                     params={"conversation": conversation.conversation_id}).text, message.message_id)
    full = re.findall(r'<a class="citation(?: media-citation)?" href="[^"]+">(.*?)</a>', answer, re.S)
    assert len(full) == 2
    for number, link in enumerate(full, 1):
        assert "aria-hidden" not in link.split("<svg", 1)[0]
        assert re.sub(r"<[^>]+>", "", link).strip().startswith(f"Source {number}")


def test_a_sourced_limitation_is_numbered_with_the_statements(cedar, monkeypatch):  # noqa: F811
    import json
    client, bench, matter, _documents = cedar
    conversation, message, _citations = save_answer(cedar, monkeypatch, ("pdf", "transcript"))
    payload = dict(message.payload)
    claims = [dict(claim, citations=[dict(citation) for citation in claim["citations"]]) for claim in payload["claims"]]
    # Only the first statement remains; the transcript passage is cited by the limitation alone.
    limitation = dict(payload.get("limitation") or {}, citations=[dict(claims[1]["citations"][0])])
    limitation.setdefault("text", "The recording does not say who opened the door.")
    payload = dict(payload, claims=claims[:1], limitation=limitation)
    connection = bench.workspace.connection
    with bench.workspace._lock, connection:  # Background work shares this connection.
        connection.execute("UPDATE workbench_message SET payload_json=? WHERE message_id=?",
                           (json.dumps(payload), message.message_id))
    page = client.get(f"/matters/{matter.slug}", params={"conversation": conversation.conversation_id})
    assert page.status_code == 200
    answer = _answer_html(page.text, message.message_id)
    section = answer[answer.index('<div class="answer-limitation">'):]
    assert re.findall(r'<a class="citation-ref" [^>]*>(\d+)</a>', section) == ["2"]
    assert re.findall(r'<span class="citation-number"><span class="visually-hidden">Source </span>(\d+)</span>',
                      section) == ["2"]


def test_numbered_transcript_citations_place_each_part_in_its_own_column():
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1] / "src/case_intelligence/static/workspace-layout.css").read_text()
    for part, column in ((".citation-action", "grid-column: 1 / -1"), (".citation-number", "grid-column: 1"),
                         (".citation-label", "grid-column: 2"), ("svg", "grid-column: 3")):
        rule = re.search(r"\.answer-claims \.media-citation " + re.escape(part) + r"[^{]*\{([^}]*)\}", css)
        assert rule and column in rule[1], part
    label = re.search(r"\.answer-claims \.media-citation \.citation-label[^{]*\{([^}]*)\}", css)[1]
    assert "min-width: 0" in label and "overflow-wrap: anywhere" in label
