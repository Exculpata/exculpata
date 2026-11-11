"""Synthetic review-code storage, HTTP, preservation and purge regressions."""
import io
import json
import re
import sqlite3
from html import unescape
from urllib.parse import parse_qs, urlsplit
from pathlib import Path

import pytest

from case_intelligence.modules import ModuleDatabase
from case_intelligence.modules.discovery_review import fields
from tests.case_lifecycle_web_helpers import claim_bench_exit
from tests.fixture_write import fixture_write
from tests.test_discovery_review_list import production as production, token_for, headers, rows
from tests.test_intake_receipt_http import ACTOR
from tests.test_matter_archive import archive_case as archive_case, close, records, OWNER
from case_intelligence.matter_archive import build_matter_archive, cleanup_archive
from case_intelligence.matter_archive_verifier import verify_matter_archive

ROOT = Path(__file__).resolve().parents[1]


def repository(workspace):
    from case_intelligence.modules.discovery_review.codes import CodeRepository
    return CodeRepository(ModuleDatabase(workspace.connection, workspace._lock))


def history(workspace, matter_id):
    return [tuple(row) for row in workspace.connection.execute(
        "SELECT * FROM workbench_discovery_code_event WHERE matter_id=? ORDER BY event_id", (matter_id,))]


def test_define_order_retire_and_label_rules(archive_case):
    bench, matter, _ = archive_case
    repo = repository(bench.workspace)
    first = repo.define(matter.matter_id, OWNER, "  Relevant  ")
    second = repo.define(matter.matter_id, OWNER, "Follow up")
    assert [(c.label, c.position) for c in repo.list(matter.matter_id)] == [("Relevant", 1), ("Follow up", 2)]
    for label in ("", "  ", "x" * 81, "Relevant"):
        with pytest.raises(ValueError):
            repo.define(matter.matter_id, OWNER, label)
    repo.retire(matter.matter_id, first.code_id)
    assert [c.code_id for c in repo.list(matter.matter_id)] == [second.code_id]
    third = repo.define(matter.matter_id, OWNER, "Check source")
    assert third.position == 3
    assert len(repo.list(matter.matter_id, include_retired=True)) == 3
    with pytest.raises(ValueError):
        repo.define(matter.matter_id, OWNER, "Relevant")


def test_events_are_append_only_latest_wins_and_noops_store_nothing(archive_case):
    bench, matter, _ = archive_case
    repo = repository(bench.workspace)
    code = repo.define(matter.matter_id, OWNER, "Relevant")
    assert not repo.record(matter.matter_id, "synthetic-document", code.code_id, OWNER, "remove")
    assert repo.record(matter.matter_id, "synthetic-document", code.code_id, OWNER, "apply")
    first = history(bench.workspace, matter.matter_id)
    assert first[0][-2] == OWNER and first[0][-1]
    assert not repo.record(matter.matter_id, "synthetic-document", code.code_id, OWNER, "apply")
    assert history(bench.workspace, matter.matter_id) == first
    assert repo.record(matter.matter_id, "synthetic-document", code.code_id, OWNER, "remove")
    assert repo.applied(matter.matter_id) == {}
    assert repo.record(matter.matter_id, "synthetic-document", code.code_id, OWNER, "apply")
    assert repo.applied(matter.matter_id) == {"synthetic-document": (code.code_id,)}
    assert history(bench.workspace, matter.matter_id)[:1] == first
    assert len(history(bench.workspace, matter.matter_id)) == 3
    for statement in ("UPDATE workbench_discovery_code_event SET action='remove' WHERE matter_id=?",
                      "DELETE FROM workbench_discovery_code_event WHERE matter_id=?",
                      "DELETE FROM workbench_discovery_code WHERE matter_id=?"):
        with pytest.raises(sqlite3.DatabaseError), fixture_write(bench.workspace) as connection:
            connection.execute(statement, (matter.matter_id,))
    repo.retire(matter.matter_id, code.code_id)
    assert len(history(bench.workspace, matter.matter_id)) == 3
    with pytest.raises(KeyError):
        repo.record(matter.matter_id, "synthetic-document", code.code_id, OWNER, "remove")


def test_forms_keyboard_mapping_and_json_field(production, monkeypatch):
    client, bench, matter = production
    repo = repository(bench.workspace)
    base = f"/matters/{matter.slug}/production"
    standard = headers(client.get(base).text)
    for number in range(11):
        data = {"label": f"Label {number}", "digit": str(number + 1) if number < 9 else ""}
        assert client.post(base + "/codes", data=data, follow_redirects=False).status_code == 303
    assert 'name="digit"' in client.get(base).text
    assert client.post(base + "/codes", data={"label": "Duplicate digit", "digit": "2"}).status_code == 400
    for invalid in ("0", "10", "1.5", "x"):
        assert client.post(base + "/codes", data={"label": "Invalid digit", "digit": invalid}).status_code == 422
    codes = repo.list(matter.matter_id)
    assert client.post(base + f"/codes/{codes[0].code_id}/retire", follow_redirects=False).status_code == 303
    token = token_for(bench, matter, "memo.txt")
    item_url = f"{base}/items/{token}"
    page = client.get(item_url).text
    assert re.findall(r'data-code-key="(\d)"', page) == list("23456789")
    assert re.findall(r'data-code-id="([^"]+)"', page) == [c.code_id for c in codes[1:]]
    assert 'name="csrf_token"' in page and 'method="post"' in page
    endpoint = item_url + f"/codes/{codes[1].code_id}"
    assert client.post(endpoint, data={"action": "apply"}, follow_redirects=False).status_code == 303
    assert 'aria-pressed="true"' in client.get(item_url).text
    assert headers(client.get(base).text) == [*standard, "Coding"]
    definition = fields.load_definition()
    chosen = next(f for f in definition.fields if f.label == "Coding")
    assert definition.by_key[chosen.key].group in {g.key for g in definition.groups if g.label == "Coding"}
    assert chosen.key in definition.default_columns and chosen.key in definition.default_item_fields
    assert client.post(base + "/view", data={"columns": ["name"]}).status_code == 200
    assert client.post(base + "/view", data={"action": "reset"}).status_code == 200
    assert headers(client.get(base).text) == [*standard, "Coding"]
    assert client.post(base + "/view", data={"columns": ["name", chosen.key]}).status_code == 200
    assert ["memo.txt", "Label 1"] in rows(client.get(base).text)
    assert client.post(endpoint, data={"action": "remove"}).status_code == 200
    assert ["memo.txt", ""] in rows(client.get(base).text)
    assert client.post(endpoint, data={"action": "toggle"}).status_code == 400
    for path in ("modules/discovery_review/routes.py", "templates/workbench_production_documents.html",
                 "templates/workbench_production_document_item.html", "static/discovery-review.js"):
        source = (ROOT / "src/case_intelligence" / path).read_text()
        assert '"' + chosen.key + '"' not in source and "'" + chosen.key + "'" not in source
        assert "Coding" not in source
    data = json.loads((ROOT / "src/case_intelligence/config/discovery_review_fields.json").read_text())
    raw = next(f for f in data["fields"] if f["key"] == chosen.key)
    raw.update(key="synthetic_labels", label="Review labels")
    data["default_columns"] = ["name", "synthetic_labels"]
    data["default_item_fields"] = ["synthetic_labels"]
    data["hide_empty_default_columns"] = False
    monkeypatch.setattr(fields, "load_definition", lambda: fields.parse_definition(data))
    client.post(base + "/view", data={"action": "reset"})
    client.post(endpoint, data={"action": "apply"})
    assert headers(client.get(base).text) == ["Name", "Review labels"]
    assert ["memo.txt", "Label 1"] in rows(client.get(base).text)


def test_matter_membership_and_foreign_item_or_code_refused(production):
    client, bench, matter = production
    repo = repository(bench.workspace)
    principal = bench.workspace.upsert_principal("test", "other-reviewer", "Synthetic reviewer", "synthetic")
    private = bench.workspace.create_matter("Synthetic private", "Synthetic", principal.principal_id)
    foreign_code = repo.define(private.matter_id, principal.principal_id, "Private label", digit=3)
    code = repo.define(matter.matter_id, ACTOR, "Shared label", digit=3)
    token = token_for(bench, matter, "memo.txt")
    base = f"/matters/{matter.slug}/production"
    private_base = f"/matters/{private.slug}/production"
    for endpoint, data in ((private_base + "/codes", {"label": "Denied"}),
                           (private_base + f"/codes/{foreign_code.code_id}/retire", {}),
                           (private_base + f"/items/{token}/codes/{foreign_code.code_id}", {"action": "apply"}),
                           (base + f"/items/{token}/codes/{foreign_code.code_id}", {"action": "apply"}),
                           (base + f"/codes/{foreign_code.code_id}/retire", {}),
                           (base + f"/items/{'0' * 32}/codes/{code.code_id}", {"action": "apply"})):
        assert client.post(endpoint, data=data).status_code == 404
    assert history(bench.workspace, matter.matter_id) == []
    assert history(bench.workspace, private.matter_id) == []
    assert client.post(base + f"/items/{token}/codes/{code.code_id}", data={"action": "apply"}).status_code == 200
    repo.record(private.matter_id, "synthetic-neighbor", foreign_code.code_id, principal.principal_id, "apply")
    foreign_history = history(bench.workspace, private.matter_id)
    prepared, lifecycle = claim_bench_exit(bench, matter)
    assert bench.execute_matter_purge(prepared, lifecycle).state == "deleted"
    assert repo.list(matter.matter_id, include_retired=True) == []
    assert history(bench.workspace, matter.matter_id) == []
    assert repo.list(private.matter_id)[0].label == "Private label"
    assert repo.list(private.matter_id)[0].digit == 3
    assert history(bench.workspace, private.matter_id) == foreign_history


def test_archive_keeps_codes_and_events_with_attribution(archive_case):
    bench, matter, store = archive_case
    repo = repository(bench.workspace)
    code = repo.define(matter.matter_id, OWNER, "Synthetic label", digit=2)
    repo.record(matter.matter_id, "synthetic-document", code.code_id, OWNER, "apply")
    repo.retire(matter.matter_id, code.code_id)
    close(bench, matter)
    artifact = build_matter_archive(bench, matter, OWNER, source_store=store)
    try:
        manifest = verify_matter_archive(artifact.path, expected_sha256=artifact.sha256)
        assert manifest["table_rows"]["workbench_discovery_code"] == 1
        assert manifest["table_rows"]["workbench_discovery_code_event"] == 1
        stored = records(artifact, "workbench_discovery_code")[0]
        event = records(artifact, "workbench_discovery_code_event")[0]
        assert stored["label"] == "Synthetic label" and stored["active"] == 0
        assert stored["digit"] == 2
        assert event["action"] == "apply" and event["code_id"] == stored["code_id"]
        assert event["created_by"].startswith("actor-") and event["created_at"]
    finally:
        cleanup_archive(artifact)


def test_code_migration_mirror():
    name = "migrations/sqlite/0047_discovery_review_codes.sql"
    assert (ROOT / name).read_bytes() == (ROOT / "src/case_intelligence" / name).read_bytes()


def test_real_session_csrf_and_non_owner_member_can_code(tmp_path):
    from fastapi.testclient import TestClient
    from case_intelligence.generation import UnavailableGenerator
    from case_intelligence.identity import LOGIN_CHALLENGE_COOKIE
    from case_intelligence.workbench import create_workbench_app

    app = create_workbench_app(tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="preview")
    with TestClient(app) as client:
        assert client.get("/auth/login").status_code == 200
        assert client.post("/auth/login", data={"identity_subject": "taylor-morgan",
            "login_challenge": client.cookies.get(LOGIN_CHALLENGE_COOKIE), "next": "/"},
            follow_redirects=False).status_code == 303
        csrf = re.search(r'data-csrf-token="([0-9a-f]{64})"', client.get("/").text).group(1)
        bench = app.state.workbench
        owner = bench.workspace.upsert_principal("test", "synthetic-owner", "Synthetic owner", "generated")
        matter = bench.workspace.create_matter("Synthetic shared review", "Synthetic", owner.principal_id)
        bench.workspace.add_member(matter.matter_id, ACTOR, owner.principal_id)
        assert bench.workspace.membership(matter.matter_id, ACTOR).role == "member"
        store = bench.source_store(matter)
        document, _ = store.store_stream("synthetic.txt", "text/plain", io.BytesIO(b"Synthetic source."))
        repo = repository(bench.workspace)
        base = f"/matters/{matter.slug}/production"
        assert client.post(base + "/codes", data={"label": "Check"}).status_code == 403
        assert repo.list(matter.matter_id) == []
        assert client.post(base + "/codes", data={"label": "Check", "csrf_token": csrf}).status_code == 200
        code = repo.list(matter.matter_id)[0]
        item_url = base + f"/items/{store.action_token(document)}/codes/{code.code_id}"
        retire_url = base + f"/codes/{code.code_id}/retire"
        assert client.post(item_url, data={"action": "apply"}).status_code == 403
        assert client.post(retire_url).status_code == 403
        for action in ("apply", "remove"):
            assert client.post(item_url, data={"action": action, "csrf_token": csrf}).status_code == 200
        assert all(row[-2] == ACTOR for row in history(bench.workspace, matter.matter_id))
        assert client.post(retire_url, data={"csrf_token": csrf}).status_code == 200
        bench.workspace.revoke_member(matter.matter_id, ACTOR, owner.principal_id)
        assert client.post(base + "/codes", data={"label": "Denied", "csrf_token": csrf}).status_code == 404
        assert len(repo.list(matter.matter_id, include_retired=True)) == 1


def test_frozen_matter_refuses_code_changes(archive_case):
    from case_intelligence.modules.discovery_review.codes import CodeUnavailable
    bench, matter, _ = archive_case
    repo = repository(bench.workspace)
    code = repo.define(matter.matter_id, OWNER, "Synthetic label", digit=2)
    close(bench, matter)
    with pytest.raises(CodeUnavailable):
        repo.define(matter.matter_id, OWNER, "Another label")
    with pytest.raises(CodeUnavailable):
        repo.record(matter.matter_id, "synthetic-document", code.code_id, OWNER, "apply")
    with pytest.raises(CodeUnavailable):
        repo.retire(matter.matter_id, code.code_id)
    assert repo.list(matter.matter_id)[0].active
    assert history(bench.workspace, matter.matter_id) == []


def test_synthetic_backup_restore_preserves_order_and_event_history(tmp_path):
    import shutil
    from case_intelligence.workspace_store import WorkspaceStore

    database, backup = tmp_path / "workspace.sqlite", tmp_path / "restored.sqlite"
    workspace = WorkspaceStore(database)
    try:
        workspace.upsert_principal("test", OWNER, "Synthetic owner", "generated", preferred_principal_id=OWNER)
        matter = workspace.create_matter("Synthetic restore", "Generated", OWNER)
        repo = repository(workspace)
        code = repo.define(matter.matter_id, OWNER, "Retained label", digit=7)
        repo.record(matter.matter_id, "synthetic-document", code.code_id, OWNER, "apply")
        repo.record(matter.matter_id, "synthetic-document", code.code_id, OWNER, "remove")
        repo.retire(matter.matter_id, code.code_id)
        expected = history(workspace, matter.matter_id)
    finally:
        workspace.close()
    shutil.copyfile(database, backup)  # fixture-write: backup of a closed synthetic workspace
    restored = WorkspaceStore(backup)
    try:
        repo = repository(restored)
        assert history(restored, matter.matter_id) == expected
        assert repo.list(matter.matter_id) == []
        assert repo.list(matter.matter_id, include_retired=True)[0].label == "Retained label"
        assert repo.list(matter.matter_id, include_retired=True)[0].digit == 7
        assert repo.define(matter.matter_id, OWNER, "Next label").position == 2
    finally:
        restored.close()


def test_retirement_hides_controls_without_undoing_applied_state(production):
    client, bench, matter = production
    repo = repository(bench.workspace)
    code = repo.define(matter.matter_id, ACTOR, "Retained review label")
    token = token_for(bench, matter, "memo.txt")
    base = f"/matters/{matter.slug}/production"
    item_url = f"{base}/items/{token}"
    assert client.post(item_url + f"/codes/{code.code_id}", data={"action": "apply"}).status_code == 200
    applied, events = repo.applied(matter.matter_id), history(bench.workspace, matter.matter_id)
    assert client.post(base + f"/codes/{code.code_id}/retire").status_code == 200
    assert repo.applied(matter.matter_id) == applied
    assert history(bench.workspace, matter.matter_id) == events
    assert f'data-code-id="{code.code_id}"' not in client.get(item_url).text
    field = next(f.key for f in fields.load_definition().fields if f.computed == "review_codes")
    client.post(base + "/view", data={"columns": ["name", field]})
    assert ["memo.txt", "Retained review label (retired)"] in rows(client.get(base).text)


@pytest.mark.parametrize("fragment", [False, True])
def test_code_posts_keep_validated_review_position(production, fragment):
    from tests.test_discovery_review_list import add_files
    client, bench, matter = production
    add_files(client, bench, matter, {"paged-memo.txt": b"Synthetic review line.\n" * 140}, key="c")
    code = repository(bench.workspace).define(matter.matter_id, ACTOR, "Review")
    token = token_for(bench, matter, "paged-memo.txt")
    base = f"/matters/{matter.slug}/production"
    state = {"q": "memo", "sort": "name", "dir": "desc", "unit": "7", "view": "text"}
    page = client.get(f"{base}/items/{token}" if fragment else base,
                      params={**state, "doc": token, "all_columns": "1"}).text
    assert "(7 of 7)" in page
    form = re.search(r'<form[^>]*data-code-form>(.*?)</form>', page, re.S).group(1)
    submitted = {name: unescape(value) for name, value in re.findall(r'name="([^"]+)" value="([^"]*)"', form)}
    for action in ("apply", "remove"):
        result = client.post(f"{base}/items/{token}/codes/{code.code_id}",
                             data={**submitted, "action": action}, follow_redirects=False)
        assert result.status_code == 303
        target = urlsplit(result.headers["location"])
        assert not target.netloc and target.path == base
        assert parse_qs(target.query) == {key: [value] for key, value in
                                         {**state, "doc": token, "all_columns": "1"}.items()}
    for bad in ({"view": "https://example.test"}, {"dir": "sideways"}, {"unit": "0"},
                {"unit": "1000001"}, {"q": "x" * 201}, {"sort": "x" * 204}):
        assert client.post(f"{base}/items/{token}/codes/{code.code_id}",
                           data={**state, **bad, "action": "apply"}).status_code == 422
    assert len(history(bench.workspace, matter.matter_id)) == 2
    unknown = client.post(f"{base}/items/{token}/codes/{code.code_id}",
                         data={**state, "sort": "https://example.test", "action": "apply"},
                         follow_redirects=False)
    assert "sort" not in parse_qs(urlsplit(unknown.headers["location"]).query)


def test_optional_stored_digits_survive_retirement(production):
    client, bench, matter = production
    repo = repository(bench.workspace)
    codes = [repo.define(matter.matter_id, ACTOR, f"Code {digit}", digit=digit) for digit in (1, 2, 3)]
    assert repo.define(matter.matter_id, ACTOR, "No key").digit is None
    for digit in (0, 10, True, "2"):
        with pytest.raises(ValueError):
            repo.define(matter.matter_id, ACTOR, "Invalid", digit=digit)
    with pytest.raises(ValueError):
        repo.define(matter.matter_id, ACTOR, "Duplicate key", digit=2)
    repo.retire(matter.matter_id, codes[0].code_id)
    token = token_for(bench, matter, "memo.txt")
    page = client.get(f"/matters/{matter.slug}/production/items/{token}").text
    assert re.findall(r'data-code-key="(\d)"', page) == ["2", "3"]
    assert [c.digit for c in repo.list(matter.matter_id)] == [2, 3, None]
    assert repo.define(matter.matter_id, ACTOR, "Reuse retired key", digit=1).digit == 1


def test_projection_preserves_apply_attribution_after_other_member_retires(production):
    from case_intelligence.modules.discovery_review import reader
    client, bench, matter = production
    repo = repository(bench.workspace)
    member = bench.workspace.upsert_principal("test", "code-author", "Synthetic member", "generated")
    bench.workspace.add_member(matter.matter_id, member.principal_id, ACTOR)
    code = repo.define(matter.matter_id, ACTOR, "Attributed label")
    token = token_for(bench, matter, "memo.txt")
    base = f"/matters/{matter.slug}/production"
    definition = fields.load_definition()
    key = next(f.key for f in definition.fields if f.computed == "review_codes")
    index = reader.index_for(matter.matter_id, bench.source_store(matter), definition)
    original = index.items[index.by_token[token]]
    empty = repo.project(matter.matter_id, index).items[index.by_token[token]]
    assert empty.value(key) == "" and empty.details[key] == ()
    assert "No value: Coding" in client.get(f"{base}/items/{token}?view=fields").text
    repo.record(matter.matter_id, original.document_id, code.code_id, member.principal_id, "apply")
    event = history(bench.workspace, matter.matter_id)[0]
    for active in (True, False):
        if not active:
            assert client.post(f"{base}/codes/{code.code_id}/retire").status_code == 200
        projected = repo.project(matter.matter_id, index).items[index.by_token[token]]
        detail = projected.details[key][0]
        assert detail["active"] == active
        assert (detail["created_by"], detail["created_at"]) == (member.principal_id, event[-1])
        label = "Attributed label" + ("" if active else " (retired)")
        assert projected.value(key) == label and original.value(key) == ""
        page = client.get(f"{base}/items/{token}?view=fields").text.split("dr-field-more")[0]
        assert label in page and member.principal_id in page and event[-1] in page
        listed = client.get(base).text
        assert label in [row[headers(listed).index("Coding")] for row in rows(listed)]


def test_fragment_script_keeps_review_state_and_localizes_apply_time():
    from tests.test_rename_javascript_consumers import declaration, run_node
    script = "src/case_intelligence/static/discovery-review.js"
    run_node("\n".join((
        "const assert = require('node:assert/strict');",
        declaration("localTimes", script), declaration("show", script),
        r'''
process.env.TZ = 'America/New_York';
let request = 0;
const window = {location: {origin: 'https://synthetic.example.test'}};
let row = {dataset: {url: '/production?q=memo&sort=name&dir=desc&all_columns=1'}};
const current = () => row;
const time = {dateTime: '2025-03-03T14:15:00+00:00', textContent: ''};
const panel = {querySelectorAll: () => [time], querySelector: () => ({dataset: {view: 'text'}})};
const say = message => assert.equal(message, '');
const missingView = {};
let fetched;
const load = async url => { fetched = new URL(url); return 'synthetic fragment'; };
(async () => {
    await show('/production/items/synthetic?view=text&unit=7', '', 'text');
    assert.deepEqual(Object.fromEntries(fetched.searchParams), {
        view: 'text', unit: '7', q: 'memo', sort: 'name', dir: 'desc', all_columns: '1'
    });
    assert.equal(panel.innerHTML, 'synthetic fragment');
    assert.match(time.textContent, /9:15:00/);
    assert.equal(time.dateTime, '2025-03-03T14:15:00+00:00');
    row = {dataset: {url: '/production?q=ledger&sort=type&dir=asc'}};
    await show('/production/items/next?view=fields', '', '');
    assert.equal(fetched.searchParams.get('q'), 'ledger');
    assert.equal(fetched.searchParams.get('dir'), 'asc');
})().catch(error => { console.error(error); process.exitCode = 1; });
''')))
