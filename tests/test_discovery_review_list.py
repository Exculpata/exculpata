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
    assert "No identifier" in memo and "No load-file row is attached to this item." in memo
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
    repository = ViewRepository(ModuleDatabase(bench.workspace.connection, bench.workspace._lock), bench.workspace.membership)
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


def test_team_identifiers_number_unlabelled_items_without_touching_produced_bates(production):
    client, bench, matter = production
    url = f"/matters/{matter.slug}/production"

    def assign(**data):
        response = client.post(url + "/identifiers", data=data, follow_redirects=False)
        assert response.status_code == 303
        return response.headers["location"].rsplit("notice=", 1)[1]

    def team_ids():
        page = client.get(url, params={"sort": "name"})
        shown = headers(page.text)
        if "Team ID" not in shown:
            return {}
        return {row[shown.index("Name")]: row[shown.index("Team ID")] for row in rows(page.text)}

    assert team_ids() == {}  # the empty standard column stays out of the way
    assert assign(prefix="9bad", start=1, digits=4) == "invalid"
    assert assign(prefix="DEF", start=99, digits=2) == "invalid"  # two digits cannot hold item 100
    assert assign(prefix="syn", start=10, digits=6) == "conflict"  # SYN000010 is a produced Bates number
    assert team_ids() == {}
    assert assign(prefix="DEF", start=1, digits=4, sort="name") == "assigned"
    # Only the two items no load file names are numbered, in the listed (name) order.
    assert team_ids() == {"ledger.txt": "", "load.csv": "DEF0001", "memo.txt": "DEF0002", "note.eml": ""}
    memo = client.get(f"{url}/items/{token_for(bench, matter, 'memo.txt')}?view=fields").text
    assert "DEF0002" in memo and "No identifier" not in memo
    ledger = client.get(f"{url}/items/{token_for(bench, matter, 'ledger.txt')}?view=fields").text
    assert "SYN000010 – SYN000012" in ledger and "DEF0" not in ledger
    assert assign(prefix="DEF", start=1, digits=4) == "nothing"
    assert assign(prefix="DEF", start=2, digits=4, include_produced="1") == "conflict"
    assert assign(prefix="DEF", start=3, digits=4, include_produced="1", q="ledger") == "assigned"
    assert team_ids()["ledger.txt"] == "DEF0003" and team_ids()["note.eml"] == ""
    assert assign(action="clear") == "confirm"
    assert team_ids()["memo.txt"] == "DEF0002"
    assert assign(action="clear", confirm="1", q="memo") == "cleared"
    assert team_ids() == {"ledger.txt": "DEF0003", "load.csv": "DEF0001", "memo.txt": "", "note.eml": ""}
    count = "SELECT COUNT(*) FROM workbench_discovery_identifier WHERE matter_id=?"
    assert bench.workspace.connection.execute(count, (matter.matter_id,)).fetchone()[0] == 2
    prepared, lifecycle = claim_bench_exit(bench, matter)
    assert bench.execute_matter_purge(prepared, lifecycle).state == "deleted"
    assert bench.workspace.connection.execute(count, (matter.matter_id,)).fetchone()[0] == 0


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
    for name in ("0045_discovery_review_view.sql", "0046_discovery_review_identifier.sql", "0047_discovery_identifier_case_uniqueness.sql"):
        path = "migrations/sqlite/" + name
        assert (ROOT / path).read_bytes() == (ROOT / "src/case_intelligence" / path).read_bytes()


@pytest.mark.parametrize('delimiter,suffix', [(',', 'csv'), ('\t', 'tsv')])
def test_load_file_preserves_complete_values_in_every_column(delimiter, suffix):
    import csv
    import io
    value = '  ' + '7' * 5000 + '\nend  '
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, delimiter=delimiter)
    writer.writerow(['PATH', 'BEGBATES', 'Note'])
    writer.writerow([value, value, value])
    parsed = load_files.parse(stream.getvalue().encode(), document_id='synthetic',
                              name='load.' + suffix, definition=fields.load_definition())
    assert not parsed.problem
    assert list(parsed.rows[0].values.values()) == [value] * 3


def test_duplicate_headers_preserve_all_columns_with_reserved_ordinal_names():
    for headings in [('PATH', 'Note', 'Note', 'Note (2)'), ('PATH', 'Note (2)', 'Note', 'Note')]:
        parsed = load_files.parse((','.join(headings) + '\na,b,c,d\n').encode(), document_id='synthetic',
                                  name='load.csv', definition=fields.load_definition())
        assert len(set(parsed.headers)) == 4
        assert list(parsed.rows[0].values.values()) == ['a', 'b', 'c', 'd']
        assert parsed.headers == load_files.unique_headers(headings)
    long = 'N' * fields.MAX_HEADER_CHARS
    assert fields.load_definition().clean(['lf:' + h for h in load_files.unique_headers([long, long])]) == (
        'lf:' + long, 'lf:' + long + ' (2)')


def test_email_header_values_are_not_silently_truncated(tmp_path):
    path = tmp_path / 'synthetic.eml'
    value = 'x' * 5000 + 'END'
    # fixture-write: synthetic email input for the production header reader.
    path.write_bytes(('Subject: ' + value + '\r\n\r\nBody').encode())
    assert reader.email_headers(path, ['Subject']) == {'Subject': value}
    # fixture-write: oversized synthetic header must be refused, never shortened.
    path.write_bytes(b'Subject: ' + b'x' * reader.MAX_EMAIL_HEADER_BYTES + b'END')
    with pytest.raises(reader.MetadataUnavailable, match='no partial header'):
        reader.email_headers(path, ['Subject'])


def test_natural_sort_handles_long_decimal_runs_without_integer_conversion():
    assert reader.natural_key('S' + '9' * 5000) < reader.natural_key('S1' + '0' * 5000)
    assert reader.natural_key('²')
    assert reader.natural_key('S٢') < reader.natural_key('S10')


@pytest.mark.parametrize('value', ['²', 'x', ',', '٢', '9' * 5000])
def test_numeric_metadata_sort_handles_nondecimal_unicode_as_text(value):
    from dataclasses import replace
    item = reader.Item('t', 'd', 'v', 'document', '', '', False, {'pages': value}, {}, {})
    key = reader.sort_key(replace(fields.load_definition().by_key['pages'], sort='number'), item)
    assert key is not None


@pytest.mark.parametrize('number', [11, 900, 901])
def test_identifier_assignment_rejects_interior_and_unmatched_produced_bates(production, number):
    client, bench, matter = production
    response = client.post(f'/matters/{matter.slug}/production/identifiers',
                           data={'prefix': 'syn', 'start': number, 'digits': 6, 'q': 'memo'}, follow_redirects=False)
    assert response.headers['location'].endswith('notice=conflict')
    assert not list(bench.workspace.connection.execute('SELECT * FROM workbench_discovery_identifier'))


def test_index_build_cannot_publish_after_real_purge_hook(production, monkeypatch, tmp_path):
    from case_intelligence.modules.discovery_review.purge import purge_hook
    client, bench, matter = production
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import uuid
    ready, resume = threading.Event(), threading.Event()
    original = reader.build
    def purge_during_build(*args, **kwargs):
        built = original(*args, **kwargs)
        assert any('memo' in item.value('name') for item in built.items)
        ready.set()
        assert resume.wait(10)
        return built
    monkeypatch.setattr(reader, 'build', purge_during_build)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(reader.index_for, matter.matter_id, bench.source_store(matter), fields.load_definition())
        try:
            assert ready.wait(10)
            purge_hook(tmp_path / matter.matter_id, matter.matter_id)
        finally:
            resume.set()
        with pytest.raises(RuntimeError):
            pending.result(timeout=10)
    assert matter.matter_id not in reader._cache
    monkeypatch.setattr(reader, 'build', original)
    for _ in range(reader.MAX_CACHED_MATTERS + 1):
        reader.index_for('ci-matter-' + uuid.uuid4().hex, bench.source_store(matter), fields.load_definition())
    for _ in range(2):
        purge_hook(tmp_path / matter.matter_id, matter.matter_id)
        with pytest.raises(RuntimeError):
            reader.index_for(matter.matter_id, bench.source_store(matter), fields.load_definition())


@pytest.mark.parametrize('scope,action', [('matter', 'save'), ('matter', 'reset'), ('user', 'save'),
                                         ('user', 'reset'), ('identifiers', 'assign'), ('identifiers', 'clear')])
def test_review_write_rechecks_revoked_membership_inside_transaction(production, monkeypatch, scope, action):
    from case_intelligence.modules.discovery_review.identifiers import IdentifierRepository
    client, bench, _ = production
    workspace = bench.workspace
    owner = workspace.upsert_principal('test', 'synthetic-owner', 'Synthetic owner', 'synthetic-owner').principal_id
    matter = workspace.create_matter('Synthetic revocation', 'Generated', owner)
    workspace.add_member(matter.matter_id, ACTOR, owner)
    add_files(client, bench, matter, {'memo.txt': b'Synthetic content'}, key='b')
    base = f'/matters/{matter.slug}/production'
    client.post(base + '/view', data={'scope': scope if scope != 'identifiers' else 'user', 'columns': ['name']})
    client.post(base + '/identifiers', data={'prefix': 'DEF', 'digits': 4})
    table = 'workbench_discovery_identifier' if scope == 'identifiers' else 'workbench_discovery_view'
    before = [tuple(row) for row in workspace.connection.execute('SELECT * FROM ' + table)]
    repository = IdentifierRepository if scope == 'identifiers' else ViewRepository
    method = action if scope == 'identifiers' else 'clear' if action == 'reset' else 'save'
    original = getattr(repository, method)
    def revoked(self, *args, **kwargs):
        workspace.revoke_member(matter.matter_id, ACTOR, owner)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(repository, method, revoked)
    data = {'scope': scope, 'action': action, 'columns': ['custodian'], 'prefix': 'NEW', 'confirm': '1'}
    if action == 'assign':
        client.post(base + '/identifiers', data={'action': 'clear', 'confirm': '1'})
        before = []
    audits = list(workspace.connection.execute(
        "SELECT action,outcome FROM workbench_audit_event WHERE action LIKE 'discovery_review.%'"))
    response = client.post(base + ('/identifiers' if scope == 'identifiers' else '/view'), data=data)
    assert response.status_code == 404
    assert list(workspace.connection.execute(
        "SELECT action,outcome FROM workbench_audit_event WHERE action LIKE 'discovery_review.%'")) == audits
    assert [tuple(row) for row in workspace.connection.execute('SELECT * FROM ' + table)] == before


def test_identifier_storage_rejects_case_equivalent_insert_and_update(production):
    import sqlite3
    from tests.fixture_write import fixture_write
    _, bench, matter = production
    workspace = bench.workspace
    with fixture_write(workspace) as connection:
        connection.execute('INSERT INTO workbench_discovery_identifier VALUES (?,?,?,?,?)',
                           (matter.matter_id, 'one', 'DEF001', ACTOR, 'synthetic'))
    with pytest.raises(sqlite3.IntegrityError):
        with fixture_write(workspace) as connection:
            connection.execute('INSERT INTO workbench_discovery_identifier VALUES (?,?,?,?,?)',
                               (matter.matter_id, 'two', 'def001', ACTOR, 'synthetic'))
    with fixture_write(workspace) as connection:
        connection.execute('INSERT INTO workbench_discovery_identifier VALUES (?,?,?,?,?)',
                           (matter.matter_id, 'two', 'DEF002', ACTOR, 'synthetic'))
    for value in ('def001', 'DÉF003', 'X', 'A' * 33, 'A' * 21 + '1'):
        with pytest.raises(sqlite3.IntegrityError):
            with fixture_write(workspace) as connection:
                connection.execute("UPDATE workbench_discovery_identifier SET identifier=? WHERE document_id='two'",
                                   (value,))



def test_fields_view_renders_complete_accepted_load_file_values(production):
    client, bench, matter = production
    value = 'Z' * 5000 + 'END'
    add_files(client, bench, matter, {'long.txt': b'Synthetic text', 'long.csv':
        ('PATH,BEGBATES,Note\nlong.txt,' + value + ',' + value + '\n').encode()}, key='c')
    html = client.get(f'/matters/{matter.slug}/production/items/{token_for(bench, matter, "long.txt")}?view=fields').text
    assert unescape(re.sub('<[^>]+>', '', html)).count(value) >= 2


def test_load_file_parser_limit_reports_rejected_row_without_partial_values():
    import csv
    parsed = load_files.parse(b'PATH,Note\na,' + b'x' * (csv.field_size_limit() + 1),
                              document_id='synthetic', name='load.csv', definition=fields.load_definition())
    assert parsed.problem and not parsed.rows


def test_bates_range_collision_check_preserves_original_strings(production):
    from case_intelligence.modules.discovery_review.identifiers import produced_collisions, RangeUnavailable
    from dataclasses import replace
    _, bench, matter = production
    definition = fields.load_definition()
    index = reader.index_for(matter.matter_id, bench.source_store(matter), definition)
    before = [dict(item.values) for item in index.items]
    assert produced_collisions(index, definition, ['sYn11'])
    assert not produced_collisions(index, definition, ['SYN13'])
    huge = '0' * 5000
    item = replace(index.items[0], values=dict(zip(definition.heading_range, ['Big' + huge + '1', 'bIG' + huge + '3'])))
    changed = replace(index, items=(item,), reads={})
    assert produced_collisions(changed, definition, ['BIG2'])
    invalid = replace(item, values=dict(zip(definition.heading_range, ['SYN010', 'Other012'])))
    with pytest.raises(RangeUnavailable):
        produced_collisions(replace(changed, items=(invalid,)), definition, ['SAFE001'])
    assert before == [dict(item.values) for item in index.items]


def test_source_revision_change_invalidates_identifier_collision_check(production, monkeypatch):
    from case_intelligence.modules.discovery_review import routes
    client, bench, matter = production
    original = routes.produced_collisions
    def changed(*args):
        result = original(*args)
        bench.source_store(matter).revision += 1
        return result
    monkeypatch.setattr(routes, 'produced_collisions', changed)
    response = client.post(f'/matters/{matter.slug}/production/identifiers', data={'prefix': 'DEF'}, follow_redirects=False)
    assert response.headers['location'].endswith('notice=retry')
    assert not list(bench.workspace.connection.execute('SELECT * FROM workbench_discovery_identifier'))


def test_server_pagination_links_preserve_explicit_view(production, monkeypatch):
    from dataclasses import replace
    client, _, matter = production
    definition = replace(fields.load_definition(), page_size=2)
    monkeypatch.setattr(fields, 'load_definition', lambda: definition)
    html = client.get(f'/matters/{matter.slug}/production?view=fields').text
    link = unescape(re.search(r'href="([^"]+)"[^>]*data-review-page="next"', html).group(1))
    assert 'view=fields' in link
    assert all('view=fields' in unescape(url) for url in re.findall(r'data-url="([^"]+)"', html))


def test_identifier_batch_failure_rolls_back_every_insert(production):
    from case_intelligence.modules.discovery_review.identifiers import IdentifierRepository, IdentifierConflict
    _, bench, matter = production
    repository = IdentifierRepository(ModuleDatabase(bench.workspace.connection, bench.workspace._lock), bench.workspace.membership)
    with pytest.raises(IdentifierConflict):
        repository.assign(matter.matter_id, ACTOR, [('a', 'DEF001'), ('b', 'def001')])
    assert repository.read(matter.matter_id) == {} and repository.mark(matter.matter_id) == 0
    other = bench.workspace.create_matter('Synthetic other numbering', 'Generated', ACTOR)
    for identity in (matter.matter_id, other.matter_id):
        assert repository.assign(identity, ACTOR, [('synthetic', 'Def001')]) == 1


@pytest.mark.parametrize("legacy", ["def001", "DÉF002", "bad"])
def test_identifier_migration_preserves_spelling_and_rejects_existing_collisions(tmp_path, legacy):
    import sqlite3
    from tests.fixture_write import fixture_write
    from case_intelligence.workspace_store import WorkspaceStore
    path = tmp_path / 'synthetic.sqlite'
    workspace = WorkspaceStore(path)
    actor = workspace.upsert_principal('test', 'synthetic', 'Synthetic', 'synthetic').principal_id
    matter = workspace.create_matter('Synthetic migration', 'Generated', actor)
    with fixture_write(workspace) as connection:
        connection.execute('DROP INDEX discovery_identifier_nocase')
        connection.execute('DROP TRIGGER discovery_identifier_domain_insert')
        connection.execute('DROP TRIGGER discovery_identifier_domain_update')
        connection.executemany('INSERT INTO workbench_discovery_identifier VALUES (?,?,?,?,?)',
                              [(matter.matter_id, 'a', 'Def001', actor, 'synthetic'),
                               (matter.matter_id, 'b', legacy, actor, 'synthetic')])
    sql = (ROOT / 'migrations/sqlite/0047_discovery_identifier_case_uniqueness.sql').read_text()
    with pytest.raises(sqlite3.IntegrityError):
        # fixture-write: real migration transaction rollback on synthetic legacy rows.
        workspace.connection.executescript('BEGIN IMMEDIATE;\n' + sql + '\nCOMMIT;')
    assert not workspace.connection.in_transaction
    assert not list(workspace.connection.execute("SELECT name FROM sqlite_master WHERE name='discovery_identifier_domain_update'"))
    with pytest.raises(RuntimeError, match='Team ID upgrade refused'):
        WorkspaceStore(path)
    with fixture_write(workspace) as connection:
        connection.execute("UPDATE workbench_discovery_identifier SET identifier='def002' WHERE document_id='b'")
    workspace.close()
    for _ in range(2):
        workspace = WorkspaceStore(path)
        assert [r[0] for r in workspace.connection.execute('SELECT identifier FROM workbench_discovery_identifier ORDER BY document_id')] == ['Def001', 'def002']
        workspace.close()


def test_concurrent_case_equivalent_identifier_batches_have_one_winner(production):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from case_intelligence.workspace_store import WorkspaceStore
    from case_intelligence.modules.discovery_review.identifiers import IdentifierRepository, IdentifierConflict
    _, bench, matter = production
    stores = [WorkspaceStore(bench.workspace.path) for _ in range(2)]
    barrier = threading.Barrier(2)
    def assign(position):
        workspace = stores[position]
        repository = IdentifierRepository(ModuleDatabase(workspace.connection, workspace._lock), workspace.membership)
        assert repository.read(matter.matter_id) == {}
        barrier.wait(timeout=10)
        try:
            repository.assign(matter.matter_id, ACTOR, [(str(position), ['Def001', 'def001'][position])])
            return 'won'
        except IdentifierConflict:
            return 'conflict'
    try:
        with ThreadPoolExecutor(2) as pool:
            assert sorted(pool.map(assign, range(2))) == ['conflict', 'won']
    finally:
        for workspace in stores:
            workspace.close()


def test_review_write_serializes_with_membership_revocation(production):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from case_intelligence.workspace_store import WorkspaceStore
    _, bench, _ = production
    workspace = bench.workspace
    owner = workspace.upsert_principal('test', 'synthetic-owner', 'Synthetic owner', 'synthetic-owner').principal_id
    matter = workspace.create_matter('Synthetic transaction', 'Generated', owner)
    workspace.add_member(matter.matter_id, ACTOR, owner)
    other = WorkspaceStore(workspace.path)
    checked, release, started = threading.Event(), threading.Event(), threading.Event()
    def authority(matter_id, actor):
        assert workspace.connection.in_transaction
        workspace.membership(matter_id, actor)
        checked.set()
        assert release.wait(10)
    repository = ViewRepository(ModuleDatabase(workspace.connection, workspace._lock), authority)
    def revoke():
        started.set()
        other.revoke_member(matter.matter_id, ACTOR, owner)
    try:
        with ThreadPoolExecutor(2) as pool:
            saved = pool.submit(repository.save, matter.matter_id, 'matter', ACTOR, ['name'], [])
            assert checked.wait(10)
            revoked = pool.submit(revoke)
            assert started.wait(10) and not revoked.done()
            release.set()
            saved.result(timeout=10)
            revoked.result(timeout=10)
        assert repository.read(matter.matter_id, ACTOR)['matter'].columns == ('name',)
        with pytest.raises(KeyError):
            repository.clear(matter.matter_id, 'matter', ACTOR)
    finally:
        release.set()
        other.close()


def test_identifier_migration_replays_for_frozen_matter_and_clean_backup(production, tmp_path):
    import sqlite3
    from case_intelligence.workspace_store import WorkspaceStore
    from case_intelligence.modules.discovery_review.identifiers import IdentifierRepository
    _, bench, matter = production
    workspace = bench.workspace
    repository = IdentifierRepository(ModuleDatabase(workspace.connection, workspace._lock), workspace.membership)
    repository.assign(matter.matter_id, ACTOR, [('synthetic', 'Def001')])
    claim_bench_exit(bench, matter)
    backup_path = tmp_path / 'synthetic-backup.sqlite'
    # fixture-write: SQLite backup of a synthetic workspace for an isolated clean restore drill.
    with workspace._lock, sqlite3.connect(backup_path) as backup:
        workspace.connection.backup(backup)
    restored = WorkspaceStore(backup_path)
    try:
        assert list(restored.connection.execute('PRAGMA integrity_check'))[0][0] == 'ok'
        assert [row[0] for row in restored.connection.execute('SELECT identifier FROM workbench_discovery_identifier')] == ['Def001']
    finally:
        restored.close()


def test_success_audit_precedes_response_time_revocation(production, monkeypatch):
    client, bench, _ = production
    workspace = bench.workspace
    owner = workspace.upsert_principal('test', 'synthetic-owner', 'Synthetic owner', 'synthetic-owner').principal_id
    matter = workspace.create_matter('Synthetic audit ordering', 'Generated', owner)
    workspace.add_member(matter.matter_id, ACTOR, owner)
    original = ViewRepository.save
    def revoke_after_commit(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        workspace.revoke_member(matter.matter_id, ACTOR, owner)
        return result
    monkeypatch.setattr(ViewRepository, 'save', revoke_after_commit)
    assert client.post(f'/matters/{matter.slug}/production/view', data={'columns': ['name']}).status_code == 404
    assert list(workspace.connection.execute(
        "SELECT 1 FROM workbench_audit_event WHERE matter_id=? AND action='discovery_review.view_save' AND outcome='success'",
        (matter.matter_id,)))


def test_duplicate_maximum_length_header_remains_selectable_and_sortable(production):
    client, bench, matter = production
    header = 'N' * fields.MAX_HEADER_CHARS
    add_files(client, bench, matter, {'duplicate.txt': b'Synthetic text', 'duplicate.csv':
        ('PATH,' + header + ',' + header + '\nduplicate.txt,one,two\n').encode()}, key='d')
    base = f'/matters/{matter.slug}/production'
    key = 'lf:' + header + ' (2)'
    assert client.post(base + '/view', data={'columns': [key, 'name']}).status_code == 200
    page = client.get(base, params={'sort': key})
    assert page.status_code == 200 and ['two', 'duplicate.txt'] in rows(page.text)


def test_unicode_bates_metadata_does_not_break_default_list_sort(production):
    client, bench, matter = production
    add_files(client, bench, matter, {'unicode.txt': b'Synthetic text', 'unicode.csv':
        'PATH,BEGBATES\nunicode.txt,²\n'.encode()}, key='e')
    page = client.get(f'/matters/{matter.slug}/production')
    assert page.status_code == 200 and '²' in page.text
