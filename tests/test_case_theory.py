"""Synthetic repository acceptance against PostgreSQL 17 with pgvector."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Event

import psycopg
import pytest

from case_intelligence.case_theory import CaseTheoryError, CaseTheoryRepository, CaseTheoryService
from case_intelligence.review_bench_v2 import delete_postgres_matter
from tests.case_theory_db import apply_through, isolated_theory_dsn
from tests.test_case_theory_reindex import index

pytestmark = pytest.mark.postgres


class ObservedCursor(psycopg.Cursor):
    def execute(self, query, params=None, **kwargs):
        text = str(query)
        self.connection.theory_queries.append(text)
        if self.connection.theory_started and ("FOR UPDATE" in text or "FOR SHARE" in text):
            self.connection.theory_started.set()
        return super().execute(query, params, **kwargs)  # fixture-write: observe SQL on an isolated synthetic PostgreSQL connection without altering it.


@pytest.fixture
def db(isolated_theory_dsn):
    with psycopg.connect(isolated_theory_dsn, autocommit=True, cursor_factory=ObservedCursor) as connection:
        connection.theory_queries, connection.theory_started = [], None
        assert 170000 <= connection.info.server_version < 180000
        apply_through(connection, 6)
        assert connection.execute("SELECT extname FROM pg_extension WHERE extname='vector'").fetchone() == ("vector",)
        for matter, document in (("m-a", "doc-1"), ("m-b", "doc-1"), ("m-b", "doc-2")):
            index(connection, matter=matter, document=document)
        connection.execute("UPDATE review_document SET protective_order='restricted' WHERE matter_id='m-a'")  # fixture-write: seed a synthetic matter-specific restriction.
        yield connection
        assert connection.execute("SELECT bool_and(state='ready') FROM review_processing_state").fetchone() == (True,)


def source_link(number=1, **changes):
    return dict(kind="source_passage", document_id="doc-1", chunk_id=f"chunk-{number}",
                source_version_id="synthetic-v1", sha256=sha256(f"S{number}: Yes.".encode()).hexdigest()) | changes


def claim(status="supported", links=None):
    return dict(text="Synthetic claim", status=status, links=links if links is not None else [source_link()])


def service(connection, **overrides):
    arguments = dict(authorize=lambda matter: matter in {"m-a", "m-b"},
                     assertion_matter=lambda assertion: {"assertion-a": "m-a", "assertion-b": "m-b"}.get(assertion))
    return CaseTheoryService(CaseTheoryRepository(connection), **(arguments | overrides))


def assert_empty(connection):
    for table in ("version", "claim", "link", "link_audit", "assertion_owner"):
        assert connection.execute(psycopg.sql.SQL("SELECT count(*) FROM {} WHERE matter_id='m-a'").format(
            psycopg.sql.Identifier(f"review_case_theory_{table}"))).fetchone() == (0,)  # fixture-write: identifier-quoted read of isolated synthetic rows after rollback.


def test_versioning_isolation_export_and_restrictions(db):
    api = service(db)
    for invalid in ([], [claim(links=[])], [claim("needs review")], [claim() | {"text": " "}],
                    [claim(links=[{"kind": "unknown"}])]):
        with pytest.raises(CaseTheoryError):
            api.create_version("m-a", invalid)
    with pytest.raises(CaseTheoryError, match="not found"):
        api.repository.create("missing", [claim()], "case-team")
    assert_empty(db)
    first = api.create_version("m-a", [claim(status, [source_link(), source_link(2)])
                                     for status in ("supported", "contested", "unsupported")])
    assert any("review_chunk" in query and "FOR SHARE" in query for query in db.theory_queries)
    second = api.create_version("m-a", [claim("contested", [{"kind": "assertion", "assertion_id": "assertion-a"}])])
    foreign = api.create_version("m-b", [claim()])
    assert (first["version"], second["version"], foreign["version"]) == (1, 2, 1)
    assert api.current("m-a") == api.for_report("m-a") == second
    assert api.version("m-a", 1) == first
    for matter, saved, expected in (("m-a", first, [source_link() | {"protective_order": "restricted"}, source_link(2) | {"protective_order": "restricted"}]),
                                   ("m-b", foreign, [source_link()])):
        for read in (saved, api.version(matter, 1), api.export(matter)["versions"][0]):
            assert read["claims"][0]["links"] == expected
            assert read["claims"][0]["needs_review"] is False
        assert api.list_claims(matter, 1) == saved["claims"]
    for operation in (api.current, api.for_report, api.list_claims, api.export):
        with pytest.raises(CaseTheoryError, match="denied"):
            operation("m-c")
    for operation in (lambda: api.version("m-c", 1), lambda: api.create_version("m-c", [claim()])):
        with pytest.raises(CaseTheoryError, match="denied"):
            operation()
    db.execute("DELETE FROM review_chunk WHERE matter_id='m-a' AND chunk_id='chunk-1'")  # fixture-write: lose one of two citations while preserving every synthetic human status.
    changed = api.version("m-a", 1)
    for saved, current in zip(first["claims"], changed["claims"]):
        assert current["status"] == saved["status"] and current["needs_review"] is True
        assert current["links"] == [source_link(2) | {"protective_order": "restricted"}]
        audit, = current["removed_citations"]
        assert (audit["document_id"], audit["chunk_id"], audit["source_version_id"], audit["sha256"], audit["reason"]) == ("doc-1", "chunk-1", "synthetic-v1", source_link()["sha256"], "passage_deleted")
        assert audit["removed_at"]
    assert api.export("m-a")["versions"] == [changed, second]
    delete_postgres_matter(db, matter_id="m-a")
    assert_empty(db)
    with pytest.raises(CaseTheoryError, match="not found"):
        api.current("m-a")
    assert api.export("m-a")["versions"] == []
    assert api.current("m-b") == foreign


def test_citation_write_racing_reindex_never_binds_changed_text(db, isolated_theory_dsn):
    with psycopg.connect(isolated_theory_dsn, autocommit=True, cursor_factory=ObservedCursor) as writer:
        writer.theory_queries, writer.theory_started = [], Event()
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            with db.transaction():
                db.execute("SELECT 1 FROM review_matter WHERE matter_id='m-a' FOR UPDATE")
                pending = pool.submit(service(writer).create_version, "m-a", [claim()])
                assert writer.theory_started.wait(10), "writer never reached a citation lock"
                reads = [query for query in writer.theory_queries if "review_matter" in query or "review_chunk" in query]
                assert "review_matter" in reads[0] and "FOR UPDATE" in reads[0]
                index(db, texts=("S1: Changed.", "S2: Yes."))
            with pytest.raises(CaseTheoryError):
                pending.result(timeout=10)
        finally:
            pool.shutdown(wait=True)
    assert_empty(db)
    assert db.execute("SELECT text FROM review_chunk WHERE matter_id='m-a' AND chunk_id='chunk-1'").fetchone() == ("S1: Changed.",)


@pytest.mark.parametrize("changes", [
    {"sha256": "0" * 64}, {"source_version_id": "synthetic-old"},
    {"chunk_id": "missing"}, {"document_id": "doc-2"},
])
def test_create_rejects_stale_passage_digest(db, changes):
    repository = CaseTheoryRepository(db)
    with pytest.raises(CaseTheoryError):
        repository.create("m-a", [claim(), claim(links=[source_link(**changes)])], "case-team")
    assert_empty(db)
    assert repository.create("m-a", [claim()], "case-team")["version"] == 1


def test_repository_rejects_foreign_assertion_in_transaction(db):
    repository = CaseTheoryRepository(db)
    db.theory_queries.clear()
    calls = []
    def owner(assertion):
        calls.append(assertion)
        assert db.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
        assert any("review_matter" in query and "FOR UPDATE" in query for query in db.theory_queries)
        return {"assertion-a": "m-a", "assertion-b": "m-b"}.get(assertion)
    links = [{"kind": "assertion", "assertion_id": assertion} for assertion in ("assertion-a", "assertion-b")]
    with pytest.raises(CaseTheoryError):
        repository.create("m-a", [claim(links=links)], "case-team", assertion_matter=owner)
    assert calls == ["assertion-a", "assertion-b"]
    assert_empty(db)
    with pytest.raises(CaseTheoryError):
        repository.create("m-a", [claim(links=links[:1])], "case-team")
    assert_empty(db)
    db.theory_queries.clear()
    saved = service(db, assertion_matter=owner).create_version("m-a", [claim(links=links[:1])])
    assert saved["version"] == 1 and saved["claims"][0]["links"] == links[:1]
    assert calls == ["assertion-a", "assertion-b", "assertion-a"]


def test_caps_and_fixed_query_count(db):
    api = service(db)
    for create in (api.create_version, lambda matter, claims: api.repository.create(matter, claims, "case-team")):
        for claims in ([claim()] * 101, [claim(links=[source_link()] * 21)]):
            with pytest.raises(CaseTheoryError):
                create("m-a", claims)
            assert_empty(db)
    small = api.create_version("m-a", [claim()])
    db.theory_queries.clear()
    assert api.export("m-a")["versions"] == [small]
    assert len(db.theory_queries) == 3
    large = api.create_version("m-a", [claim(links=[source_link()] * 20)] * 100)
    assert len(large["claims"]) == 100 and all(len(item["links"]) == 20 for item in large["claims"])
    for version, expected in ((1, small), (2, large), (None, large)):
        db.theory_queries.clear()
        assert api.repository.read("m-a", version) == expected
        assert len(db.theory_queries) == 3
    db.theory_queries.clear()
    assert api.export("m-a")["versions"] == [small, large]
    assert len(db.theory_queries) <= 3 * 2


def test_create_on_non_autocommit_connection(db, isolated_theory_dsn):
    with psycopg.connect(isolated_theory_dsn) as writer:
        saved = service(writer).create_version("m-a", [claim()])
        assert saved["version"] == 1
        assert service(db).current("m-a") == saved
    assert service(db).export("m-a")["versions"] == [saved]
