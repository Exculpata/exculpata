"""Synthetic regression: the assistant dock suggests questions for the page that is open."""
from __future__ import annotations

import html
import re
from urllib.parse import urlencode

from tests.test_automatic_discovery import upload, workbench  # noqa: F401

SOURCE = ("Synthetic delivery log.txt", b"The crate was delivered on 2026-03-05 at the north dock.")


def dock(page: str) -> str:
    found = re.search(r'<aside class="matter-assistant".*?</aside>', page, re.S)
    assert found, "assistant dock missing"
    return found[0]


def suggestions(fragment: str) -> tuple[str, list[tuple[str, str]]]:
    group = re.search(r'<div class="assistant-suggestions" role="group".*?</div>', fragment, re.S)
    assert group, "suggestions missing"
    heading = re.search(r'id="assistant-suggestions-heading"[^>]*>([^<]+)<', group[0])[1]
    buttons = re.findall(r'<button type="button" data-assistant-suggestion="([^"]+)"[^>]*>([^<]+)</button>', group[0])
    return html.unescape(heading), [(html.unescape(question), html.unescape(label)) for question, label in buttons]


def source_path(bench, matter, name=SOURCE[0]):
    store = bench.source_store(matter)
    document = next(item for item in store.documents.values() if item.display_name == name)
    return f"/matters/{matter.slug}/sources/{store.action_token(document)}"


def test_an_open_source_suggests_questions_limited_to_it(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, *SOURCE)
    fragment = dock(client.get(source_path(bench, matter)).text)
    heading, items = suggestions(fragment)
    assert heading == "Suggested for this source"
    assert items == [
        ("Summarize this source.", "Summarize this source"),
        ("Which people, organizations and things appear in this source?", "Who and what appears here"),
        ("Which dates appear in this source, and what happened on each?", "What dates appear here"),
    ]
    # Choosing one limits the next question to this source before filling it in.
    # Three shown in the empty chat, three more kept for a new chat drafted in the browser.
    assert fragment.count('data-assistant-suggestion-scope="source"') == 6
    assert "limits your next question to this source; nothing is sent until you choose Send." in fragment
    assert fragment.count('aria-describedby="assistant-suggestions-hint"') == 3
    assert 'data-assistant-suggestion-status role="status"' in fragment


def test_other_pages_keep_the_general_suggestions(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, *SOURCE)
    for path in (f"/matters/{matter.slug}/notebook", f"/matters/{matter.slug}/setup"):
        heading, items = suggestions(dock(client.get(path).text))
        assert heading == "Suggested questions"
        assert [label for _question, label in items] == [
            "Summarize the records", "Find people and organizations", "Review dates and events"]
        assert "data-assistant-suggestion-scope" not in dock(client.get(path).text)


def test_the_refreshed_dock_keeps_its_page_and_ignores_paths_outside_the_matter(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, *SOURCE)
    reader = source_path(bench, matter)
    fragment_url = html.unescape(re.search(r'data-fragment-url="([^"]+)"', dock(client.get(reader).text))[1])
    assert "assistant_from=" in fragment_url
    heading, _items = suggestions(client.get(fragment_url).text)
    assert heading == "Suggested for this source"
    for outside in ("https://example.invalid/matters/x/sources/y", "/matters/other-matter/sources/abc",
                    f"/matters/{matter.slug}/sources/not-a-token"):
        refreshed = client.get(f"/matters/{matter.slug}/assistant?" + urlencode({"assistant_from": outside}))
        assert refreshed.status_code == 200
        assert suggestions(refreshed.text)[0] == "Suggested questions"


def test_the_dock_has_a_labelled_close_control(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, *SOURCE)
    fragment = dock(client.get(f"/matters/{matter.slug}/notebook").text)
    close = re.search(r'<button type="button" class="assistant-close" data-assistant-collapse '
                      r'aria-label="([^"]+)">.*?<span>([^<]+)</span>', fragment, re.S)
    assert close and close[2] == "Close" and close[2] in close[1]


def test_a_new_chat_drafted_in_the_browser_can_offer_the_same_suggestions(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, *SOURCE)
    conversation = bench.workspace.get_conversation(matter.matter_id)
    bench.workspace.append_message(matter.matter_id, conversation.conversation_id, "user", "A synthetic earlier question?")
    fragment = dock(client.get(source_path(bench, matter)).text)
    assert 'id="assistant-suggestions-heading"' not in fragment
    template = re.search(r"<template data-assistant-suggestions-template>(.*?)</template>", fragment, re.S)
    assert template
    heading, items = suggestions(template[1].replace('-draft"', '"'))
    assert heading == "Suggested for this source" and len(items) == 3
    assert 'id="assistant-suggestions-heading-draft"' in template[1]
    identifiers = re.findall(r'\bid="([^"]+)"', client.get(source_path(bench, matter)).text)
    assert len(identifiers) == len(set(identifiers))
