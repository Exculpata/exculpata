"""Synthetic acceptance for the production document list, viewer and saved views."""
import hashlib
import json
import re
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.modules import ModuleDatabase
from case_intelligence.modules.discovery_review import fields, load_files, reader
from case_intelligence.modules.discovery_review.views import ViewRepository
from case_intelligence.workbench import create_workbench_app
from tests.case_lifecycle_web_helpers import claim_bench_exit
from tests.test_intake_receipt_http import ACTOR, put, selection, upload
from tests.test_upload_occurrences import _settled

ROOT = Path(__file__).resolve().parents[1]
MEDIA = {".txt": "text/plain", ".csv": "text/csv", ".eml": "message/rfc822"}
EMAIL = (b"From: sender@example.test\r\nTo: reader@example.test\r\nSubject: Synthetic delivery note\r\n"
         b"Date: Mon, 03 Mar 2025 09:15:00 -0500\r\n\r\nSynthetic message body.\r\n")
LOAD_FILE = (
    "BEGBATES,ENDBATES,Custodian,NATIVEPATH,Review Tag\r\n"
    "SYN000010,SYN000012,\"Rivera, Alex\",VOL001\\NATIVES\\ledger.txt,Hot\r\n"
    "SYN000002,SYN000002,\"Chen, Morgan\",VOL001/NATIVES/note.eml,\r\n"
    "SYN000900,SYN000901,Nobody,VOL001/NATIVES/not-produced.txt,\r\n"
).encode()
PRODUCTION = {
    "VOL001/NATIVES/ledger.txt": b"Synthetic ledger line one.\nSynthetic ledger line two.\n",
    "VOL001/NATIVES/note.eml": EMAIL,
    "VOL001/DATA/load.csv": LOAD_FILE,
    "Loose/memo.txt": b"Synthetic memo outside the load file.\n",
}


def add_files(client, bench, matter, files, key="a"):
    paths = sorted(files)
    described = [{"name": path.rsplit("/", 1)[-1], "relative_path": path, "size": len(files[path]),
                  "media_type": MEDIA[Path(path).suffix]} for path in paths]
    ordinals = list(range(len(paths)))
    receipt = selection(client, matter.slug, described, ordinals, key=key * 32)
    session = upload(client, matter.slug, receipt, described, ordinals)
    for item, path in zip(session["items"], paths):
        put(client, item, files[path])
        assert client.post(item["finalize_url"]).status_code == 200
    _settled(bench, matter)


@pytest.fixture
def production(tmp_path):
    app = create_workbench_app(tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="test",
                               malware_scan_mode="disabled", background_ingestion=True, ingestion_workers=1)
    with TestClient(app) as client:
        response = client.post("/matters", data={"name": "Synthetic production", "descriptor": "Generated"},
                               follow_redirects=False)
        bench = app.state.workbench
        matter = bench.matter(response.headers["location"].split("/")[2], ACTOR)
        add_files(client, bench, matter, PRODUCTION)
        yield client, bench, matter


def rows(html):
    """Return each list row as its visible cell texts."""
    body = re.search(r"<tbody>(.*?)</tbody>", html, re.S).group(1)
    return [[unescape(re.sub(r"<[^>]+>", "", cell)).strip() for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            for row in re.findall(r"<tr[^>]*data-review-row[^>]*>(.*?)</tr>", body, re.S)]


def headers(html):
    return [unescape(re.sub(r"<[^>]+>|[▲▼]", "", cell)).strip()
            for cell in re.findall(r"<th[^>]*>(.*?)</th>", html, re.S)]


def token_for(bench, matter, name):
    store = bench.source_store(matter)
    return next(store.action_token(d) for d in store.documents.values() if d.display_name.endswith(name))


def test_list_leads_with_load_file_values_exactly_as_produced(production):
    client, bench, matter = production
    page = client.get(f"/matters/{matter.slug}/production")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert "4 documents in this matter" in page.text
    listed = rows(page.text)
    shown = headers(page.text)
    bates, name, custodian = (shown.index(label) for label in ("Begin Bates", "Name", "Custodian"))
    # Natural order: SYN000002 before SYN000010; items with no Bates follow, never invented.
    assert [row[bates] for row in listed[:2]] == ["SYN000002", "SYN000010"]
    assert [row[name] for row in listed[:2]] == ["note.eml", "ledger.txt"]
    assert listed[1][custodian] == "Rivera, Alex"
    assert {row[name] for row in listed[2:]} == {"load.csv", "memo.txt"}
    assert all(row[bates] == "—" for row in listed[2:])
    assert "2 of 3 rows matched an item" in page.text and "SYN000900" not in page.text
    assert "Back to Review" in page.text and "discovery-review.js" in page.text


def test_open_item_views_fields_and_email_only_fields(production):
    client, bench, matter = production
    base = f"/matters/{matter.slug}/production/items/"
    ledger = client.get(base + token_for(bench, matter, "ledger.txt") + "?view=fields").text
    assert "SYN000010 – SYN000012" in ledger and "Rivera, Alex" in ledger
    assert "load.csv, line 2 (matched by path)" in ledger
    assert "Review Tag" in ledger and "Hot" in ledger  # every load-file header stays reachable, as produced
    assert "sender@example.test" not in ledger and ">Email<" not in ledger and ">From<" not in ledger
    email = client.get(base + token_for(bench, matter, "note.eml") + "?view=fields").text
    assert ">Email<" in email and "sender@example.test" in email and "Synthetic delivery note" in email
    memo = client.get(base + token_for(bench, matter, "memo.txt") + "?view=fields").text
    assert "No Bates" in memo and "No load-file row is attached to this item." in memo
    text = client.get(base + token_for(bench, matter, "ledger.txt") + "?view=text").text
    assert "Synthetic ledger line two." in text and "Text extracted by" in text
    # A view the item does not have falls back to one it has instead of a blank pane.
    assert 'data-view="text"' in client.get(base + token_for(bench, matter, "ledger.txt") + "?view=document").text
    assert client.get(base + "0" * 32).status_code == 404
    assert client.get(base + "not-a-token").status_code == 404


def test_filter_sort_and_selection_follow_the_reviewer(production):
    client, bench, matter = production
    url = f"/matters/{matter.slug}/production"
    filtered = client.get(url, params={"q": "rivera"})
    assert [row[0] for row in rows(filtered.text)] == ["SYN000010"]
    assert "1 matches the filter" in filtered.text
    assert "No documents match this filter" in client.get(url, params={"q": "absent-term"}).text
    descending = rows(client.get(url, params={"sort": "name", "dir": "desc"}).text)
    names = [row[headers(filtered.text).index("Name")] for row in descending]
    assert names == sorted(names, key=str.casefold, reverse=True)
    token = token_for(bench, matter, "memo.txt")
    opened = client.get(url, params={"doc": token})
    assert f'data-token="{token}"' in re.search(r"<tr[^>]*aria-current[^>]*>", opened.text).group(0)
    assert re.search(r'<article[^>]*data-token="' + token, opened.text)


def test_other_matters_and_non_members_see_nothing(production):
    client, bench, matter = production
    response = client.post("/matters", data={"name": "Synthetic neighbor", "descriptor": "Generated"},
                           follow_redirects=False)
    neighbor = bench.matter(response.headers["location"].split("/")[2], ACTOR)
    add_files(client, bench, neighbor, {"only.txt": b"Synthetic neighbor record.\n"}, key="b")
    page = client.get(f"/matters/{neighbor.slug}/production")
    assert "1 document in this matter" in page.text and "SYN000010" not in page.text
    # With no load file the empty standard columns step aside, and the page says so.
    assert "No load file is matched to these items" in page.text
    assert "Hidden because no item has a value: Begin Bates, End Bates" in page.text
    assert "Begin Bates" not in headers(page.text)
    assert "Begin Bates" in headers(client.get(f"/matters/{neighbor.slug}/production?all_columns=1").text)
    foreign = token_for(bench, matter, "ledger.txt")
    assert client.get(f"/matters/{neighbor.slug}/production/items/{foreign}").status_code == 404
    principal = bench.workspace.upsert_principal("test", "other", "Synthetic other", "other")
    private = bench.workspace.create_matter("Synthetic private matter", "Synthetic", principal.principal_id)
    assert client.get(f"/matters/{private.slug}/production").status_code == 404
    assert client.get(f"/matters/{private.slug}/production/items/{foreign}").status_code == 404
    assert client.post(f"/matters/{private.slug}/production/view", data={"columns": ["name"]}).status_code == 404


def test_saved_views_change_columns_and_reset_restores_the_definition(production):
    client, bench, matter = production
    url = f"/matters/{matter.slug}/production"
    standard = headers(client.get(url).text)
    saved = client.post(url + "/view", data={"columns": ["custodian", "lf:Review Tag", "name", "unknown_key"],
                                             "item_fields": ["custodian"], "scope": "user"}, follow_redirects=False)
    assert saved.status_code == 303 and saved.headers["location"].endswith("notice=saved")
    page = client.get(url)
    assert headers(page.text) == ["Custodian", "Review Tag", "Name"]
    assert ["Rivera, Alex", "Hot", "ledger.txt"] in rows(page.text)
    # An item without that load-file field shows it as absent, not as an empty value.
    assert "has no such field" in page.text
    repository = ViewRepository(ModuleDatabase(bench.workspace.connection, bench.workspace._lock))
    repository.save(matter.matter_id, "matter", ACTOR, ("name",), ())
    assert headers(client.get(url).text) == ["Custodian", "Review Tag", "Name"]  # own view wins
    assert client.post(url + "/view", data={"action": "reset", "scope": "user"}, follow_redirects=False).status_code == 303
    assert headers(client.get(url).text) == ["Name"]  # then the matter's shared view
    assert client.post(url + "/view", data={"action": "reset", "scope": "matter"}, follow_redirects=False).status_code == 303
    assert headers(client.get(url).text) == standard
    assert client.post(url + "/view", data={"scope": "everyone", "columns": ["name"]}).status_code == 400


def test_changing_the_definition_changes_defaults_without_code(production, monkeypatch):
    client, bench, matter = production
    data = json.loads((ROOT / "src/case_intelligence/config/discovery_review_fields.json").read_text())
    data["fields"].append({"key": "review_tag", "label": "Tag", "group": "identity",
                           "load_file_headers": ["review tag"]})
    data["default_columns"] = ["review_tag", "name"]
    data["default_item_fields"] = ["review_tag"]
    changed = fields.parse_definition(data)
    monkeypatch.setattr(fields, "load_definition", lambda: changed)
    page = client.get(f"/matters/{matter.slug}/production")
    assert headers(page.text) == ["Tag", "Name"]
    assert ["Hot", "ledger.txt"] in rows(page.text)
    item = client.get(f"/matters/{matter.slug}/production/items/{token_for(bench, matter, 'ledger.txt')}?view=fields").text
    primary = item.split("dr-field-more")[0]
    assert ">Tag<" in primary and ">Custodian<" not in primary and ">Custodian<" in item


@pytest.mark.parametrize("damage", [
    lambda d: d.update(version=2),
    lambda d: d["default_columns"].append("missing_field"),
    lambda d: d["fields"].append({"key": "extra", "label": "Extra", "group": "identity"}),
    lambda d: d["fields"].append({"key": "second_begin", "label": "Second", "group": "identity",
                                  "load_file_headers": ["beg bates"]}),
    lambda d: d["fields"][0].update(group="missing_group"),
])
def test_malformed_definition_is_rejected_whole(damage):
    data = json.loads((ROOT / "src/case_intelligence/config/discovery_review_fields.json").read_text())
    fields.parse_definition(data)
    damage(data)
    with pytest.raises(fields.DefinitionError):
        fields.parse_definition(data)


def test_load_file_rows_attach_only_when_unambiguous():
    definition = fields.load_definition()
    table = load_files.parse(
        ("BegDoc#,Native Link,Note,Note\n"
         "SYN1,natives/a.txt,first,second\n"      # unique file name under a different root
         "SYN2,b.txt,,\n"                         # two files share this name: attaches to neither
         "SYN3,,,\n"                              # no path: the Bates-named file
         "SYN4,missing.txt,,\n"
         "SYN1,VOL/natives/a.txt,again,\n").encode(),
        document_id="load", name="load.csv", definition=definition)
    assert table.headers == ("BegDoc#", "Native Link", "Note", "Note (2)")
    paths = {"a": "VOL/natives/a.txt", "b1": "one/b.txt", "b2": "two/b.txt", "c": "images/syn3.pdf"}
    matches, reports = load_files.attach([table], paths)
    assert {key: (m.line, m.basis) for key, m in matches.items()} == {
        "a": (2, "file name"), "c": (4, "Bates-named file")}
    assert matches["a"].values == {"BegDoc#": "SYN1", "Native Link": "natives/a.txt", "Note": "first", "Note (2)": "second"}
    assert (reports[0].rows, reports[0].matched, reports[0].extra) == (5, 2, 1)
    assert load_files.parse(b"Title,Amount\nSynthetic,4\n", document_id="x", name="table.csv", definition=definition) is None
    short = load_files.parse(b"BEGBATES,PATH\nSYN1,a.txt\nSYN2\n", document_id="x", name="l.csv", definition=definition)
    assert len(short.rows) == 1 and "Line 3" in short.problem


def test_review_reads_never_change_originals_and_deletion_leaves_no_view_state(production):
    client, bench, matter = production
    store = bench.source_store(matter)
    before = {d.document_id: hashlib.sha256(store.source_path(d.document_id).read_bytes()).hexdigest()
              for d in store.documents.values()}
    url = f"/matters/{matter.slug}/production"
    for name in ("ledger.txt", "note.eml"):
        for view in ("text", "fields"):
            assert client.get(f"{url}/items/{token_for(bench, matter, name)}?view={view}").status_code == 200
    assert client.post(url + "/view", data={"columns": ["name"], "scope": "matter"}, follow_redirects=False).status_code == 303
    assert before == {d.document_id: hashlib.sha256(store.source_path(d.document_id, verify_digest=True).read_bytes()).hexdigest()
                      for d in store.documents.values()}
    count = "SELECT COUNT(*) FROM workbench_discovery_view WHERE matter_id=?"
    assert bench.workspace.connection.execute(count, (matter.matter_id,)).fetchone()[0] == 1
    assert matter.matter_id in reader._cache
    prepared, lifecycle = claim_bench_exit(bench, matter)
    assert bench.execute_matter_purge(prepared, lifecycle).state == "deleted"
    assert bench.workspace.connection.execute(count, (matter.matter_id,)).fetchone()[0] == 0
    assert matter.matter_id not in reader._cache


def test_migration_mirror_matches_packaged_copy():
    name = "migrations/sqlite/0045_discovery_review_view.sql"
    assert (ROOT / name).read_bytes() == (ROOT / "src/case_intelligence" / name).read_bytes()
