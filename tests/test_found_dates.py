"""Synthetic regression: found dates form a read-only draft on the timeline."""
from __future__ import annotations

from tests.fixture_write import fixture_write

import hashlib
import html
import re
from urllib.parse import parse_qs, urlsplit

from tests.test_automatic_discovery import upload, workbench  # noqa: F401
from tests.test_matter_notebook import WEB_ACTOR

SOURCES = (
    ("Synthetic delivery.txt", b"The crate was delivered on 2026-03-05 at the north dock."),
    ("Synthetic inspection.txt", b"Inspection began 2026-01-02T09:30Z before the shift change."),
    ("Synthetic call log.txt", b"A call was logged on 03/04/2026 about the crate."),
    ("Synthetic form.txt", b"The form is dated 2026-02-30 in the header."),
)


def section(page: str) -> str:
    found = re.search(r'<section class="found-dates".*?</section>', page, re.S)
    return found[0] if found else ""


def stated(fragment: str) -> list[str]:
    return re.findall(r'<p class="found-date-when"><strong>([^<]+)</strong>', fragment)


def prepare(client, bench, matter, sources=SOURCES):
    for name, content in sources:
        upload(client, matter.slug, name, content)
    bench.run_automatic_discovery_once()


def test_full_dates_are_ordered_and_uncertain_ones_are_never_guessed(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    prepare(client, bench, matter)
    page = client.get(f"/matters/{matter.slug}/chronology")
    assert page.status_code == 200
    draft = section(page.text)
    assert '<h2 id="found-dates-heading">Dates found in sources <span class="suggestion-total">4</span></h2>' in draft
    ordered = re.search(r"<h3>In date order</h3><ol class=\"found-date-list\">(.*?)</ol>", draft, re.S)[1]
    unordered = re.search(r"<h3>Order not established</h3><ul class=\"found-date-list\">(.*?)</ul>", draft, re.S)[1]
    assert stated(ordered) == ["2026-01-02T09:30Z", "2026-03-05"]
    # Day/month order unknown, and an impossible calendar day: kept apart, in the order found.
    assert stated(unordered) == ["03/04/2026", "2026-02-30"]
    assert "day/month order unresolved" in unordered and "calendar date not established" in unordered
    assert "2 of 4 are full calendar dates" in draft
    assert "<mark>2026-03-05</mark>" in ordered
    assert re.search(rf'class="suggestion-source" href="/matters/{matter.slug}\?support=[0-9a-f]{{40}}#support-pane">'
                     r"Synthetic delivery\.txt", ordered)


def test_the_draft_saves_nothing_and_skips_dates_set_aside(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    prepare(client, bench, matter)
    service = bench.entity_service(matter)
    rows, _ = service.list(matter.matter_id, WEB_ACTOR)
    by_name = {row["display_name"]: row for row in rows}
    service.decide(matter.matter_id, WEB_ACTOR, [(by_name["03/04/2026"]["entity_id"], by_name["03/04/2026"]["revision"])],
                   status="dismissed")
    march = service.detail(matter.matter_id, WEB_ACTOR, by_name["2026-03-05"]["entity_id"])
    service.review_mention(matter.matter_id, WEB_ACTOR, march[0]["entity_id"], expected_revision=march[0]["revision"],
                           mention_id=march[1][0]["mention_id"], status="dismissed")
    draft = section(client.get(f"/matters/{matter.slug}/chronology").text)
    assert sorted(stated(draft)) == ["2026-01-02T09:30Z", "2026-02-30"]
    assert "viewing it saves and confirms nothing" in draft
    assert bench.assertion_service(matter).list(matter.matter_id, WEB_ACTOR)[1] == 0


def test_an_identity_timeline_and_an_empty_matter_show_no_draft(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    assert section(client.get(f"/matters/{matter.slug}/chronology").text) == ""
    prepare(client, bench, matter, SOURCES[:1])
    rows, _ = bench.entity_service(matter).list(matter.matter_id, WEB_ACTOR)
    page = client.get(f"/matters/{matter.slug}/chronology", params={"entity_id": rows[0]["entity_id"]})
    assert page.status_code == 200 and section(page.text) == ""


def test_a_changed_source_is_shown_as_retained_history(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    prepare(client, bench, matter, SOURCES[:2])
    connection = bench.workspace.connection
    with fixture_write(bench.workspace):  # The inspection passage now cites an earlier source version.
        connection.execute("UPDATE workbench_entity_mention SET source_version_id='superseded-version' "
                           "WHERE matter_id=? AND source_name='Synthetic inspection.txt'", (matter.matter_id,))
    draft = section(client.get(f"/matters/{matter.slug}/chronology").text)
    entries = re.findall(r'<li class="found-date">.*?</li>', draft, re.S)
    inspection = next(entry for entry in entries if "2026-01-02T09:30Z" in entry)
    delivery = next(entry for entry in entries if "2026-03-05" in entry)
    assert "?support=" not in inspection and "original passage changed or is unavailable" in inspection
    assert 'class="suggestion-source" href=' in delivery


def test_a_renamed_date_keeps_the_date_as_stated(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    prepare(client, bench, matter, SOURCES[:1])
    service = bench.entity_service(matter)
    row = next(row for row in service.list(matter.matter_id, WEB_ACTOR)[0] if row["display_name"] == "2026-03-05")
    service.update(matter.matter_id, WEB_ACTOR, row["entity_id"], expected_revision=row["revision"],
                   display_name="Delivery day", entity_type="date", status="suggested", aliases="")
    draft = section(client.get(f"/matters/{matter.slug}/chronology").text)
    assert stated(draft) == ["2026-03-05"] and "<h3>In date order</h3>" in draft


def _add_dates(bench, matter, sample, count, start):
    connection = bench.workspace.connection
    with fixture_write(bench.workspace):  # Background work shares this connection.
        for index in range(start, start + count):
            day = f"2025-{1 + index // 28 % 12:02d}-{1 + index % 28:02d}"
            entity_id = f"synthetic-date-{index:06d}"
            connection.execute(
                "INSERT INTO workbench_entity(entity_id,matter_id,entity_type,display_name,status,origin,"
                "created_by,created_at,updated_by,updated_at) VALUES (?,?,'date',?,'suggested','extraction',"
                "?,'2026-01-01T00:00:00Z',?,'2026-01-01T00:00:00Z')",
                (entity_id, matter.matter_id, day, WEB_ACTOR, WEB_ACTOR))
            connection.execute(
                "INSERT INTO workbench_entity_mention(mention_id,matter_id,entity_id,document_id,source_version_id,"
                "source_name,location,unit_number,chunk_id,excerpt_digest,excerpt,support_token,origin,created_by,"
                "created_at,surface_text,date_json) VALUES (?,?,?,?,?,?,?,1,?,?,?,?,'extraction',?,'2026-01-01T00:00:00Z',?,"
                "'{\"kind\": \"date\", \"date\": {}}')",
                (f"synthetic-date-mention-{index:06d}", matter.matter_id, entity_id, sample["document_id"],
                 sample["source_version_id"], sample["source_name"], sample["location"], sample["chunk_id"],
                 hashlib.sha256(str(index).encode()).hexdigest(), f"Logged {day}.", sample["support_token"],
                 WEB_ACTOR, day))


def test_found_dates_page_in_order_and_keep_the_timeline_page(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    prepare(client, bench, matter, SOURCES[:1])
    service = bench.entity_service(matter)
    row = service.list(matter.matter_id, WEB_ACTOR)[0][0]
    sample = service.detail(matter.matter_id, WEB_ACTOR, row["entity_id"])[1][0]
    _add_dates(bench, matter, sample, 29, 0)
    first = section(client.get(f"/matters/{matter.slug}/chronology").text)
    assert len(stated(first)) == 25 and stated(first) == sorted(stated(first))
    more = html.unescape(re.search(r'<a href="([^"]+)">More dates</a>', first)[1])
    assert "dates_page=2" in more and "page=1" in more and more.endswith("#found-dates-heading")
    second = section(client.get(more.split("#")[0]).text)
    assert len(stated(second)) == 5 and min(stated(second)) >= max(stated(first))
    assert "Previous dates" in second and "More dates" not in second


def test_found_date_work_grows_linearly(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    prepare(client, bench, matter, SOURCES[:1])
    service = bench.entity_service(matter)
    row = service.list(matter.matter_id, WEB_ACTOR)[0][0]
    sample = service.detail(matter.matter_id, WEB_ACTOR, row["entity_id"])[1][0]
    connection = bench.workspace.connection

    def steps():
        counted = [0]

        def tick():
            counted[0] += 1
            return 0
        with bench.workspace._lock:
            connection.set_progress_handler(tick, 1000)
        try:
            items, total, _ordered = service.date_draft(matter.matter_id, WEB_ACTOR)
        finally:
            with bench.workspace._lock:
                connection.set_progress_handler(None, 0)
        assert len(items) == 25
        return counted[0], total

    _add_dates(bench, matter, sample, 1000, 0)
    small, total = steps()
    _add_dates(bench, matter, sample, 3000, 1000)
    large, larger_total = steps()
    assert larger_total - total == 3000
    # One pass plus a sort: four times the dates costs about four times the work.
    assert large <= 6 * max(small, 1), (small, large)


def test_reviewing_a_found_date_can_return_to_the_same_timeline_position(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    prepare(client, bench, matter, SOURCES[:1])
    service = bench.entity_service(matter)
    row = service.list(matter.matter_id, WEB_ACTOR)[0][0]
    sample = service.detail(matter.matter_id, WEB_ACTOR, row["entity_id"])[1][0]
    _add_dates(bench, matter, sample, 29, 0)
    page = client.get(f"/matters/{matter.slug}/chronology", params={"dates_page": 2}).text
    draft = section(page)
    links = re.findall(r'<a href="([^"]+)" aria-label="([^"]+)">Review this date</a>', draft)
    assert len(links) == 5 and len({label for _href, label in links}) == 5
    assert all(label.startswith("Review date ") and " from " in label for _href, label in links)
    opened = client.get(html.unescape(links[0][0])).text
    back = html.unescape(re.search(r'<a href="([^"]+)">Return to source review</a>', opened)[1])
    assert back.startswith(f"/matters/{matter.slug}/chronology?") and "dates_page=2" in back
    assert back.endswith("#found-dates-heading")


def test_paging_saved_records_keeps_the_found_date_page(workbench, monkeypatch):  # noqa: F811
    client, bench, matter, _runtime = workbench
    prepare(client, bench, matter, SOURCES[:1])
    service = bench.assertion_service(matter)
    original = service.list
    # More than one page of saved records, without creating fifty synthetic records.
    monkeypatch.setattr(service.__class__, "list", lambda self, *args, **kwargs: (original(*args, **kwargs)[0], 120))
    page = client.get(f"/matters/{matter.slug}/chronology", params={"dates_page": 3, "page": 2}).text
    nav = re.search(r'<nav aria-label="Chronology pages">(.*?)</nav>', page, re.S)[1]
    links = [html.unescape(href) for href in re.findall(r'href="([^"]+)"', nav)]
    assert links and all("dates_page=3" in href for href in links)


def test_only_passages_discovery_found_are_listed_whichever_identity_holds_them(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    prepare(client, bench, matter, SOURCES[:3])
    service = bench.entity_service(matter)
    rows = {row["display_name"]: row for row in service.list(matter.matter_id, WEB_ACTOR)[0]}
    march = rows["2026-03-05"]
    call = service.detail(matter.matter_id, WEB_ACTOR, rows["03/04/2026"]["entity_id"])[1][0]
    # A reviewer attaches an unrelated passage to a found date: it is the reviewer's, not a found date.
    service.attach(matter.matter_id, WEB_ACTOR, march["entity_id"], expected_revision=march["revision"],
                   support=call["support_token"])
    draft = section(client.get(f"/matters/{matter.slug}/chronology").text)
    assert sorted(stated(draft)) == ["03/04/2026", "2026-01-02T09:30Z", "2026-03-05"]
    # A found passage split into a reviewer-created date identity is still a found date.
    manual = service.create(matter.matter_id, WEB_ACTOR, display_name="Inspection day", entity_type="date",
                            status="confirmed", aliases="")
    inspection = rows["2026-01-02T09:30Z"]
    found = service.detail(matter.matter_id, WEB_ACTOR, inspection["entity_id"])[1][0]
    service.reconcile(matter.matter_id, WEB_ACTOR, inspection["entity_id"], expected_revision=inspection["revision"],
                      target_id=manual["entity_id"], target_revision=manual["revision"], action="split",
                      mention_ids=[found["mention_id"]])
    draft = section(client.get(f"/matters/{matter.slug}/chronology").text)
    assert sorted(stated(draft)) == ["03/04/2026", "2026-01-02T09:30Z", "2026-03-05"]


def test_calendar_validity_does_not_depend_on_the_sqlite_build(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    prepare(client, bench, matter, (
        ("Synthetic leap.txt", b"Filed 2028-02-29 and again 2026-12-31 at noon."),
        ("Synthetic bad.txt", b"Copied 2026-02-29, 2026-04-31, 2026-13-01 and 2026-00-10 by hand."),
        ("Synthetic year zero.txt", b"A template stamp reads 0000-02-29 in the footer."),
    ))
    connection = bench.workspace.connection
    # Some SQLite builds return an impossible day from date() unchanged; behave like one.
    with bench.workspace._lock:
        connection.create_function("date", 1, lambda value: value, deterministic=True)
    try:
        draft = section(client.get(f"/matters/{matter.slug}/chronology").text)
    finally:
        with bench.workspace._lock:
            connection.create_function("date", 1, None)
    ordered = re.search(r"<h3>In date order</h3><ol class=\"found-date-list\">(.*?)</ol>", draft, re.S)[1]
    unordered = re.search(r"<h3>Order not established</h3><ul class=\"found-date-list\">(.*?)</ul>", draft, re.S)[1]
    assert stated(ordered) == ["2026-12-31", "2028-02-29"]
    assert sorted(stated(unordered)) == ["0000-02-29", "2026-00-10", "2026-02-29", "2026-04-31", "2026-13-01"]


def test_a_passage_found_as_another_kind_is_not_listed_after_retyping_or_merging(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    prepare(client, bench, matter, (
        ("Synthetic shift log.txt", b"Morgan Ellis inspected the crate on 2026-03-05 at Synthetic Freight Ltd."),
    ))
    service = bench.entity_service(matter)
    rows = service.list(matter.matter_id, WEB_ACTOR)[0]
    others = [row for row in rows if row["entity_type"] != "date"]
    date = next(row for row in rows if row["entity_type"] == "date")
    assert len(others) >= 2, [(row["display_name"], row["entity_type"]) for row in rows]
    # A reviewer retypes one found name as a date, and merges another into the found date.
    retyped, merged = others[0], others[1]
    service.update(matter.matter_id, WEB_ACTOR, retyped["entity_id"], expected_revision=retyped["revision"],
                   display_name=retyped["display_name"], entity_type="date", status="suggested", aliases="")
    service.reconcile(matter.matter_id, WEB_ACTOR, merged["entity_id"], expected_revision=merged["revision"],
                      target_id=date["entity_id"], target_revision=date["revision"], action="merge")
    draft = section(client.get(f"/matters/{matter.slug}/chronology").text)
    assert stated(draft) == ["2026-03-05"]


def test_repeated_dates_in_one_passage_have_distinct_review_link_names(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    prepare(client, bench, matter, (
        ("Synthetic notice.txt", b"Payment due 2026-03-05; the reminder repeats 2026-03-05 at the foot."),
    ))
    draft = section(client.get(f"/matters/{matter.slug}/chronology").text)
    labels = re.findall(r'aria-label="([^"]+)">Review this date</a>', draft)
    assert stated(draft) == ["2026-03-05", "2026-03-05"]
    assert len(set(labels)) == 2 and all("Synthetic notice.txt" in label for label in labels)


def test_an_entry_shows_its_passage_review_apart_from_its_identity(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    prepare(client, bench, matter, SOURCES[:1])
    service = bench.entity_service(matter)
    row = service.list(matter.matter_id, WEB_ACTOR)[0][0]
    service.decide(matter.matter_id, WEB_ACTOR, [(row["entity_id"], row["revision"])], status="confirmed")
    entity, mentions = service.detail(matter.matter_id, WEB_ACTOR, row["entity_id"])[:2]
    service.review_mention(matter.matter_id, WEB_ACTOR, entity["entity_id"], expected_revision=entity["revision"],
                           mention_id=mentions[0]["mention_id"], status="disputed")
    draft = section(client.get(f"/matters/{matter.slug}/chronology").text)
    when = re.search(r'<p class="found-date-when">(.*?)</p>', draft, re.S)[1]
    assert '<span class="suggestion-type">Identity: Confirmed</span>' in when
    assert '<span class="suggestion-type">Passage: Disputed</span>' in when


def test_reviewing_a_found_date_keeps_the_selected_passage_and_its_return(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    prepare(client, bench, matter, SOURCES[:2])
    service = bench.entity_service(matter)
    row = next(row for row in service.list(matter.matter_id, WEB_ACTOR)[0] if row["display_name"] == "2026-03-05")
    token = service.detail(matter.matter_id, WEB_ACTOR, row["entity_id"])[1][0]["support_token"]
    source_review = f"/matters/{matter.slug}?support={token}"
    page = client.get(f"/matters/{matter.slug}/chronology", params={"support": token, "return_to": source_review}).text
    link = html.unescape(re.findall(r'<a href="([^"]+)" aria-label="[^"]+">Review this date</a>', section(page))[0])
    opened = client.get(link).text
    back = html.unescape(re.search(r'<a href="([^"]+)">Return to source review</a>', opened)[1])
    assert back.startswith(f"/matters/{matter.slug}/chronology?") and back.endswith("#found-dates-heading")
    query = parse_qs(urlsplit(back).query)
    assert query["support"] == [token] and query["return_to"] == [source_review]
    assert client.get(back.split("#")[0]).status_code == 200


def test_only_a_date_or_a_stated_time_after_it_is_placed_in_order(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    prepare(client, bench, matter, SOURCES[:1])
    service = bench.entity_service(matter)
    row = service.list(matter.matter_id, WEB_ACTOR)[0][0]
    sample = service.detail(matter.matter_id, WEB_ACTOR, row["entity_id"])[1][0]
    _add_dates(bench, matter, sample, 6, 0)
    stated_forms = ["2025-01-02 draft", "2025-01-03T09", "2025-01-04T09:30Zed",
                    "2025-01-05 09:30", "2025-01-06T09:30:15+01:00", "2025-01-07T09:30Z"]
    connection = bench.workspace.connection
    with fixture_write(bench.workspace):  # A replacement extractor may emit other date-shaped labels.
        for index, value in enumerate(stated_forms):
            connection.execute("UPDATE workbench_entity_mention SET surface_text=? WHERE mention_id=?",
                               (value, f"synthetic-date-mention-{index:06d}"))
    draft = section(client.get(f"/matters/{matter.slug}/chronology").text)
    ordered = re.search(r"<h3>In date order</h3><ol class=\"found-date-list\">(.*?)</ol>", draft, re.S)[1]
    unordered = re.search(r"<h3>Order not established</h3><ul class=\"found-date-list\">(.*?)</ul>", draft, re.S)[1]
    assert {"2025-01-05 09:30", "2025-01-06T09:30:15+01:00", "2025-01-07T09:30Z"} <= set(stated(ordered))
    assert {"2025-01-02 draft", "2025-01-03T09", "2025-01-04T09:30Zed"} == set(stated(unordered))


def test_a_long_return_path_still_opens_the_date_identity(workbench):  # noqa: F811
    client, bench, matter, _runtime = workbench
    prepare(client, bench, matter, SOURCES[:1])
    service = bench.entity_service(matter)
    token = service.detail(matter.matter_id, WEB_ACTOR,
                           service.list(matter.matter_id, WEB_ACTOR)[0][0]["entity_id"])[1][0]["support_token"]
    # A same-matter return path just inside the accepted limit, which grows when nested.
    long_return = f"/matters/{matter.slug}?q=" + "a%2Fb" * ((3970 - len(matter.slug)) // 5)
    assert 3950 <= len(long_return) <= 4000
    page = client.get(f"/matters/{matter.slug}/chronology", params={"support": token, "return_to": long_return}).text
    link = html.unescape(re.findall(r'<a href="([^"]+)" aria-label="[^"]+">Review this date</a>', section(page))[0])
    assert client.get(link).status_code == 200
    back = parse_qs(urlsplit(link).query)["return_to"][0]
    # The nested return path is dropped to fit; the selected passage is kept.
    assert len(back) <= 4000 and parse_qs(urlsplit(back).query)["support"] == [token]
