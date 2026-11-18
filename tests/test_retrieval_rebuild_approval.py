"""Operator preview and approval for bulk embedding requests."""
from contextlib import contextmanager, nullcontext
import json
import sys

import pytest

from case_intelligence import retrieval_endpoints, retrieval_rebuild as rebuilds
from case_intelligence.embedding_index import embedding_index_path, load_embedding_index
from tests.test_retrieval_endpoints import endpoint as endpoint


class RebuildDatabase:
    autocommit = True

    def __init__(self):
        self.rows = [
            ("matter-a", "document-a", "shared-chunk", "Synthetic passage A"),
            ("matter-b", "document-b", "shared-chunk", "Synthetic passage B"),
        ]
        self.queries = []
        self.updates = []
        self.identity_at_commit = None

    @contextmanager
    def transaction(self):
        yield
        self.identity_at_commit = embedding_index_path().exists()

    def cursor(self, **kwargs):
        return nullcontext(RebuildCursor(self))


class RebuildCursor:
    def __init__(self, database):
        self.database = database
        self.result = []

    def execute(self, sql, params=()):
        query = " ".join(sql.lower().split())
        self.database.queries.append(query)
        if query.startswith("select distinct matter_id"):
            self.result = [(matter,) for matter in sorted({row[0] for row in self.database.rows})]
        elif query.startswith("select") and "text" in query:
            self.result = self.database.rows.copy()
        else:
            assert query.startswith("lock table review_chunk")

    def fetchmany(self, size):
        result, self.result = self.result[:size], self.result[size:]
        return result

    def executemany(self, sql, values):
        assert "matter_id=%s AND document_id=%s AND chunk_id=%s" in sql
        self.database.updates.extend(row[1:] for row in values)


def test_unapproved_rebuild_cannot_read_or_send_text(endpoint):
    database = RebuildDatabase()
    before = embedding_index_path().read_bytes()
    with pytest.raises(RuntimeError, match="(?i)approv"):
        rebuilds.rebuild(database)
    assert all("text" not in query for query in database.queries)
    assert endpoint["requests"] == []
    assert embedding_index_path().read_bytes() == before


@pytest.mark.parametrize("change", [
    "missing-matter", "added-matter", "removed-matter", "changed-endpoint",
    "removed-endpoint", "changed-model", "wrong-capability", "duplicate-matter",
])
def test_approval_must_cover_exact_scope(endpoint, monkeypatch, change):
    db = RebuildDatabase()
    plan = rebuilds.rebuild_plan(db)
    assert plan == {"capability": "embedding", "endpoint": "https://models.example.test",
                    "model": "synthetic", "matter_ids": ["matter-a", "matter-b"]}
    if change == "missing-matter":
        plan["matter_ids"].pop()
    elif change == "added-matter":
        db.rows.append(("matter-c", "document-c", "shared-chunk", "Synthetic passage C"))
    elif change == "removed-matter":
        db.rows.pop()
    elif change == "changed-endpoint":
        monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_URL", "https://replacement.example.test")
    elif change == "removed-endpoint":
        monkeypatch.delenv("CASE_INTELLIGENCE_EMBEDDING_URL")
    elif change == "changed-model":
        monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_MODEL", "replacement")
    elif change == "wrong-capability":
        plan["capability"] = "rerank"
    else:
        plan["matter_ids"].append("matter-a")
    before = embedding_index_path().read_bytes()
    with pytest.raises(RuntimeError, match="(?i)approv"):
        rebuilds.rebuild(db, approved_plan=plan)
    assert all("text" not in query for query in db.queries)
    assert endpoint["requests"] == []
    assert embedding_index_path().read_bytes() == before


@pytest.mark.parametrize("fail", [False, True])
def test_approved_rebuild_preserves_compound_identity(endpoint, monkeypatch, fail):
    db = RebuildDatabase()
    path = embedding_index_path()
    legacy = load_embedding_index(path)
    del legacy["projection_format"]
    path.write_text(json.dumps(legacy))  # fixture-write: synthetic pre-migration index identity
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_MODEL", "replacement")
    plan = rebuilds.rebuild_plan(db)
    assert endpoint["requests"] == [] and all("text" not in query for query in db.queries)
    db.queries.clear()
    if fail:
        endpoint.update(status=401, fail_on_post=2)
        with pytest.raises(RuntimeError, match="authentication"):
            rebuilds.rebuild(db, approved_plan=plan)
        assert load_embedding_index(path) == legacy
    else:
        assert rebuilds.rebuild(db, approved_plan=plan) == 2
        assert db.updates == [row[:3] for row in db.rows]
        assert db.identity_at_commit is False
        record = load_embedding_index(path)
        assert record["model"] == "replacement" and record["populated"] is True
        retrieval_endpoints.require_index_identity(require_record=True)
        assert "synthetic-secret" not in json.dumps(record)
    assert "access exclusive" in db.queries[0]
    assert [body["input"] for body in endpoint["bodies"]] == [[row[3]] for row in db.rows]


def test_bundled_worker_can_rebuild_without_external_approval(endpoint, monkeypatch):
    monkeypatch.delenv("CASE_INTELLIGENCE_EMBEDDING_URL")
    monkeypatch.setenv("CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL", "http://retrieval:8787")
    local_inputs = []

    def local_request(url, operation, body, timeout):
        assert url == "http://retrieval:8787" and operation == "embed"
        inputs = json.loads(body)["texts"]
        local_inputs.extend(inputs)
        return {"vectors": [[0.25] * 768 for _ in inputs]}

    monkeypatch.setattr(rebuilds, "_local_model_request", local_request)
    db = RebuildDatabase()
    assert rebuilds.rebuild(db) == 2
    assert local_inputs == [row[3] for row in db.rows]
    assert endpoint["requests"] == []


def _command(monkeypatch, db, *args):
    import psycopg

    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: nullcontext(db))
    monkeypatch.setattr(sys, "argv", ["retrieval_rebuild", *args])
    rebuilds.main()


def test_cli_preview_then_apply(endpoint, monkeypatch, tmp_path, capsys):
    db = RebuildDatabase()
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_URL", "https://MODELS.example.test:443/")
    _command(monkeypatch, db, "--plan")
    preview = capsys.readouterr()
    plan = json.loads(preview.out)
    assert set(plan) == {"capability", "endpoint", "model", "matter_ids"}
    assert plan["endpoint"] == "https://models.example.test"
    assert plan["matter_ids"] == ["matter-a", "matter-b"]
    assert all("text" not in query for query in db.queries)
    assert endpoint["requests"] == []
    file = tmp_path / "approved.json"
    file.write_text(preview.out)  # fixture-write: synthetic reviewed rebuild plan
    _command(monkeypatch, db, "--confirm-stopped", "--approved-plan", str(file))
    assert "2 chunks" in capsys.readouterr().out
    assert [body["input"] for body in endpoint["bodies"]] == [[row[3]] for row in db.rows]


@pytest.mark.parametrize("invalid", ["oversized", "invalid-json", "duplicate-key", "control"])
def test_invalid_plan_file_fails_before_text_read(endpoint, monkeypatch, tmp_path, capsys, invalid):
    db = RebuildDatabase()
    raw = json.dumps(rebuilds.rebuild_plan(db))
    if invalid == "oversized":
        raw += " " * rebuilds.MAX_PLAN_BYTES
    elif invalid == "invalid-json":
        raw = "{synthetic-secret"
    elif invalid == "duplicate-key":
        raw = '{"model":"synthetic-secret",' + raw[1:]
    else:
        raw = raw.replace("matter-a", "synthetic-secret\\u001b")
    file = tmp_path / "approved.json"
    file.write_text(raw)  # fixture-write: synthetic invalid approval file
    with pytest.raises(SystemExit) as exit_info:
        _command(monkeypatch, db, "--confirm-stopped", "--approved-plan", str(file))
    output = capsys.readouterr()
    message = str(exit_info.value) + output.err + output.out
    assert "synthetic-secret" not in message and "\x1b" not in message
    assert all("text" not in query for query in db.queries)
    assert endpoint["requests"] == []
