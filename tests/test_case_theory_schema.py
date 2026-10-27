import io
import os
import subprocess
from hashlib import sha256
from pathlib import Path
import psycopg
import pytest
from case_intelligence.review_bench import ReviewBench
from case_intelligence.review_bench_v2 import apply_postgres_migrations, upsert_postgres_document
from case_intelligence.workbench import CaseIntelligenceWorkbench
from tests.case_theory_db import MIGRATIONS, apply_through, isolated_database, isolated_theory_dsn, postgres_client_command, seed_source, seed_theory

pytestmark = pytest.mark.skipif(
    not os.getenv("CASE_REVIEW_POSTGRES_TEST_DSN"),
    reason="set CASE_REVIEW_POSTGRES_TEST_DSN for the isolated development database",
)

@pytest.fixture
def db(isolated_theory_dsn):
    with psycopg.connect(isolated_theory_dsn, autocommit=True) as connection:
        apply_through(connection, 5)
        seed_source(connection)
        seed_source(connection, "m-b", "doc-2")
        yield connection

def rows(connection, table, matter="m-a"):
    return connection.execute(psycopg.sql.SQL("SELECT * FROM {} WHERE matter_id=%s ORDER BY 1,2,3").format(psycopg.sql.Identifier(table)), (matter,)).fetchall()  # fixture-write: identifier-quoted read of synthetic rows on an independent PostgreSQL connection.

def assert_loss(connection, matter="m-a", status="supported", ordinal=1):
    assert connection.execute("SELECT claim_id FROM review_case_theory_link_audit WHERE matter_id=%s", (matter,)).fetchall() == connection.execute("SELECT claim_id FROM review_case_theory_claim WHERE matter_id=%s", (matter,)).fetchall()
    assert connection.execute("SELECT status,needs_review FROM review_case_theory_claim WHERE matter_id=%s", (matter,)).fetchall() == [(status, True)]
    assert connection.execute("SELECT link_ordinal,document_id,chunk_id,source_version_id,sha256,reason FROM review_case_theory_link_audit WHERE matter_id=%s", (matter,)).fetchall() == [(ordinal, "doc-1" if matter == "m-a" else "doc-2", f"chunk-{ordinal}", "synthetic-v1", sha256(f"S{ordinal}: Yes.".encode()).hexdigest(), "passage_deleted")]

@pytest.mark.parametrize("operation", ["reindex", "delete", "rebuild", "startup"])
def test_cited_passage_delete_never_raises(isolated_theory_dsn, tmp_path, monkeypatch, operation):
    monkeypatch.setenv("CASE_REVIEW_POSTGRES_DSN", isolated_theory_dsn)
    monkeypatch.setenv("CASE_REVIEW_ENABLE_MODELS", "0")
    bench = ReviewBench(tmp_path / "bench")
    try:
        assert bench.postgres_ready
        document, _ = bench.pilot.store_stream("Synthetic.txt", "text/plain", io.BytesIO(b"S1: Yes."), request_used=0)
        bench.pilot.process(document.document_id)
        bench.index_pilot_document(document.document_id)
        assert document.state == "ready"
        matter = "ci-matter-00000000000000000000000000000001" if operation == "startup" else "matter-pilot"
        connection = bench.postgres_connection
        if operation == "startup":
            upsert_postgres_document(connection, matter_id=matter, matter_name="Synthetic", document_id=document.document_id, display_name="Synthetic.txt", media_type="text/plain", units=document.parsed_units())
        seed_theory(connection, matter, document.document_id)
        original = rows(connection, "review_case_theory_link", matter)
        assert len(original) == 1
        if operation == "reindex":
            bench.index_pilot_document(document.document_id)
        elif operation == "delete":
            bench._delete_pilot_postgres(document.document_id)
        elif operation == "rebuild":
            bench._refresh_pilot_retrieval(index_postgres=True)
        with psycopg.connect(isolated_theory_dsn) as other:
            workspace = CaseIntelligenceWorkbench(tmp_path / "workspace", postgres_connection=other, learned_retrieval=True)
            try:
                assert workspace.postgres_ready and workspace.learned_retrieval
            finally:
                workspace.close()
        assert bench.postgres_ready
        if operation in {"reindex", "rebuild"}:
            assert document.state == "ready"
        links = rows(connection, "review_case_theory_link", matter)
        audits = connection.execute("SELECT claim_id,link_ordinal,document_id,chunk_id,source_version_id,sha256,reason FROM review_case_theory_link_audit WHERE matter_id=%s", (matter,)).fetchall()
        if operation == "startup":
            assert links == audits == rows(connection, "review_case_theory_claim", matter) == []
        else:
            status, flagged = connection.execute("SELECT status,needs_review FROM review_case_theory_claim WHERE matter_id=%s", (matter,)).fetchone()
            assert status == "supported"
            if operation in {"reindex", "rebuild"} and links:
                assert links == original and audits == [] and flagged is False
            else:
                assert links == [] and flagged is True
                link = original[0]
                assert audits == [(link[1], link[2], link[5], link[6], link[7], link[8], "passage_deleted")]
    finally:
        bench.close()

def test_every_lost_link_audited_and_flagged(db):
    seed_theory(db, links=2)
    remaining = rows(db, "review_case_theory_link")[1:]
    db.execute("DELETE FROM review_chunk WHERE matter_id='m-a' AND chunk_id='chunk-1'")  # fixture-write: lose one of two synthetic citations, leaving support still attached.
    assert rows(db, "review_case_theory_link") == remaining
    assert_loss(db)

@pytest.mark.parametrize("status", ["supported", "contested", "unsupported"])
def test_needs_review_preserves_prior_status(db, status):
    seed_theory(db, status=status)
    db.execute("DELETE FROM review_chunk WHERE matter_id='m-a'")  # fixture-write: remove the synthetic cited passage to challenge human status preservation.
    assert_loss(db, status=status)

def test_status_rejects_needs_review_value(db):
    seed_theory(db, links=0)
    assert db.execute("SELECT needs_review FROM review_case_theory_claim").fetchall() == [(False,)]
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute("INSERT INTO review_case_theory_claim(matter_id,version,ordinal,claim_id,text,status) VALUES ('m-a',1,2,'00000000-0000-0000-0000-000000000002','Synthetic','needs review')")  # fixture-write: challenge the status CHECK with a synthetic invalid claim.
    with pytest.raises(psycopg.errors.NotNullViolation), db.transaction():
        db.execute("UPDATE review_case_theory_claim SET needs_review=NULL")  # fixture-write: challenge the non-null review flag on the synthetic claim.

def test_db_refuses_cross_matter_passage_and_foreign_assertion(db):
    claim = seed_theory(db, links=0)
    db.execute("INSERT INTO review_case_theory_assertion_owner VALUES ('assertion-b','m-b')")  # fixture-write: give the synthetic assertion exclusively to the other matter.
    with pytest.raises(psycopg.errors.ForeignKeyViolation), db.transaction():
        db.execute("INSERT INTO review_case_theory_link(matter_id,claim_id,ordinal,kind,document_id,chunk_id,source_version_id,sha256) VALUES ('m-a',%s,1,'source_passage','doc-2','chunk-1','synthetic-v1',%s)", (claim, sha256(b"S1: Yes.").hexdigest()))  # fixture-write: try citing a passage that exists only in the other matter.
    with pytest.raises(psycopg.errors.ForeignKeyViolation), db.transaction():
        db.execute("INSERT INTO review_case_theory_link(matter_id,claim_id,ordinal,kind,assertion_id) VALUES ('m-a',%s,1,'assertion','assertion-b')", (claim,))  # fixture-write: bypass application ownership checks with a synthetic foreign assertion.
    assert rows(db, "review_case_theory_link") == []

def test_matter_purge_removes_audit_rows(db):
    for matter, document in (("m-a", "doc-1"), ("m-b", "doc-2")):
        seed_theory(db, matter, document, links=2)
    db.execute("DELETE FROM review_chunk WHERE chunk_id='chunk-1'")  # fixture-write: create loss audits while retaining a live link in each synthetic matter.
    assert_loss(db)
    assert_loss(db, "m-b")
    foreign = rows(db, "review_case_theory_link_audit", "m-b")
    db.execute("DELETE FROM review_matter WHERE matter_id='m-a'")  # fixture-write: purge a matter with both an existing audit and a cascading live citation.
    assert rows(db, "review_case_theory_link_audit") == []
    assert rows(db, "review_case_theory_link") == rows(db, "review_case_theory_claim") == []
    assert rows(db, "review_case_theory_link_audit", "m-b") == foreign

def test_upgrade_from_0005_and_clean_dump_restore(db, isolated_theory_dsn):
    assert db.execute("SELECT max(version) FROM review_schema_migration").fetchone() == (5,)
    sources = rows(db, "review_chunk")
    with db.cursor() as cursor:  # fixture-write: upgrade the populated synthetic version 5 database with packaged migrations.
        assert apply_postgres_migrations(cursor) == tuple(range(1, 7))
        assert apply_postgres_migrations(cursor) == tuple(range(1, 7))
    assert db.execute("SELECT max(version) FROM review_schema_migration").fetchone() == (6,)
    operator = Path(__file__).resolve().parents[1] / "migrations/postgresql"
    assert not list(operator.glob("0007*.sql")) and not any(p.name.startswith("0007") for p in MIGRATIONS.iterdir())
    assert (operator / "0006_case_theory.sql").read_bytes() == MIGRATIONS.joinpath("0006_case_theory.sql").read_bytes()
    assert rows(db, "review_chunk") == sources
    seed_theory(db)
    dump = subprocess.run(postgres_client_command("pg_dump", "--dbname", isolated_theory_dsn, "--no-owner", "--no-privileges"), capture_output=True)
    assert dump.returncode == 0, dump.stderr.decode(errors="replace")
    with isolated_database() as target:
        restore = subprocess.run(postgres_client_command("psql", "--dbname", target, "-X", "-v", "ON_ERROR_STOP=1"), input=dump.stdout, capture_output=True)  # fixture-write: restore the synthetic dump into a genuinely fresh database.
        assert restore.returncode == 0, restore.stderr.decode(errors="replace")
        with psycopg.connect(target, autocommit=True) as restored:
            for table in ("review_chunk", "review_case_theory_version", "review_case_theory_claim", "review_case_theory_link"):
                assert rows(restored, table) == rows(db, table)
            assert set(restored.execute("SELECT confrelid::regclass::text,confdeltype FROM pg_constraint WHERE conrelid='review_case_theory_link'::regclass AND contype='f'").fetchall()) == {("review_case_theory_claim", "c"), ("review_chunk", "c"), ("review_case_theory_assertion_owner", "c")}
            assert restored.execute("SELECT confrelid::regclass::text,confdeltype FROM pg_constraint WHERE conrelid='review_case_theory_link_audit'::regclass AND contype='f'").fetchall() == [("review_matter", "c")]
            assert "'version_rebound'" in restored.execute("SELECT string_agg(pg_get_constraintdef(oid),' ') FROM pg_constraint WHERE conrelid='review_case_theory_link_audit'::regclass AND contype='c'").fetchone()[0]
            restored.execute("DELETE FROM review_chunk WHERE matter_id='m-a' AND chunk_id='chunk-1'")  # fixture-write: exercise the restored passage FK cascade and audit trigger.
            assert rows(restored, "review_case_theory_link") == []
            assert_loss(restored)
