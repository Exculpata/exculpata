"""Synthetic regressions for the source reader: page fit, folders and find in file."""
from __future__ import annotations

import html
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.source_find import find_in_source, highlight_find
from case_intelligence.workbench import create_workbench_app
from tests.test_saved_answer_report_support import cedar  # noqa: F401
from tests.test_source_folder_navigation import _register
from tests.test_source_library import ACTOR, _matter


def _document(*texts, media_type="text/plain"):
    units = tuple(SimpleNamespace(text=text, number=index, location=f"Lines {index}")
                  for index, text in enumerate(texts, 1))
    return SimpleNamespace(state="ready", media_type=media_type, parsed_units=lambda: units)


def test_find_is_literal_case_insensitive_and_spans_line_breaks():
    document = _document("A synthetic Red\nBicycle was logged.", "No match here.",
                         "The red bicycle and the RED  BICYCLE returned.")
    result = find_in_source(document, "  red   bicycle ")
    assert result.query == "red bicycle"
    assert [(hit.unit_index, hit.count) for hit in result.sections] == [(1, 1), (3, 2)]
    assert result.total_matches == 3 and not result.truncated
    assert result.sections[0].match == "Red Bicycle"
    assert find_in_source(document, "re.d").total_matches == 0  # not a regular expression
    assert find_in_source(document, "   ") is None


def test_highlight_escapes_source_text_and_query():
    marked = highlight_find("<b>Synthetic</b> & synthetic", "synthetic")
    assert str(marked) == "&lt;b&gt;<mark>Synthetic</mark>&lt;/b&gt; &amp; <mark>synthetic</mark>"
    assert str(highlight_find("<i>x</i>", "")) == "&lt;i&gt;x&lt;/i&gt;"


def test_pdf_reader_fits_the_page_and_offers_width(cedar):  # noqa: F811
    client, bench, matter, documents = cedar
    token = bench.source_store(matter).action_token(documents["pdf"])
    page = client.get(f"/matters/{matter.slug}/sources/{token}", params={"unit": 1})
    assert page.status_code == 200
    iframe = re.search(r'<iframe[^>]+src="([^"]+)"[^>]*data-pdf-base="([^"]+)"', page.text)
    assert iframe is not None
    fragment = parse_qs(urlsplit(html.unescape(iframe.group(1))).fragment)
    assert fragment["view"] == ["Fit"] and fragment["navpanes"] == ["0"]
    assert html.unescape(iframe.group(2)).endswith("/content#page=1")
    assert 'data-pdf-fit="page" aria-pressed="true"' in page.text
    assert 'data-pdf-fit="width" aria-pressed="false"' in page.text


def test_find_in_file_lists_matching_sections_and_highlights_text(cedar):  # noqa: F811
    client, bench, matter, documents = cedar
    token = bench.source_store(matter).action_token(documents["pdf"])
    path = f"/matters/{matter.slug}/sources/{token}"
    page = client.get(path, params={"unit": 1, "q": "GAUGE"})
    assert page.status_code == 200
    assert 'aria-label="Find in this file"' in page.text
    assert "1 match for “GAUGE” in 1 section" in page.text
    assert "<mark>gauge</mark>" in page.text
    link = re.search(r'<a href="([^"]+)" aria-current="true"><strong>Page 1</strong>', page.text)
    assert link is not None
    target = urlsplit(html.unescape(link.group(1)))
    assert target.path == path and parse_qs(target.query)["unit"] == ["1"]
    assert parse_qs(target.query)["q"] == ["GAUGE"]

    missing = client.get(path, params={"unit": 1, "q": "<script>absent</script>"})
    assert missing.status_code == 200
    assert "No matches for “&lt;script&gt;absent&lt;/script&gt;”" in missing.text
    assert "<script>absent" not in missing.text
    assert "does not prove the words are absent" in missing.text

    plain = client.get(path, params={"unit": 1})
    assert "data-review-find-results" not in plain.text and "<mark>" not in plain.text


def test_reader_queue_steps_through_folders_without_leaving_the_source(tmp_path):
    app = create_workbench_app(tmp_path / "runtime", generator=UnavailableGenerator(),
                               auth_mode="test", background_ingestion=False)
    with TestClient(app) as client:
        slug = _matter(client, "Synthetic reader folders")
        bench = app.state.workbench
        matter = bench.matter(slug, ACTOR)
        documents = _register(bench, matter, [
            "Production/North/report.txt", "Production/North/Interviews/detail.txt",
            "Production/Northwest/report.txt", "Other/summary.txt"])
        store = bench.source_store(matter)
        token = store.action_token(documents["Production/North/report.txt"])
        path = f"/matters/{slug}/sources/{token}"

        root = client.get(path, params={"browse": "view=list&kind=TXT"})
        assert root.status_code == 200
        folders = re.search(r'data-source-browser-folders>(.*?)</nav>', root.text, re.S).group(1)
        names = re.findall(r"<span>([^<]+)</span><small>(\d+)</small>", folders)
        assert names == [("Other", "1"), ("Production", "3")]
        production = html.unescape(re.search(r'<a href="([^"]+)"><span>Production</span>', folders).group(1))
        target = urlsplit(production)
        assert target.path == path
        browse = parse_qs(parse_qs(target.query)["browse"][0])
        assert browse["folder"] == ["Production"] and browse["kind"] == ["TXT"]

        nested = client.get(path, params={"browse": "view=list&kind=TXT&folder=Production/North"})
        folders = re.search(r'data-source-browser-folders>(.*?)</nav>', nested.text, re.S).group(1)
        assert "<strong>Production/North</strong>" in folders
        assert re.findall(r"<span>([^<]+)</span>", folders) == ["Interviews"]
        parent = html.unescape(re.search(r'class="source-folder-parent" href="([^"]+)"', folders).group(1))
        assert parse_qs(parse_qs(urlsplit(parent).query)["browse"][0])["folder"] == ["Production"]
        assert "Northwest" not in folders

        # Folder steps keep the reader's own section and find state.
        kept = client.get(path, params={"browse": "view=list&kind=TXT&folder=Production/North",
                                        "unit": 1, "q": "synthetic"})
        folders = re.search(r'data-source-browser-folders>(.*?)</nav>', kept.text, re.S).group(1)
        for pattern in (r'class="source-folder-parent" href="([^"]+)"', r'<a href="([^"]+)"><span>Interviews</span>'):
            query = parse_qs(urlsplit(html.unescape(re.search(pattern, folders).group(1))).query)
            assert query["unit"] == ["1"] and query["q"] == ["synthetic"]
        # Nothing extra is added when the reader has no state of its own.
        plain = re.search(r'data-source-browser-folders>(.*?)</nav>', nested.text, re.S).group(1)
        query = parse_qs(urlsplit(html.unescape(re.search(r'<a href="([^"]+)"><span>Interviews</span>', plain).group(1))).query)
        assert set(query) == {"browse"}


def test_find_work_is_bounded_for_a_one_character_query_over_huge_sections():
    import tracemalloc
    from case_intelligence.source_find import MAX_FIND_MATCHES, MAX_FIND_SECTIONS
    # Maximum-size synthetic sections: every character matches.
    document = _document(*(["a" * 5_000_000] * 3))
    tracemalloc.start()
    result = find_in_source(document, "a")
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 5_000_000  # No per-match objects are retained.
    # Counting stops at the budget; later sections contribute their first
    # match. Every count that left matches uncounted is a lower bound.
    assert result.counts_capped and result.total_matches == MAX_FIND_MATCHES + 2
    assert result.matching_sections == 3 and len(result.sections) == 3
    assert [(hit.count, hit.partial) for hit in result.sections] == [
        (MAX_FIND_MATCHES, True), (1, True), (1, True)]
    # Exactly the budget is exact, not "at least".
    exact = find_in_source(_document("a" * MAX_FIND_MATCHES), "a")
    assert exact.total_matches == MAX_FIND_MATCHES and not exact.counts_capped
    assert not exact.sections[0].partial
    # After the budget, a later section with a single match is still exact.
    tail = find_in_source(_document("a" * (MAX_FIND_MATCHES + 1), "xyz", "one a"), "a")
    assert [(hit.count, hit.partial) for hit in tail.sections] == [(MAX_FIND_MATCHES, True), (1, False)]
    assert tail.counts_capped
    # Section totals stay exact beyond the display cap.
    many = find_in_source(_document(*(["one synthetic x"] * (MAX_FIND_SECTIONS + 50))), "x")
    assert many.matching_sections == MAX_FIND_SECTIONS + 50 and len(many.sections) == MAX_FIND_SECTIONS
    assert many.truncated and many.total_matches == MAX_FIND_SECTIONS + 50 and not many.counts_capped


def test_highlighting_is_bounded():
    from case_intelligence.source_find import MAX_HIGHLIGHTS
    marked = str(highlight_find("<" + "a" * (MAX_HIGHLIGHTS + 500), "a"))
    assert marked.count("<mark>") == MAX_HIGHLIGHTS
    assert marked.startswith("&lt;") and marked.endswith("a" * 500)


def test_find_survives_section_navigation_and_clear_keeps_review_origin(tmp_path):
    lines = "\n".join(f"Synthetic line {index} mentions the gauge." for index in range(1, 46))
    app = create_workbench_app(tmp_path / "runtime", generator=UnavailableGenerator(),
                               auth_mode="test", background_ingestion=False)
    with TestClient(app) as client:
        slug = _matter(client)
        client.post(f"/matters/{slug}/uploads",
                    files=[("files", ("Synthetic long log.txt", lines.encode(), "text/plain"))])
        bench = app.state.workbench
        matter = bench.matter(slug, ACTOR)
        store = bench.source_store(matter)
        document = next(iter(store.documents.values()))
        path = f"/matters/{slug}/sources/{store.action_token(document)}"
        origin = f"/matters/{slug}/exact-search?words=gauge"
        page = client.get(path, params={"unit": 1, "q": "gauge", "entity_return_to": origin})
        assert "45 matches for “gauge” in 3 sections" in page.text
        nxt = re.search(r'<a class="button button-secondary" href="([^"]+)">Next</a>', page.text)
        query = parse_qs(urlsplit(html.unescape(nxt.group(1))).query)
        assert query["q"] == ["gauge"] and query["unit"] == ["2"] and query["entity_return_to"] == [origin]
        following = client.get(html.unescape(nxt.group(1)))
        assert following.status_code == 200 and "data-review-find-results" in following.text
        assert "<mark>gauge</mark>" in following.text
        previous = re.search(r'<a class="button button-secondary" href="([^"]+)">Previous</a>', following.text)
        assert parse_qs(urlsplit(html.unescape(previous.group(1))).query)["q"] == ["gauge"]
        clear = re.search(r'<a href="([^"]+)" data-review-find-clear>Clear</a>', page.text)
        cleared = parse_qs(urlsplit(html.unescape(clear.group(1))).query)
        assert cleared["entity_return_to"] == [origin] and "q" not in cleared
        assert "Return to review context" in client.get(html.unescape(clear.group(1))).text
        plain = client.get(path, params={"unit": 1})
        nxt_plain = re.search(r'<a class="button button-secondary" href="([^"]+)">Next</a>', plain.text)
        assert "q" not in parse_qs(urlsplit(html.unescape(nxt_plain.group(1))).query)


def test_find_heading_counts_every_matching_section(cedar):  # noqa: F811
    client, bench, matter, documents = cedar
    token = bench.source_store(matter).action_token(documents["pdf"])
    page = client.get(f"/matters/{matter.slug}/sources/{token}", params={"unit": 1, "q": "gauge"})
    assert "1 match for “gauge” in 1 section" in page.text
