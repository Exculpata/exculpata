"""Synthetic regression: automatic suggestions are reviewed from one inbox."""
from __future__ import annotations

from tests.fixture_write import fixture_write

import html
import re

from tests.test_automatic_discovery import FIRST, upload, workbench  # noqa: F401
from tests.test_matter_notebook import WEB_ACTOR

SECOND = b"Jordan Sample called Alex Example again about badge: QX-77."


def inbox_names(html: str) -> list[str]:
    section = re.search(r'<section class="suggestion-inbox".*?</section>', html, re.S)
    if section is None:
        return []
    return re.findall(r'<p class="suggestion-title"><a href="[^"]+">([^<]+)</a>', section[0])


def test_inbox_lists_suggestions_most_mentioned_first_with_their_passage(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    upload(client, matter.slug, "Synthetic call.txt", SECOND)
    bench.run_automatic_discovery_once()
    page = client.get(f"/matters/{matter.slug}/entities").text
    names = inbox_names(page)
    # Same-named suggestions share one row, most-mentioned first; they stay
    # separate identities.
    assert names[:2] == ["Alex Example", "Jordan Sample"]
    assert sorted(names[2:]) == ["03/04/2026", "Amber Cooperative", "QX-77"]
    assert "2 mentions · 2 sources" in page
    assert "Matching names are grouped; a decision applies to each and never merges them." in page
    assert len(bench.entity_service(matter).list(matter.matter_id, WEB_ACTOR)[0]) == 7
    assert 'Suggestions to review <span class="suggestion-total">' in page
    assert "<mark>Jordan Sample</mark>" in page
    assert re.search(rf'class="suggestion-source" href="/matters/{matter.slug}\?support=[0-9a-f]{{40}}#support-pane">Synthetic', page)
    assert '<details class="notebook-tool-card guided-discovery" aria-labelledby="discovery-heading">' in page


def test_type_filter_narrows_the_inbox(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    dates = client.get(f"/matters/{matter.slug}/entities", params={"kind": "dates"}).text
    assert inbox_names(dates) == ["03/04/2026"]
    assert re.search(r'aria-current="page">Dates <span>1</span>', dates)
    people = client.get(f"/matters/{matter.slug}/entities", params={"kind": "people"}).text
    assert sorted(inbox_names(people)) == ["Alex Example", "Jordan Sample"]
    assert len(inbox_names(client.get(f"/matters/{matter.slug}/entities", params={"kind": "x"}).text)) == 4


def test_one_click_decisions_apply_to_each_same_named_suggestion(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    upload(client, matter.slug, "Synthetic call.txt", SECOND)
    bench.run_automatic_discovery_once()
    service = bench.entity_service(matter)
    rows, _ = service.list(matter.matter_id, WEB_ACTOR)
    jordans = [row for row in rows if row["display_name"] == "Jordan Sample"]
    amber = next(row for row in rows if row["display_name"] == "Amber Cooperative")
    page = client.get(f"/matters/{matter.slug}/entities", params={"kind": "people"}).text
    targets = re.findall(r'name="targets" value="([^"]+)"><input type="hidden" name="status" value="confirmed"', page)
    jordan_targets = next(value for value in targets if jordans[0]["entity_id"] in value)
    assert sorted(jordan_targets.split(",")) == sorted(f"{row['entity_id']}:{row['revision']}" for row in jordans)
    path = f"/matters/{matter.slug}/entities/actions"
    response = client.post(path, data=dict(action="decide", targets=jordan_targets, status="confirmed", kind="people"),
                           follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/matters/{matter.slug}/entities?kind=people")
    assert response.headers["location"].endswith("#suggestions")
    response = client.post(path, data=dict(action="decide", targets=f"{amber['entity_id']}:{amber['revision']}",
                                           status="dismissed"), follow_redirects=False)
    assert response.status_code == 303
    page = client.get(f"/matters/{matter.slug}/entities").text
    assert sorted(inbox_names(page)) == ["03/04/2026", "Alex Example", "QX-77"]
    for row in jordans:
        entity, mentions, history, _note = service.detail(matter.matter_id, WEB_ACTOR, row["entity_id"])
        assert entity["status"] == "confirmed" and entity["display_name"] == "Jordan Sample"
        assert len(mentions) == 1  # Still separate identities with their own passage.
        assert sorted(entry["action"] for entry in history) == ["edited", "extracted"]
    # A stale click (someone else decided first) changes nothing and shows the current list.
    alexes = [row for row in rows if row["display_name"] == "Alex Example"]
    service.decide(matter.matter_id, WEB_ACTOR, [(alexes[1]["entity_id"], alexes[1]["revision"])], status="dismissed")
    stale = client.post(path, data=dict(action="decide", status="confirmed",
                                        targets=",".join(f"{row['entity_id']}:{row['revision']}" for row in alexes)))
    assert stale.status_code == 409
    assert "That suggestion changed since this page loaded." in stale.text
    assert service.detail(matter.matter_id, WEB_ACTOR, alexes[0]["entity_id"])[0]["status"] == "suggested"
    # Only review decisions, on well-formed targets, are accepted.
    for data in (dict(status="suggested", targets=f"{alexes[0]['entity_id']}:{alexes[0]['revision']}"),
                 dict(status="confirmed", targets="not-a-target"),
                 dict(status="confirmed", targets=f"{alexes[0]['entity_id']}:1,{alexes[0]['entity_id']}:1")):
        refused = client.post(path, data=dict(action="decide", **data))
        assert refused.status_code == 409
        assert service.detail(matter.matter_id, WEB_ACTOR, alexes[0]["entity_id"])[0]["status"] == "suggested"


def test_no_inbox_without_suggestions(workbench):  # noqa: F811
    client, _bench, matter, _runtime = workbench
    page = client.get(f"/matters/{matter.slug}/entities").text
    assert 'class="suggestion-inbox"' not in page
    assert '<details class="notebook-tool-card guided-discovery" aria-labelledby="discovery-heading" open>' in page


def test_only_automatically_found_identities_are_listed(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    service = bench.entity_service(matter)
    # A reviewer may record a suggestion by hand; it keeps its own provenance.
    service.create(matter.matter_id, WEB_ACTOR, display_name="Casey Placeholder", status="suggested")
    service.create(matter.matter_id, WEB_ACTOR, display_name="Alex Example", status="suggested")
    page = client.get(f"/matters/{matter.slug}/entities").text
    assert "Casey Placeholder" not in inbox_names(page)
    alex = re.search(r'<li class="suggestion" data-suggestion>(?:(?!</li>).)*?>Alex Example</a>.*?</li>', page, re.S)[0]
    assert "1 mention · 1 source<" in alex and alex.count('name="targets"') == 2
    assert len(re.search(r'name="targets" value="([^"]+)"', alex)[1].split(",")) == 1


def test_source_count_includes_every_attached_passage(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    upload(client, matter.slug, "Synthetic call.txt", SECOND)
    bench.run_automatic_discovery_once()
    service = bench.entity_service(matter)
    rows, _ = service.list(matter.matter_id, WEB_ACTOR)
    amber = next(row for row in rows if row["display_name"] == "Amber Cooperative")
    badge = next(row for row in rows if row["display_name"] == "QX-77")
    support = service.detail(matter.matter_id, WEB_ACTOR, badge["entity_id"])[1][0]["support_token"]
    service.attach(matter.matter_id, WEB_ACTOR, amber["entity_id"], expected_revision=amber["revision"], support=support)
    items, _total, _kinds = service.inbox(matter.matter_id, WEB_ACTOR)
    group = next(item for item in items if item["display_name"] == "Amber Cooperative")
    assert (group["identity_count"], group["mention_count"], group["source_count"]) == (1, 2, 2)


def test_highlight_survives_case_folding_that_changes_length():
    from case_intelligence.entity_repository import EntityRepository
    assert EntityRepository.snippet("Straße; Alex Example met", "alex example") == ("Straße; ", "Alex Example", " met")
    assert EntityRepository.snippet("STRASSE Alex", "straße") == ("", "STRASSE", " Alex")


def test_each_grouped_decision_is_audited_against_its_identity(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    upload(client, matter.slug, "Synthetic call.txt", SECOND)
    bench.run_automatic_discovery_once()
    rows, _ = bench.entity_service(matter).list(matter.matter_id, WEB_ACTOR)
    jordans = [row for row in rows if row["display_name"] == "Jordan Sample"]
    targets = ",".join(f"{row['entity_id']}:{row['revision']}" for row in jordans)
    response = client.post(f"/matters/{matter.slug}/entities/actions",
                           data=dict(action="decide", targets=targets, status="confirmed"), follow_redirects=False)
    assert response.status_code == 303
    events = [event for event in bench.workspace.audit_events(matter.matter_id) if event.action == "entity.decide"]
    assert sorted(event.object_id for event in events) == sorted(row["entity_id"] for row in jordans)
    assert {event.object_type for event in events} == {"entity"}


def test_inbox_work_grows_linearly_with_suggestions(workbench):  # noqa: F811
    import hashlib
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    service = bench.entity_service(matter)
    rows, _ = service.list(matter.matter_id, WEB_ACTOR)
    sample = service.detail(matter.matter_id, WEB_ACTOR, rows[0]["entity_id"])[1][0]
    connection = bench.workspace.connection

    def add(count, start):
        # Hold the store's lock: background work shares this connection.
        with fixture_write(bench.workspace):
            for index in range(start, start + count):
                entity_id = f"synthetic-entity-{index:06d}"
                connection.execute(
                    "INSERT INTO workbench_entity(entity_id,matter_id,entity_type,display_name,status,origin,"
                    "created_by,created_at,updated_by,updated_at) VALUES (?,?,'person',?,'suggested','extraction',"
                    "?,'2026-01-01T00:00:00Z',?,'2026-01-01T00:00:00Z')",
                    (entity_id, matter.matter_id, f"Synthetic Person {index:06d}", WEB_ACTOR, WEB_ACTOR))
                connection.execute(
                    "INSERT INTO workbench_entity_mention(mention_id,matter_id,entity_id,document_id,source_version_id,"
                    "source_name,location,unit_number,chunk_id,excerpt_digest,excerpt,support_token,origin,created_by,"
                    "created_at) VALUES (?,?,?,?,?,?,?,1,?,?,?,?,'extraction',?,'2026-01-01T00:00:00Z')",
                    (f"synthetic-mention-{index:06d}", matter.matter_id, entity_id, sample["document_id"],
                     sample["source_version_id"], sample["source_name"], sample["location"], sample["chunk_id"],
                     hashlib.sha256(str(index).encode()).hexdigest(), f"Synthetic Person {index:06d} called.",
                     sample["support_token"], WEB_ACTOR))

    def steps():
        counted = [0]

        def tick():
            counted[0] += 1
            return 0
        connection.set_progress_handler(tick, 1000)
        try:
            items, total, _kinds = service.inbox(matter.matter_id, WEB_ACTOR)
        finally:
            connection.set_progress_handler(None, 0)
        assert len(items) == 25
        return counted[0], total

    add(1000, 0)
    small, total = steps()
    add(3000, 1000)
    large, larger_total = steps()
    assert larger_total - total == 3000
    # Four times the suggestions costs about four times the work, not sixteen.
    assert large <= 6 * max(small, 1), (small, large)


def test_decisions_apply_only_to_pending_automatic_suggestions(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    service = bench.entity_service(matter)
    manual = service.create(matter.matter_id, WEB_ACTOR, display_name="Casey Placeholder", status="suggested")
    rows, _ = service.list(matter.matter_id, WEB_ACTOR)
    amber = next(row for row in rows if row["display_name"] == "Amber Cooperative")
    alex = next(row for row in rows if row["display_name"] == "Alex Example")
    service.decide(matter.matter_id, WEB_ACTOR, [(alex["entity_id"], alex["revision"])], status="confirmed")
    alex = service.detail(matter.matter_id, WEB_ACTOR, alex["entity_id"])[0]
    path = f"/matters/{matter.slug}/entities/actions"
    # A forged or replayed form naming a manual or already reviewed identity at its
    # current revision changes nothing, including any other target in the batch.
    for forged in (manual, alex):
        targets = f"{amber['entity_id']}:{amber['revision']},{forged['entity_id']}:{forged['revision']}"
        response = client.post(path, data=dict(action="decide", targets=targets, status="dismissed"))
        assert response.status_code == 409
    assert service.detail(matter.matter_id, WEB_ACTOR, amber["entity_id"])[0]["status"] == "suggested"
    assert service.detail(matter.matter_id, WEB_ACTOR, manual["entity_id"])[0]["status"] == "suggested"
    assert service.detail(matter.matter_id, WEB_ACTOR, alex["entity_id"])[0]["status"] == "confirmed"
    assert not [event for event in bench.workspace.audit_events(matter.matter_id)
                if event.action == "entity.decide" and event.object_id in (amber["entity_id"], manual["entity_id"])]


def test_a_group_larger_than_one_decision_says_so(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    connection = bench.workspace.connection
    with fixture_write(bench.workspace):
        for index in range(201):
            connection.execute(
                "INSERT INTO workbench_entity(entity_id,matter_id,entity_type,display_name,status,origin,"
                "created_by,created_at,updated_by,updated_at) VALUES (?,?,'person','Riley Placeholder','suggested',"
                "'extraction',?,'2026-01-01T00:00:00Z',?,'2026-01-01T00:00:00Z')",
                (f"synthetic-riley-{index:03d}", matter.matter_id, WEB_ACTOR, WEB_ACTOR))
    page = client.get(f"/matters/{matter.slug}/entities").text
    riley = re.search(r'<li class="suggestion" data-suggestion>(?:(?!</li>).)*?>Riley Placeholder</a>.*?</li>', page, re.S)[0]
    assert "applies to 200 of these 201 suggestions at a time; the rest stay listed" in riley
    assert len(re.search(r'name="targets" value="([^"]+)"', riley)[1].split(",")) == 200


def test_an_unavailable_original_is_not_offered_as_a_link(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    service = bench.entity_service(matter)
    rows, _ = service.list(matter.matter_id, WEB_ACTOR)
    amber = next(row for row in rows if row["display_name"] == "Amber Cooperative")
    connection = bench.workspace.connection
    with fixture_write(bench.workspace):  # The retained reference cites an earlier source version.
        connection.execute("UPDATE workbench_entity_mention SET source_version_id='superseded-version' WHERE entity_id=?",
                           (amber["entity_id"],))
    page = client.get(f"/matters/{matter.slug}/entities").text
    row = re.search(r'<li class="suggestion" data-suggestion>(?:(?!</li>).)*?>Amber Cooperative</a>.*?</li>', page, re.S)[0]
    assert 'class="suggestion-source"' not in row and "?support=" not in row
    assert "original passage changed or is unavailable" in row
    alex = re.search(r'<li class="suggestion" data-suggestion>(?:(?!</li>).)*?>Alex Example</a>.*?</li>', page, re.S)[0]
    assert 'class="suggestion-source" href=' in alex


def test_guided_discovery_stays_collapsed_while_searching(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    page = client.get(f"/matters/{matter.slug}/entities", params={"q": "Alex"}).text
    assert 'class="suggestion-inbox"' not in page
    assert '<details class="notebook-tool-card guided-discovery" aria-labelledby="discovery-heading">' in page


def _row(page, name):
    return re.search(rf'<li class="suggestion" data-suggestion>(?:(?!</li>).)*?>{re.escape(name)}</a>.*?</li>', page, re.S)[0]


import pytest


@pytest.mark.parametrize("damage", ["missing", "malformed"])
def test_a_damaged_original_is_reported_unavailable_not_as_an_error(workbench, damage):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    store = bench.source_store(matter)
    rows, _ = bench.entity_service(matter).list(matter.matter_id, WEB_ACTOR)
    amber = next(row for row in rows if row["display_name"] == "Amber Cooperative")
    mention = bench.entity_service(matter).detail(matter.matter_id, WEB_ACTOR, amber["entity_id"])[1][0]
    document = store.get(mention["document_id"])
    assert document.units_file
    path = store.derived / document.units_file
    if damage == "missing":
        path.unlink()  # Derived searchable text is lost.
    else:
        import json
        serialized = json.loads(path.read_text(encoding="utf-8"))
        serialized["units"] = [{"unexpected": 1} for _unit in serialized["units"]]
        path.write_text(json.dumps(serialized), encoding="utf-8")  # Valid JSON, wrong record shape.
    # The inbox checks catalog metadata only; the identity page validates the
    # exact passage text and reports the damage.
    page = client.get(f"/matters/{matter.slug}/entities")
    assert page.status_code == 200 and "Amber Cooperative" in _row(page.text, "Amber Cooperative")
    detail = client.get(f"/matters/{matter.slug}/entities/{amber['entity_id']}")
    assert detail.status_code == 200 and "Original passage changed or is unavailable" in detail.text
    # Following the inbox link reports the passage unavailable instead of failing,
    # and a passage in an undamaged source still opens.
    link = re.search(r'class="suggestion-source" href="([^"]+)"', _row(page.text, "Amber Cooperative"))[1]
    opened = client.get(html.unescape(link))
    assert opened.status_code == 404 and "Source support is unavailable" in opened.text
    upload(client, matter.slug, "Synthetic call.txt", SECOND)
    bench.run_automatic_discovery_once()
    items, _total, _kinds = bench.entity_service(matter).inbox(matter.matter_id, WEB_ACTOR)
    healthy = next(item["first_mention"] for item in items
                   if item["first_mention"]["document_id"] != mention["document_id"])
    assert client.get(f"/matters/{matter.slug}", params={"support": healthy["support_token"]}).status_code == 200


def test_an_inbox_page_citing_many_sources_reads_no_derived_text(workbench, monkeypatch):  # noqa: F811
    from case_intelligence.entity_unit_reader import EntityUnitReader
    from case_intelligence.pilot_uploads import PilotDocument
    client, bench, matter, _runtime = workbench
    for number in range(12):  # More sources than any source-index cache holds.
        upload(client, matter.slug, f"Synthetic note {number}.txt",
               f"Casey Person{number} met Alex Example about badge QX-{number:02}.".encode())
    bench.run_automatic_discovery_once()
    service = bench.entity_service(matter)
    def refuse(*_args, **_kwargs):
        raise AssertionError("derived text read while listing the inbox")
    monkeypatch.setattr(PilotDocument, "parsed_units", refuse)
    monkeypatch.setattr(EntityUnitReader, "iter_selected", refuse)
    service.validate_references = refuse
    items, total, _kinds = service.inbox(matter.matter_id, WEB_ACTOR)
    sources = {item["first_mention"]["document_id"] for item in items}
    assert total >= 13 and len(sources) == 12
    assert all(item["first_mention"]["available"] for item in items)
    page = client.get(f"/matters/{matter.slug}/entities")
    assert page.status_code == 200 and page.text.count('class="suggestion-source" href=') == len(items)


def test_a_source_that_is_not_ready_is_not_offered_as_a_link(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    rows, _ = bench.entity_service(matter).list(matter.matter_id, WEB_ACTOR)
    amber = next(row for row in rows if row["display_name"] == "Amber Cooperative")
    mention = bench.entity_service(matter).detail(matter.matter_id, WEB_ACTOR, amber["entity_id"])[1][0]
    bench.source_store(matter).mark_failed(mention["document_id"], "Synthetic failure")
    page = client.get(f"/matters/{matter.slug}/entities").text
    assert "original passage changed or is unavailable" in _row(page, "Amber Cooperative")
    assert 'class="suggestion-source" href=' not in page


def test_searching_keeps_the_inbox_position(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    page = client.get(f"/matters/{matter.slug}/entities", params={"kind": "people"}).text
    search = re.search(r'<form method="get" class="notebook-filter-form">.*?</form>', page, re.S)[0]
    assert '<input type="hidden" name="kind" value="people">' in search
    results = client.get(f"/matters/{matter.slug}/entities", params={"q": "Alex", "kind": "people"}).text
    link = re.search(r'<a href="([^"]+)">Alex Example</a>', results)[1]
    assert "kind=people" in html.unescape(link)
    back = client.get(html.unescape(link)).text
    assert "kind=people" in re.search(r'<a href="([^"]+)">All entities</a>', back)[1]


def test_selected_support_survives_inbox_navigation_and_decisions(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    service = bench.entity_service(matter)
    rows, _ = service.list(matter.matter_id, WEB_ACTOR)
    amber = next(row for row in rows if row["display_name"] == "Amber Cooperative")
    support = service.detail(matter.matter_id, WEB_ACTOR, amber["entity_id"])[1][0]["support_token"]
    page = client.get(f"/matters/{matter.slug}/entities", params={"support": support}).text
    section = re.search(r'<section class="suggestion-inbox".*?</section>', page, re.S)[0]
    links = re.findall(r'<a href="(/matters/[^"]+\?kind=[^"]*)"', section)
    assert links and all(f"support={support}" in link for link in links)
    assert f'name="support" value="{support}"' in section
    response = client.post(f"/matters/{matter.slug}/entities/actions", follow_redirects=False, data=dict(
        action="decide", targets=f"{amber['entity_id']}:{amber['revision']}", status="dismissed", support=support))
    assert response.status_code == 303 and f"support={support}" in response.headers["location"]


def test_a_renamed_suggestion_still_highlights_the_extracted_text(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    service = bench.entity_service(matter)
    rows, _ = service.list(matter.matter_id, WEB_ACTOR)
    amber = next(row for row in rows if row["display_name"] == "Amber Cooperative")
    service.update(matter.matter_id, WEB_ACTOR, amber["entity_id"], expected_revision=amber["revision"],
                   display_name="Amber Co-op", entity_type=amber["entity_type"], status="suggested")
    row = _row(client.get(f"/matters/{matter.slug}/entities").text, "Amber Co-op")
    assert "<mark>Amber Cooperative</mark>" in row


def test_a_search_does_not_wait_for_source_work(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    service = bench.entity_service(matter)
    def busy():
        raise AssertionError("the source guard was requested")
    service.source_guard = busy
    _items, _total, kinds = service.inbox(matter.matter_id, WEB_ACTOR, rows=False)
    assert sum(kinds.values()) == 4


def test_the_recorded_occurrence_is_highlighted_when_a_name_repeats():
    from case_intelligence.entity_repository import EntityRepository
    excerpt = "Alex Example arrived. Later, Alex Example left."
    second = excerpt.rindex("Alex Example")
    before, match, after = EntityRepository.snippet_at(excerpt, second, second + len("Alex Example"))
    assert match == "Alex Example" and before.endswith("Later, ") and after == " left."
    assert "arrived" in before  # The first occurrence is context, not the highlight.


def test_a_retyped_place_suggestion_stays_counted_and_filterable(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    service = bench.entity_service(matter)
    rows, _ = service.list(matter.matter_id, WEB_ACTOR)
    amber = next(row for row in rows if row["display_name"] == "Amber Cooperative")
    service.update(matter.matter_id, WEB_ACTOR, amber["entity_id"], expected_revision=amber["revision"],
                   display_name="Amber Cooperative", entity_type="place", status="suggested")
    page = client.get(f"/matters/{matter.slug}/entities").text
    assert re.search(r'>Places <span>1</span>', page)
    places = client.get(f"/matters/{matter.slug}/entities", params={"kind": "places"}).text
    assert inbox_names(places) == ["Amber Cooperative"]


def test_opening_a_suggestion_keeps_the_inbox_position(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    page = client.get(f"/matters/{matter.slug}/entities", params={"kind": "people"}).text
    link = re.search(r'<p class="suggestion-title"><a href="([^"]+)">Alex Example</a>', page)[1]
    assert "kind=people" in link
    detail = client.get(html.unescape(link)).text
    back = re.search(r'<a href="([^"]+)">All entities</a>', detail)[1]
    assert "kind=people" in back
    service = bench.entity_service(matter)
    rows, _ = service.list(matter.matter_id, WEB_ACTOR)
    alex = next(row for row in rows if row["display_name"] == "Alex Example")
    response = client.post(f"/matters/{matter.slug}/entities/actions", follow_redirects=False, data=dict(
        action="update", entity_id=alex["entity_id"], expected_revision=alex["revision"], display_name="Alex Example",
        entity_type="person", status="suggested", kind="people"))
    assert response.status_code == 303 and "kind=people" in response.headers["location"]


def test_a_failed_detail_action_keeps_the_inbox_position(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    rows, _ = bench.entity_service(matter).list(matter.matter_id, WEB_ACTOR)
    alex = next(row for row in rows if row["display_name"] == "Alex Example")
    stale = client.post(f"/matters/{matter.slug}/entities/actions", data=dict(
        action="update", entity_id=alex["entity_id"], expected_revision=alex["revision"] + 5,
        display_name="Alex Example", entity_type="person", status="suggested", kind="people", inbox_page="2"))
    assert stale.status_code == 409
    back = re.search(r'<a href="([^"]+)">All entities</a>', stale.text)[1]
    assert "kind=people" in back and "inbox_page=2" in back
    assert '<input type="hidden" name="kind" value="people">' in stale.text


def test_the_reconciliation_form_keeps_the_inbox_position(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    rows, _ = bench.entity_service(matter).list(matter.matter_id, WEB_ACTOR)
    alex = next(row for row in rows if row["display_name"] == "Alex Example")
    detail = client.get(f"/matters/{matter.slug}/entities/{alex['entity_id']}",
                        params={"kind": "people", "inbox_page": "2"}).text
    form = re.search(r'<form[^>]*>(?:(?!</form>).)*Mention to split(?:(?!</form>).)*</form>', detail, re.S)[0]
    assert '<input type="hidden" name="kind" value="people">' in form
    assert '<input type="hidden" name="inbox_page" value="2">' in form


def test_long_suggestion_names_wrap_instead_of_overflowing():
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1] / "src/case_intelligence/static/workspace-layout.css").read_text()
    rule = re.search(r"\.suggestion-title a \{([^}]*)\}", css)[1]
    assert "overflow-wrap: anywhere" in rule and "min-width: 0" in rule
    assert "overflow-wrap: anywhere" in re.search(r"\.notebook-tool-card h3 \{([^}]*)\}", css)[1]


def test_candidate_and_review_pages_keep_the_inbox_position(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    rows, _ = bench.entity_service(matter).list(matter.matter_id, WEB_ACTOR)
    alex = next(row for row in rows if row["display_name"] == "Alex Example")
    detail = client.get(f"/matters/{matter.slug}/entities/{alex['entity_id']}",
                        params={"kind": "people", "inbox_page": "2", "candidate_page": "2"}).text
    previous = re.search(r'<a href="([^"]+)">Previous candidates</a>', detail)[1]
    assert "kind=people" in previous and "inbox_page=2" in previous


def test_a_row_opens_the_identity_whose_passage_it_shows(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    upload(client, matter.slug, "Synthetic call.txt", SECOND)
    bench.run_automatic_discovery_once()
    service = bench.entity_service(matter)
    rows, _ = service.list(matter.matter_id, WEB_ACTOR)
    earliest, later = sorted((row for row in rows if row["display_name"] == "Alex Example"),
                             key=lambda row: (row["created_at"], row["entity_id"]))
    # A reviewer merges the earliest identity's passages into the other; both stay Suggested.
    service.reconcile(matter.matter_id, WEB_ACTOR, earliest["entity_id"], expected_revision=earliest["revision"],
                      target_id=later["entity_id"], target_revision=later["revision"], action="merge")
    assert {row["status"] for row in service.list(matter.matter_id, WEB_ACTOR)[0]
            if row["display_name"] == "Alex Example"} == {"suggested"}
    items, _total, _kinds = service.inbox(matter.matter_id, WEB_ACTOR)
    alex = next(item for item in items if item["display_name"] == "Alex Example")
    assert alex["first_mention"]["entity_id"] == later["entity_id"] == alex["entity_id"]
    page = client.get(f"/matters/{matter.slug}/entities").text
    link = re.search(r'<p class="suggestion-title"><a href="([^"]+)">Alex Example</a>', page)[1]
    assert f"/entities/{later['entity_id']}" in link


def test_guided_discovery_keeps_the_inbox_position(workbench):  # noqa: F811
    from case_intelligence.full_text_review import FullTextReviewLedger
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    upload(client, matter.slug, "Synthetic call.txt", SECOND)
    bench.run_automatic_discovery_once()
    bench.full_review.close()
    store = bench.workspace
    _, version = store.create_review_criterion(matter.matter_id, WEB_ACTOR,
        title="Synthetic guided review", instructions="Review synthetic people and dates.")
    store.queue_review_run(matter.matter_id, WEB_ACTOR, version.criterion_version_id,
                           run_kind="full", review_mode="full_text")
    run = store.claim_review_run("synthetic-guided-inventory")
    decision = store.next_review_decision(run.run_id)
    document = bench.source_store(matter).get(decision.document_id)
    FullTextReviewLedger(store).inventory(run, decision, document.parsed_units(), current_source=lambda: True)
    store.fail_review_run(run.run_id, "Synthetic partial inventory; no generation requested.")
    page = client.get(f"/matters/{matter.slug}/entities", params={"kind": "people", "inbox_page": 2}).text
    coverage = html.unescape(re.search(r'href="([^"]+)">Check readiness and discover suggestions', page)[1])
    assert "kind=people" in coverage and "inbox_page=2" in coverage
    discovery = client.get(coverage).text
    back = html.unescape(re.search(r'<a href="([^"]+)">People (?:&amp;|&) things</a>', discovery)[1])
    assert "kind=people" in back and "inbox_page=2" in back
    form = re.search(r'<form data-discovery-batch.*?</form>', discovery, re.S)[0]
    assert '<input type="hidden" name="kind" value="people">' in form
    assert '<input type="hidden" name="inbox_page" value="2">' in form
    other = client.get(coverage.replace("kind=people", "kind=unknown")).text
    back = html.unescape(re.search(r'<a href="([^"]+)">People (?:&amp;|&) things</a>', other)[1])
    assert "kind=unknown" not in back and "inbox_page=2" in back
    assert 'name="kind"' not in re.search(r'<form data-discovery-batch.*?</form>', other, re.S)[0]
    # The return after a batch keeps it too.
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', discovery)[1]
    done = client.post(f"/matters/{matter.slug}/entities/actions", follow_redirects=False, data=dict(
        csrf_token=csrf, action="discover", run_id=run.run_id, kind="people", inbox_page=2))
    assert done.status_code == 303 and "kind=people" in done.headers["location"]
    assert "inbox_page=2" in done.headers["location"]


def test_an_occurrence_past_the_retained_excerpt_shows_what_was_found(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    filler = "the crate sat quietly beside the loading dock while rain fell on the yard " * 6
    lines = [filler] * 19 + ["Late in the log, Riley Sample signed the receiving sheet."]
    assert len("\n".join(lines[:19])) > 6000
    upload(client, matter.slug, "Synthetic long log.txt", "\n".join(lines).encode())
    bench.run_automatic_discovery_once()
    items, _total, _kinds = bench.entity_service(matter).inbox(matter.matter_id, WEB_ACTOR)
    riley = next(item for item in items if item["display_name"] == "Riley Sample")["first_mention"]
    assert riley["start_offset"] > len(riley["excerpt"])
    assert riley["snippet"] == ("…", "Riley Sample", "…")
    row = _row(client.get(f"/matters/{matter.slug}/entities").text, "Riley Sample")
    assert "<mark>Riley Sample</mark>" in row and "the crate sat" not in row
    assert "Found later in this passage" in row


@pytest.mark.parametrize("revision", ["\u00b2", "9" * 5000, "-1", ""],
                         ids=["superscript", "over-conversion-limit", "negative", "empty"])
def test_a_malformed_batch_revision_is_refused_not_an_error(workbench, revision):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    rows, _ = bench.entity_service(matter).list(matter.matter_id, WEB_ACTOR)
    alex = next(row for row in rows if row["display_name"] == "Alex Example")
    response = client.post(f"/matters/{matter.slug}/entities/actions", data=dict(
        action="decide", status="confirmed", targets=f"{alex['entity_id']}:{revision}"))
    assert response.status_code == 409
    after = bench.entity_service(matter).list(matter.matter_id, WEB_ACTOR)[0]
    assert next(row for row in after if row["entity_id"] == alex["entity_id"])["status"] == "suggested"


def test_a_large_group_decides_the_identity_whose_passage_it_shows(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    connection = bench.workspace.connection
    with fixture_write(bench.workspace):
        for index in range(201):
            connection.execute(
                "INSERT INTO workbench_entity(entity_id,matter_id,entity_type,display_name,status,origin,"
                "created_by,created_at,updated_by,updated_at) VALUES (?,?,'person','Riley Placeholder','suggested',"
                "'extraction',?,'2026-01-01T00:00:00Z',?,'2026-01-01T00:00:00Z')",
                (f"synthetic-riley-{index:03d}", matter.matter_id, WEB_ACTOR, WEB_ACTOR))
    service = bench.entity_service(matter)
    rows, _ = service.list(matter.matter_id, WEB_ACTOR)
    alex = next(row for row in rows if row["display_name"] == "Alex Example")
    passage = service.detail(matter.matter_id, WEB_ACTOR, alex["entity_id"])[1][0]
    # The only passage belongs to the last identity by creation order, outside the first 200.
    last = service.detail(matter.matter_id, WEB_ACTOR, "synthetic-riley-200")[0]
    service.attach(matter.matter_id, WEB_ACTOR, last["entity_id"], expected_revision=last["revision"],
                   support=passage["support_token"])
    page = client.get(f"/matters/{matter.slug}/entities").text
    riley = _row(page, "Riley Placeholder")
    assert "/entities/synthetic-riley-200" in re.search(r'<p class="suggestion-title"><a href="([^"]+)"', riley)[1]
    targets = re.search(r'name="targets" value="([^"]+)"', riley)[1].split(",")
    assert len(targets) == 200 and targets[0].startswith("synthetic-riley-200:")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page)[1]
    response = client.post(f"/matters/{matter.slug}/entities/actions", follow_redirects=False, data=dict(
        csrf_token=csrf, action="decide", status="confirmed", targets=",".join(targets)))
    assert response.status_code == 303
    decided = service.detail(matter.matter_id, WEB_ACTOR, "synthetic-riley-200")[0]
    assert decided["status"] == "confirmed"


def test_exploring_connections_keeps_the_inbox_position(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, "Synthetic memo.txt", FIRST)
    bench.run_automatic_discovery_once()
    rows, _ = bench.entity_service(matter).list(matter.matter_id, WEB_ACTOR)
    alex = next(row for row in rows if row["display_name"] == "Alex Example")
    link = f"/matters/{matter.slug}/entities/{alex['entity_id']}?kind=people&inbox_page=2"
    detail = client.get(link).text
    explore = html.unescape(re.search(r'<a class="button button-secondary" href="([^"]+)">Explore connections</a>', detail)[1])
    graph = client.get(explore)
    assert graph.status_code == 200
    back = html.unescape(re.search(r'<a href="([^"]+)">Return to previous review</a>', graph.text)[1])
    assert "kind=people" in back and "inbox_page=2" in back
    returned = client.get(back).text
    everything = html.unescape(re.search(r'<a href="([^"]+)">All entities</a>', returned)[1])
    assert "kind=people" in everything and "inbox_page=2" in everything
