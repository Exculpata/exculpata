"""On-demand cited context preserves saved basis and current authorization.

All sources and answers are synthetic. Deterministic answer/transcript fixtures
exercise persistence and review behavior, not model or speech-recognition quality.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import html
import io
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.templating import Jinja2Templates

from case_intelligence import report_materials
from case_intelligence.identity import SESSION_COOKIE
from tests.test_assertion_workflow import protected_assertion  # noqa: F401
from tests.test_evidence_graph_workflow import _LinkParser
from tests.test_saved_answer_note_preview import (  # noqa: F401
    ACTOR, LONG_TEXT, STATEMENT, long_answer,
)


def context_path(fixture, *, message=None, passage="claim-0", index=0):
    _, _, matter, _, _, conversation, original = fixture
    return (f"/matters/{matter.slug}/conversations/{conversation.conversation_id}"
        f"/messages/{(message or original).message_id}/cited-context/{passage}/{index}")


def append_answer(fixture, **payload):
    _, bench, matter, _, _, conversation, original = fixture
    # These reconstructed historical citations predate saved source-window metadata.
    legacy = {key: value for key, value in original.payload.items() if key != "evidence_windows"}
    return bench.workspace.append_message(matter.matter_id, conversation.conversation_id,
        "assistant", original.content, {**legacy, **payload})


def saved_reference(bench, citation):
    reference = bench._workflow_citation_payload(citation)
    reference.pop("excerpt")
    return reference


def unavailable(response, *, forbidden="Synthetic training padding."):
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["state"] == "unavailable"
    assert not value.get("excerpt") and not value.get("source_href")
    assert value["notice"]
    assert forbidden not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("long_answer", ["document", "transcript"], indirect=True)
def test_cited_context_resolves_bounded_exact_prefix_without_saving_text(long_answer):
    client, bench, matter, _, citation, conversation, message = long_answer
    before = bench.workspace.messages(matter.matter_id, conversation.conversation_id)
    assert "excerpt" not in message.payload["claims"][0]["citations"][0]
    response = client.get(context_path(long_answer), params={"format": "json"})
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["state"] == "available"
    assert value["excerpt"] == citation.excerpt[:6_000]
    assert len(value["excerpt"]) == 6_000
    assert (value["source_name"], value["location"], value["source_version_id"],
            value["excerpt_digest"]) == (citation.source_name, citation.location,
            citation.source_version_id, citation.excerpt_digest)
    assert value["excerpt_digest"] == hashlib.sha256(citation.excerpt.encode()).hexdigest()
    assert value["excerpt_digest"] != hashlib.sha256(value["excerpt"].encode()).hexdigest()
    assert value["source_href"].startswith(f"/matters/{matter.slug}")
    assert client.get(value["source_href"]).status_code == 200
    assert value["notice"] and response.headers["cache-control"] == "no-store"
    assert bench.workspace.messages(matter.matter_id, conversation.conversation_id) == before
    assert not bench.workspace.all_notebook_items(matter.matter_id, ACTOR)


def test_multiple_citations_and_limitation_resolve_independently(long_answer):
    client, bench, matter, _, first, _, original = long_answer
    other_text = "Synthetic second account reports 9 psi and leaves the timing uncertain."
    store = bench.source_store(matter)
    document, _ = store.store_stream("Synthetic second pressure account.txt", "text/plain",
        io.BytesIO(other_text.encode()))
    bench._sync_source_catalog(matter, [document])
    second = bench._citation(matter, bench._candidate(matter, document, document.parsed_units()[0], 1))
    references = [saved_reference(bench, citation) for citation in (first, second)]
    message = append_answer(long_answer,
        claims=[{**original.payload["claims"][0], "citations": references}],
        limitation={"text": "Synthetic accounts do not resolve the timing.", "citations": [references[1]]})
    for passage, index, expected in (("claim-0", 0, first), ("claim-0", 1, second), ("limitation", 0, second)):
        response = client.get(context_path(long_answer, message=message, passage=passage, index=index),
            params={"format": "json"})
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["state"] == "available" and value["excerpt"] == expected.excerpt[:6_000]
        assert value["source_name"] == expected.source_name
    with store.mutation_guard():
        store.get(document.document_id).version_id = "f" * 32
        store._save()
    assert client.get(context_path(long_answer, message=message), params={"format": "json"}).json()["state"] == "available"
    unavailable(client.get(context_path(long_answer, message=message, index=1),
        params={"format": "json"}), forbidden=other_text)
    unavailable(client.get(context_path(long_answer, message=message, passage="limitation"),
        params={"format": "json"}), forbidden=other_text)


@pytest.mark.parametrize("format_name", ["html", "json"])
def test_comparison_links_return_to_selected_answer_after_newer_answer(long_answer, format_name):
    client, _, matter, _, _, conversation, selected = long_answer
    newer = append_answer(long_answer, claims=[{**selected.payload["claims"][0],
        "text": "Synthetic newer answer that must not replace the selected review context."}])
    assert newer.message_id != selected.message_id
    expected = (f"/matters/{matter.slug}?conversation={conversation.conversation_id}"
        f"#answer-support-{selected.message_id}")
    response = client.get(context_path(long_answer), params={"format": format_name})
    assert response.status_code == 200
    if format_name == "html":
        returned = next(href for href, label in _LinkParser(response.text).links if label == "Return to answer")
        assert returned == expected
    else:
        source_href = response.json()["source_href"]
        assert parse_qs(urlsplit(source_href).query)["entity_return_to"] == [expected]
        source = client.get(source_href)
        assert source.status_code == 200
        returned = next(href for href, label in _LinkParser(source.text).links
            if label == "Return to review context")
        assert returned == expected
    original_page = client.get(expected)
    assert original_page.status_code == 200
    assert f'id="answer-support-{selected.message_id}"' in original_page.text


def test_pdf_full_source_selects_cited_physical_page_after_unsearchable_pages(long_answer, monkeypatch):
    from pypdf import PdfReader
    from case_intelligence.pilot_uploads import PilotUnit
    from tests.test_workflow_quality_handoff import pdf_bytes

    client, bench, matter, _, _, conversation, _ = long_answer
    later_text = "Synthetic page four describes a different training observation."
    original_bytes = pdf_bytes(("", STATEMENT, "", later_text))
    pages = PdfReader(io.BytesIO(original_bytes)).pages
    extracted = [(number, (page.extract_text() or "").strip())
        for number, page in enumerate(pages, 1)]
    searchable = [(number, text) for number, text in extracted if text]
    assert searchable == [(2, STATEMENT), (4, later_text)]
    store = bench.source_store(matter)
    document, _ = store.store_stream("Synthetic skipped PDF pages.pdf", "application/pdf",
        io.BytesIO(original_bytes), defer_processing=True)
    # Seed synthetic parsed units from these actual PDF bytes. This isolates the
    # navigation contract from the platform-specific extraction child sandbox.
    with store.mutation_guard():
        document.units = [asdict(PilotUnit(number, text,
            excerpt_digest=hashlib.sha256(text.encode()).hexdigest())) for number, text in searchable]
        document.page_count = len(pages)
        document.state = "ready"
        document.message = "Synthetic PDF with two searchable pages."
        store._save()
    bench._sync_source_catalog(matter, [document])
    citation = bench._citation(matter, bench._candidate(matter, document, document.parsed_units()[0], 1))
    assert citation.unit_number == 2 and citation.chunk_id == "chunk-1"
    monkeypatch.setattr(bench, "_answer_search", lambda *args, **kwargs: (citation,))
    message = bench.ask(matter, conversation, "What did the synthetic pressure reading show?")
    assert message.payload["kind"] == "generated" and message.payload["claims"][0]["text"] == STATEMENT
    before = bench.workspace.messages(matter.matter_id, conversation.conversation_id)
    comparison = client.get(context_path(long_answer, message=message), params={"format": "json"})
    assert comparison.status_code == 200
    value = comparison.json()
    assert value["state"] == "available" and value["excerpt"] == STATEMENT
    assert value["passage_text"] == STATEMENT and value["location"] == "Page 2"
    source = client.get(value["source_href"])
    assert source.status_code == 200
    selected_page = source.text.split('<h2 id="extracted-reader-heading">', 1)[1].split('</h2>', 1)[0]
    assert selected_page == "Page 2 of 4"
    assert '<pre>' + STATEMENT + '</pre>' in source.text
    assert '<pre>' + later_text + '</pre>' not in source.text
    iframe = re.search(r'<iframe[^>]+src="([^"]+)"', source.text)
    assert iframe is not None
    fragment = parse_qs(urlsplit(html.unescape(iframe.group(1))).fragment)
    assert fragment == {'page': ['2'], 'view': ['Fit'], 'navpanes': ['0']}
    assert parse_qs(urlsplit(value["source_href"]).query)["unit"] == ["1"]
    assert bench.workspace.messages(matter.matter_id, conversation.conversation_id) == before
    assert message.payload["claims"][0]["citations"][0] == saved_reference(bench, citation)
    assert store.source_path(document.document_id).read_bytes() == original_bytes


@pytest.mark.parametrize("long_answer", ["transcript"], indirect=True)
@pytest.mark.parametrize("legacy_missing_lines", [False, True])
def test_transcript_source_navigation_uses_validated_time_and_segment(long_answer, legacy_missing_lines):
    client, bench, matter, document, _, conversation, original = long_answer
    message = original
    if legacy_missing_lines:
        claim = original.payload["claims"][0]
        reference = dict(claim["citations"][0])
        reference.pop("line_start")
        reference.pop("line_end")
        # Optional legacy fields cannot substitute an unvalidated playback time.
        reference["start_ms"] = 2_000
        message = append_answer(long_answer, claims=[{**claim, "citations": [reference]}])
    saved = bench.workspace.messages(matter.matter_id, conversation.conversation_id)
    segment = bench.workspace.transcript_segments(matter.matter_id, document.document_id, document.version_id)[0]
    assert segment.start_ms == 500
    response = client.get(context_path(long_answer, message=message), params={"format": "json"})
    assert response.status_code == 200 and response.json()["state"] == "available"
    source_href = response.json()["source_href"]
    locator = urlsplit(source_href)
    assert parse_qs(locator.query)["start_ms"] == [str(segment.start_ms)]
    assert locator.fragment == f"segment-{segment.ordinal}"
    source = client.get(source_href)
    assert source.status_code == 200
    assert f'data-media-review data-start-ms="{segment.start_ms}"' in source.text
    assert f'id="segment-{segment.ordinal}"' in source.text
    assert bench.workspace.messages(matter.matter_id, conversation.conversation_id) == saved


@pytest.mark.parametrize("long_answer", ["document", "transcript"], indirect=True)
def test_source_navigation_metadata_is_opt_in_and_does_not_enter_saved_work(long_answer):
    _, bench, matter, _, _, conversation, message = long_answer
    citations = message.payload["claims"][0]["citations"]
    saved = bench.workspace.messages(matter.matter_id, conversation.conversation_id)
    with bench.source_store(matter).mutation_guard(), bench.workspace._lock:
        ordinary, = bench._saved_answer_references(matter, citations, notebook_preview=True)
        navigation, = bench._saved_answer_references(matter, citations,
            notebook_preview=True, source_navigation=True)
    assert "source_navigation" not in ordinary
    coordinates = navigation.pop("source_navigation")
    assert coordinates["ordinal"] == 1
    assert navigation == ordinary
    note, _ = bench.save_answer_to_notebook(matter, ACTOR, conversation.conversation_id,
        message.message_id, 0)
    reference, = bench.workspace.notebook_references(matter.matter_id, ACTOR, note.item_id)
    assert "source_navigation" not in asdict(reference)
    assert bench.workspace.messages(matter.matter_id, conversation.conversation_id) == saved


@pytest.mark.parametrize("format_name", ["html", "json"])
def test_comparison_does_not_load_whole_conversation_or_context_receipts(long_answer, monkeypatch, format_name):
    client, bench, _, _, _, _, _ = long_answer
    monkeypatch.setattr(bench.workspace, "messages",
        lambda *args: pytest.fail("A single comparison must not load the whole conversation"))
    response = client.get(context_path(long_answer), params={"format": format_name})
    assert response.status_code == 200
    assert "Synthetic training padding." in response.text


@pytest.mark.parametrize("long_answer", ["document", "transcript"], indirect=True)
def test_changed_text_after_prefix_cannot_refresh_historical_context(long_answer):
    client, bench, matter, document, citation, conversation, message = long_answer
    changed_text = LONG_TEXT + " Revised synthetic ending."
    store = bench.source_store(matter)
    if document.media_type == "audio/wav":
        segment = bench.workspace.transcript_segments(matter.matter_id, document.document_id, document.version_id)[0]
        edited = client.post(f"/matters/{matter.slug}/sources/{store.action_token(document)}/segments/{segment.segment_id}",
            data={"expected_revision": segment.current_revision, "text": changed_text}, follow_redirects=False)
        assert edited.status_code == 303 and "error=" not in edited.headers["location"]
    else:
        # A synthetic extraction change keeps original bytes and version intact;
        # exact-text verification must still refuse the historical citation.
        with store.mutation_guard():
            unit = document.parsed_units()[0]
            document.units = [asdict(replace(unit, text=changed_text,
                excerpt_digest=hashlib.sha256(changed_text.encode()).hexdigest()))]
            store._save()
    current = store.get(document.document_id)
    changed = bench._citation(matter, bench._candidate(matter, current, current.parsed_units()[0], 1))
    assert changed.excerpt[:6_000] == citation.excerpt[:6_000]
    assert changed.excerpt_digest != citation.excerpt_digest
    response = client.get(context_path(long_answer), params={"format": "json"})
    unavailable(response)
    assert response.json()["source_version_id"] == citation.source_version_id
    assert response.json()["excerpt_digest"] == citation.excerpt_digest
    assert "Revised synthetic ending" not in client.get(context_path(long_answer)).text
    assert bench.workspace.messages(matter.matter_id, conversation.conversation_id)[-1] == message


@pytest.mark.parametrize("change", ["version", "missing", "not_ready"])
def test_unavailable_source_does_not_substitute_current_context(long_answer, change):
    client, bench, matter, document, _, _, _ = long_answer
    store = bench.source_store(matter)
    with store.mutation_guard():
        if change == "version":
            store.get(document.document_id).version_id = "f" * 32
        elif change == "missing":
            del store.documents[document.document_id]
        else:
            store.get(document.document_id).state = "failed"
        store._save()
    unavailable(client.get(context_path(long_answer), params={"format": "json"}))


@pytest.mark.parametrize("field,replacement", [
    ("excerpt_digest", "f" * 64), ("source_version_id", "f" * 32),
    ("matter_id", "ci-matter-" + "f" * 32), ("document_id", "f" * 32),
    ("support_token", "f" * 40), ("excerpt", "Forged synthetic source text."),
    ("source_name", "Synthetic incorrect source.txt"), ("location", "Page 999"),
    ("unit_number", 999), ("chunk_id", "chunk-999"), ("line_start", 999),
    ("evidence_kind", "unrelated"),
])
@pytest.mark.parametrize("long_answer", ["document", "transcript"], indirect=True)
def test_forged_saved_citation_fails_closed(long_answer, field, replacement):
    client, _, _, _, _, _, original = long_answer
    claim = original.payload["claims"][0]
    message = append_answer(long_answer, claims=[{**claim,
        "citations": [{**claim["citations"][0], field: replacement}]}])
    unavailable(client.get(context_path(long_answer, message=message), params={"format": "json"}))


@pytest.mark.parametrize("long_answer", ["transcript"], indirect=True)
@pytest.mark.parametrize("legacy_digest_token", [False, True])
def test_transcript_without_saved_digest_requires_exact_legacy_text_token(long_answer, legacy_digest_token):
    client, bench, matter, document, citation, _, original = long_answer
    claim = original.payload["claims"][0]
    reference = dict(claim["citations"][0])
    reference.pop("excerpt_digest")
    if legacy_digest_token:
        candidate = bench._candidate(matter, document, document.parsed_units()[0], 1)
        token = bench._legacy_support_token(candidate)
        reference.update(support_token=token, href=citation.href.replace(citation.support_token, token))
    message = append_answer(long_answer, claims=[{**claim, "citations": [reference]}])
    response = client.get(context_path(long_answer, message=message), params={"format": "json"})
    if legacy_digest_token:
        assert response.status_code == 200
        assert response.json()["state"] == "available"
        assert response.json()["excerpt"] == citation.excerpt[:6_000]
    else:
        unavailable(response)


def test_saved_href_cannot_redirect_context_to_another_origin(long_answer):
    client, _, _, _, _, _, original = long_answer
    claim = original.payload["claims"][0]
    message = append_answer(long_answer, claims=[{**claim,
        "citations": [{**claim["citations"][0], "href": "https://untrusted.example.test/context"}]}])
    response = client.get(context_path(long_answer, message=message), params={"format": "json"})
    assert response.status_code == 200
    assert "untrusted.example.test" not in response.text
    if response.json()["state"] == "available":
        assert response.json()["source_href"].startswith("/matters/")


@pytest.mark.parametrize("passage,index", [("claim-1", 0), ("claim--1", 0),
    ("claim-999999999999999999999999", 0), ("claim-0", -1), ("claim-0", 12),
    ("limitation", 0), ("other", 0)])
def test_invalid_passage_or_citation_does_not_expose_context(long_answer, passage, index):
    client = long_answer[0]
    response = client.get(context_path(long_answer, passage=passage, index=index), params={"format": "json"})
    assert response.status_code == 404
    assert STATEMENT not in response.text


@pytest.mark.parametrize("foreign", ["conversation", "matter", "accessible_matter", "message", "non_generated", "user"])
def test_context_is_bound_to_generated_message_and_authorized_matter(long_answer, foreign):
    client, bench, matter, _, _, conversation, original = long_answer
    path = context_path(long_answer)
    if foreign == "conversation":
        other = bench.workspace.create_conversation(matter.matter_id, "Synthetic unrelated conversation")
        path = path.replace(conversation.conversation_id, other.conversation_id)
    elif foreign == "accessible_matter":
        other = bench.create_matter("Synthetic separate accessible matter", "", ACTOR)
        path = path.replace(matter.slug, other.slug)
    elif foreign == "matter":
        owner = "synthetic-unrelated-owner"
        bench.workspace.upsert_principal("test", owner, "Synthetic unrelated owner", owner, preferred_principal_id=owner)
        other = bench.create_matter("Synthetic inaccessible matter", "", owner)
        path = path.replace(matter.slug, other.slug)
    elif foreign == "message":
        path = path.replace(original.message_id, "message-" + "f" * 32)
    else:
        message = bench.workspace.append_message(matter.matter_id, conversation.conversation_id,
            "user" if foreign == "user" else "assistant", original.content,
            {**original.payload, "kind": "generated" if foreign == "user" else "retrieval"})
        path = context_path(long_answer, message=message)
    response = client.get(path, params={"format": "json"})
    assert response.status_code == 404
    assert STATEMENT not in response.text


@pytest.mark.parametrize("limit,value", [("MAX_SOURCE_SCAN_CHARS", 6_000),
    ("MAX_SOURCE_SCAN_SERIALIZED_CHARS", 6_000), ("MAX_SOURCE_SCAN_RECORD_CHARS", 6_000),
    ("MAX_SOURCE_SCAN_UNITS", 0), ("MAX_SOURCE_SCAN_SECONDS", 0)])
def test_context_refuses_incomplete_validation_within_existing_scan_budgets(long_answer, monkeypatch, limit, value):
    monkeypatch.setattr(report_materials, limit, value)
    unavailable(long_answer[0].get(context_path(long_answer), params={"format": "json"}))


def test_comparison_html_escapes_generated_and_source_text(long_answer):
    client, bench, matter, _, _, _, original = long_answer
    text = "Synthetic <script>sourceCanary()</script> & cited context."
    claim_text = "Synthetic <script>claimCanary()</script> & interpretation."
    document, _ = bench.source_store(matter).store_stream("Synthetic markup source.txt",
        "text/plain", io.BytesIO(text.encode()))
    bench._sync_source_catalog(matter, [document])
    citation = bench._citation(matter, bench._candidate(matter, document, document.parsed_units()[0], 1))
    message = append_answer(long_answer, claims=[{**original.payload["claims"][0],
        "text": claim_text, "citations": [saved_reference(bench, citation)]}])
    response = client.get(context_path(long_answer, message=message))
    assert response.status_code == 200, response.text
    assert html.escape(text) in response.text and html.escape(claim_text) in response.text
    assert "<script>sourceCanary()" not in response.text and "<script>claimCanary()" not in response.text
    assert "cited context" in response.text.casefold()
    assert response.headers["cache-control"] == "no-store"


@pytest.fixture
def protected_answer(protected_assertion):
    context = protected_assertion
    bench, matter = context["bench"], context["matter"]
    document = next(iter(bench.source_store(matter).documents.values()))
    citation = bench._citation(matter, bench._candidate(matter, document, document.parsed_units()[0], 1))
    conversation = bench.workspace.get_conversation(matter.matter_id)
    message = bench.workspace.append_message(matter.matter_id, conversation.conversation_id,
        "assistant", "Synthetic generated comparison canary", {"kind": "generated", "claims": [{
            "text": "Synthetic generated comparison canary", "citations": [saved_reference(bench, citation)]}]})
    fixture = (context["client"], bench, matter, document, citation, conversation, message)
    return context, fixture


@pytest.mark.parametrize("moment,format_name", [("before", "json"), ("resolution", "json"),
    ("resolution", "html"), ("render", "html")])
@pytest.mark.parametrize("change", ["membership", "session"])
def test_authority_rechecked_after_context_resolution_and_render(protected_answer, monkeypatch,
        moment, format_name, change):
    context, fixture = protected_answer
    client, bench, matter, _, citation, _, _ = fixture
    identity = client.app.state.identity
    session_context = identity.resolve(client.cookies.get(SESSION_COOKIE))
    assert session_context is not None
    changes = []

    def restrict():
        if changes:
            return
        if change == "membership":
            bench.workspace.revoke_member(matter.matter_id, context["member_id"], context["owner_id"])
        else:
            identity.logout(session_context)
        changes.append(True)

    if moment == "before":
        restrict()
    elif moment == "resolution":
        original = bench._saved_answer_references

        def resolved_then_restrict(*args, **kwargs):
            result = original(*args, **kwargs)
            restrict()
            return result

        monkeypatch.setattr(bench, "_saved_answer_references", resolved_then_restrict)
    else:
        original = Jinja2Templates.TemplateResponse

        def rendered_then_restrict(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            restrict()
            return result

        monkeypatch.setattr(Jinja2Templates, "TemplateResponse", rendered_then_restrict)
    response = client.get(context_path(fixture),
        params={"format": "json"} if format_name == "json" else {},
        headers={**context["headers"], "Accept": "application/json"}, follow_redirects=False)
    assert changes
    assert response.status_code in ({401, 404} if moment == "before" and change == "session" else {404})
    assert citation.excerpt not in response.text and "Synthetic generated comparison canary" not in response.text
    if response.status_code == 404:
        assert response.headers.get("cache-control") == "no-store"


@pytest.mark.parametrize("format_name", ["html", "json"])
def test_admin_demotion_with_retained_membership_cannot_expose_foreign_navigation(tmp_path,
        monkeypatch, format_name):
    from fastapi.testclient import TestClient
    from tests.test_matter_management import _app, _headers, ADMIN, USER_GROUP

    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    with TestClient(_app(tmp_path), base_url="https://recordbench.example.test") as client:
        assert client.get("/matters/new", headers=_headers(ADMIN)).status_code == 200
        bench, identity = client.app.state.workbench, client.app.state.identity
        actor = identity.resolve(client.cookies.get(SESSION_COOKIE)).principal_id
        matter = bench.create_matter("Synthetic administrator member matter", "", actor)
        other = "synthetic-unrelated-owner"
        bench.workspace.upsert_principal("test", other, "Synthetic unrelated owner", other,
            preferred_principal_id=other)
        foreign = bench.create_matter("Synthetic foreign navigation canary", "", other)
        document, _ = bench.source_store(matter).store_stream("Synthetic context.txt", "text/plain",
            io.BytesIO(b"Synthetic source passage."))
        bench._sync_source_catalog(matter, [document])
        citation = bench._citation(matter, bench._candidate(matter, document, document.parsed_units()[0], 1))
        conversation = bench.workspace.get_conversation(matter.matter_id)
        message = bench.workspace.append_message(matter.matter_id, conversation.conversation_id,
            "assistant", "Synthetic passage.", {"kind": "generated", "claims": [{
                "text": "Synthetic passage.", "citations": [saved_reference(bench, citation)]}]})
        fixture = (client, bench, matter, document, citation, conversation, message)
        original = bench._saved_answer_references
        demoted = []

        def resolve_then_demote(*args, **kwargs):
            result = original(*args, **kwargs)
            monkeypatch.setattr(identity, "kerberos_group_resolver", lambda principal: {USER_GROUP})
            demoted.append(True)
            return result

        monkeypatch.setattr(bench, "_saved_answer_references", resolve_then_demote)
        response = client.get(context_path(fixture), params={"format": format_name}, headers=_headers(ADMIN))
        assert demoted
        assert response.status_code == (404 if format_name == "html" else 200)
        assert foreign.display_name not in response.text
        if format_name == "json":
            assert response.json()["state"] == "available"
            assert response.json()["excerpt"] == citation.excerpt
        else:
            assert citation.excerpt not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert client.get(f"/matters/{foreign.slug}", headers=_headers(ADMIN)).status_code == 404
