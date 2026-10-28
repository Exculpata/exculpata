"""Synthetic citation lifecycle checks against PostgreSQL 17 and pgvector."""
from contextlib import nullcontext
from hashlib import sha256
from types import SimpleNamespace

import psycopg
import pytest

from case_intelligence.review_bench_v2 import (
    delete_postgres_document, delete_postgres_matter_documents, upsert_postgres_document,
)
from tests.case_theory_db import apply_through, isolated_theory_dsn, seed_theory
from tests.test_case_theory_schema import rows


def index(connection, texts=("S1: Yes.", "S2: Yes."), *, numbers=None,
          matter="m-a", document="doc-1", version="synthetic-v1"):
    units = [SimpleNamespace(number=number, text=text) for number, text in
             zip(numbers or range(1, len(texts) + 1), texts)]
    upsert_postgres_document(connection, matter_id=matter, matter_name="Synthetic",
        document_id=document, display_name="Synthetic.txt", media_type="text/plain",
        units=units, source_version_id=version, content_digest=f"synthetic-{version}", ensure_schema=False)


@pytest.fixture
def db(isolated_theory_dsn):
    with psycopg.connect(isolated_theory_dsn, autocommit=True) as connection:
        assert 170000 <= int(connection.info.server_version) < 180000
        apply_through(connection, 6)
        assert connection.execute("SELECT extname FROM pg_extension WHERE extname='vector'").fetchone() == ("vector",)
        # fixture-write: reject even a no-op status UPDATE while testing synthetic lifecycle operations.
        connection.execute("""
            CREATE FUNCTION forbid_status_write() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'human status must not be written'; END; $$;
            CREATE TRIGGER forbid_status_write BEFORE UPDATE OF status ON review_case_theory_claim
            FOR EACH STATEMENT EXECUTE FUNCTION forbid_status_write();
        """)
        index(connection, matter="m-b", document="doc-2")
        seed_theory(connection, "m-b", "doc-2", links=2)
        foreign = [rows(connection, table, "m-b") for table in
                   ("review_case_theory_link", "review_case_theory_claim", "review_case_theory_link_audit")]
        yield connection
        assert foreign == [rows(connection, table, "m-b") for table in
                           ("review_case_theory_link", "review_case_theory_claim", "review_case_theory_link_audit")]
        assert connection.execute("SELECT 1").fetchone() == (1,)


def seed(connection, texts=("S1: Yes.", "S2: Yes."), status="supported"):
    index(connection, texts)
    claim = seed_theory(connection, status=status, links=2)
    # fixture-write: match each synthetic link digest to its actual cited text, including duplicate controls.
    connection.execute("""UPDATE review_case_theory_link l
        SET sha256=encode(sha256(convert_to(c.text,'UTF8')),'hex') FROM review_chunk c
        WHERE (l.matter_id,l.document_id,l.chunk_id)=(c.matter_id,c.document_id,c.chunk_id)
        AND l.matter_id='m-a'""")
    # fixture-write: retain a non-default synthetic document restriction across replacement.
    connection.execute("UPDATE review_document SET protective_order='restricted' WHERE matter_id='m-a'")
    return claim


def assert_state(connection, status, flagged):
    assert connection.execute("SELECT status,needs_review FROM review_case_theory_claim WHERE matter_id='m-a' ORDER BY version").fetchall() == [(status, flagged)]
    assert connection.execute("SELECT protective_order FROM review_document WHERE matter_id='m-a'").fetchone() == ("restricted",)
    assert connection.execute("SELECT state FROM review_processing_state WHERE matter_id='m-a'").fetchall() == [("ready",)]
    assert connection.execute("SELECT count(*) FROM review_page WHERE page_id LIKE 'citation-reindex-%%'").fetchone() == (0,)


def assert_audits(connection, original, reason):
    expected = [(link[1], link[2], *link[4:], reason) for link in original]
    assert connection.execute("""SELECT claim_id,link_ordinal,assertion_id,document_id,chunk_id,
        source_version_id,sha256,reason FROM review_case_theory_link_audit
        WHERE matter_id='m-a' ORDER BY claim_id,link_ordinal""").fetchall() == sorted(expected)
    assert connection.execute("SELECT bool_and(removed_at IS NOT NULL) FROM review_case_theory_link_audit WHERE matter_id='m-a'").fetchone() == (True,)


@pytest.mark.parametrize("pages", ["same", "shifted_pages"])
@pytest.mark.parametrize("texts", [("S1: Yes.", "S2: Yes."), ("S1: Yes.", "S1: Yes.")])
@pytest.mark.postgres
def test_reindex_unchanged_keeps_citations(db, pages, texts):
    seed(db, texts, status="contested")
    original = rows(db, "review_case_theory_link")
    for _ in range(2):
        index(db, texts, numbers=[4, 7] if pages == "shifted_pages" else None)
        assert rows(db, "review_case_theory_link") == original
        assert rows(db, "review_case_theory_link_audit") == []
        assert_state(db, "contested", False)


@pytest.mark.parametrize(("scenario", "old", "new"), [
    ("elsewhere", ("S1: Yes.", "S2: Yes."), ("S2: Yes.", "S1: Yes.")),
    ("period_shift", ("S1: Yes.", "S1: Yes.", "S2: Yes."),
                     ("S1: Yes.", "S1: Yes.", "S1: Yes.", "S2: Yes.")),
    ("transcript_correction", ("S1: Yes.", "S2: Yes."), ("S1: Yes.", "S1: Yes.")),
])
@pytest.mark.postgres
def test_reindex_never_repoints_identical_text(db, scenario, old, new):
    seed(db, old, status="unsupported")
    original = rows(db, "review_case_theory_link")
    index(db, new)
    assert rows(db, "review_case_theory_link") == []
    assert_audits(db, original, "passage_deleted")
    assert_state(db, "unsupported", True)


@pytest.mark.parametrize(("change", "version"), [
    (change, version) for change in ("uncited_text", "removed", "ordinal", "duplicate_ordinal")
    for version in ("synthetic-v1", "synthetic-v2")
] + [("chunk_id", "synthetic-v1")])
@pytest.mark.postgres
def test_reindex_changed_document_drops_audits_flags(db, change, version):
    claim = seed(db, ("S1: Yes.", "S2: Yes.", "S1: Yes."))
    # fixture-write: retain a historical claim and a second version citing the same synthetic evidence.
    db.execute("INSERT INTO review_case_theory_version(matter_id,version,created_by) VALUES ('m-a',2,'case-team')")
    # fixture-write: copy a synthetic historical claim without changing its human status.
    db.execute("INSERT INTO review_case_theory_claim SELECT matter_id,2,ordinal,'00000000-0000-0000-0000-000000000002',text,status,needs_review FROM review_case_theory_claim WHERE claim_id=%s", (claim,))
    # fixture-write: copy the synthetic links into the current version.
    db.execute("INSERT INTO review_case_theory_link SELECT matter_id,'00000000-0000-0000-0000-000000000002',ordinal,kind,assertion_id,document_id,chunk_id,source_version_id,sha256 FROM review_case_theory_link WHERE claim_id=%s", (claim,))
    if change == "chunk_id":
        # fixture-write: change an uncited passage identity while leaving its actual bytes unchanged.
        db.execute("UPDATE review_chunk SET chunk_id='uncited' WHERE matter_id='m-a' AND chunk_id='chunk-3'")
    elif change in {"ordinal", "duplicate_ordinal"}:
        # fixture-write: challenge the complete ordered-list comparison with an uncited ordinal change.
        db.execute("UPDATE review_chunk SET ordinal=%s WHERE matter_id='m-a' AND chunk_id='chunk-3'", (0 if change == "duplicate_ordinal" else 7,))
    original = rows(db, "review_case_theory_link")
    new = ("S1: Yes.", "S2: Yes.", "S2: Yes.") if change == "uncited_text" else ("S1: Yes.", "S2: Yes.", "S1: Yes.")
    if change == "removed":
        new = new[:2]
    index(db, new, version=version)
    assert rows(db, "review_case_theory_link") == []
    assert_audits(db, original, "passage_deleted")
    assert db.execute("SELECT status,needs_review FROM review_case_theory_claim WHERE matter_id='m-a' ORDER BY version").fetchall() == [("supported", True)] * 2
    assert db.execute("SELECT protective_order FROM review_document WHERE matter_id='m-a'").fetchone() == ("restricted",)


@pytest.mark.parametrize("status", ["supported", "contested", "unsupported"])
@pytest.mark.parametrize("texts", [("S1: Yes.", "S2: Yes."), ("S1: Yes.", "S1: Yes.")])
@pytest.mark.parametrize("renamed", [False, True])
@pytest.mark.postgres
def test_identical_text_new_version_keeps_citations_rebound_with_audit(db, status, texts, renamed):
    seed(db, texts, status)
    if renamed:
        # fixture-write: copy synthetic chunks under different IDs and temporary per-page ordinals.
        db.execute("INSERT INTO review_chunk(matter_id,document_id,page_id,chunk_id,ordinal,text,source_version_id) SELECT matter_id,document_id,page_id,'old-' || chunk_id,ordinal+10,text,source_version_id FROM review_chunk WHERE matter_id='m-a'")
        # fixture-write: attach the synthetic links to the alternate initial IDs before re-indexing.
        db.execute("UPDATE review_case_theory_link SET chunk_id='old-' || chunk_id WHERE matter_id='m-a'")
        # fixture-write: remove the now-uncited copies without creating deletion audits.
        db.execute("DELETE FROM review_chunk WHERE matter_id='m-a' AND chunk_id NOT LIKE 'old-%%'")
        # fixture-write: give the synthetic alternate IDs their original ordered positions.
        db.execute("UPDATE review_chunk SET ordinal=ordinal-10 WHERE matter_id='m-a'")
    original = rows(db, "review_case_theory_link")
    index(db, texts, version="synthetic-v2")
    expected = [(*link[:6], f"chunk-{link[2]}", "synthetic-v2", link[8]) for link in original]
    assert rows(db, "review_case_theory_link") == expected
    assert db.execute("""SELECT c.ordinal,l.source_version_id,c.source_version_id,l.sha256,
        encode(sha256(convert_to(c.text,'UTF8')),'hex') FROM review_case_theory_link l
        JOIN review_chunk c USING(matter_id,document_id,chunk_id)
        WHERE l.matter_id='m-a' ORDER BY l.ordinal""").fetchall() == [
            (i, "synthetic-v2", "synthetic-v2", sha256(text.encode()).hexdigest(), sha256(text.encode()).hexdigest())
            for i, text in enumerate(texts)]
    assert_audits(db, original, "version_rebound")
    assert_state(db, status, False)
    index(db, texts, version="synthetic-v2")
    assert rows(db, "review_case_theory_link") == expected
    assert_audits(db, original, "version_rebound")
    assert_state(db, status, False)


@pytest.mark.postgres
def test_failed_reindex_rolls_back_staged_citations(db):
    seed(db)
    original = rows(db, "review_case_theory_link")
    with pytest.raises(psycopg.errors.CheckViolation):
        index(db, numbers=[0, 2], version="synthetic-v2")
    assert rows(db, "review_case_theory_link") == original
    assert rows(db, "review_case_theory_link_audit") == []
    assert_state(db, "supported", False)


def test_indexing_path_is_execute_only():
    calls = []
    cursor = SimpleNamespace(execute=lambda *args: calls.append(args))
    for autocommit in (False, True):
        connection = SimpleNamespace(autocommit=autocommit, cursor=lambda: nullcontext(cursor),
            transaction=nullcontext, commit=lambda: None, rollback=lambda: None)
        index(connection)
        delete_postgres_document(connection, matter_id="m-a", document_id="doc-1")
        delete_postgres_matter_documents(connection, matter_id="m-a")
    assert any("INSERT INTO review_chunk" in call[0] for call in calls)
    assert any("DELETE FROM review_document" in call[0] for call in calls)


@pytest.mark.parametrize("status", ["supported", "contested", "unsupported"])
@pytest.mark.postgres
def test_assertion_deleted_audits_each_link_and_flags(db, status):
    claim = seed(db, status=status)
    passages = rows(db, "review_case_theory_link")
    # fixture-write: seed a synthetic assertion owner with two links and surviving passage support.
    db.execute("INSERT INTO review_case_theory_assertion_owner VALUES ('assertion-a','m-a')")
    for ordinal in (3, 4):
        # fixture-write: two distinct synthetic links test that every lost link gets an audit receipt.
        db.execute("INSERT INTO review_case_theory_link(matter_id,claim_id,ordinal,kind,assertion_id) VALUES ('m-a',%s,%s,'assertion','assertion-a')", (claim, ordinal))
    lost = rows(db, "review_case_theory_link")[2:]
    # fixture-write: delete the synthetic assertion owner through its cascading FK.
    db.execute("DELETE FROM review_case_theory_assertion_owner WHERE assertion_id='assertion-a'")
    assert rows(db, "review_case_theory_link") == passages
    assert_audits(db, lost, "assertion_deleted")
    assert_state(db, status, True)


@pytest.mark.postgres
def test_claim_deleted_audits_each_link(db):
    claim = seed(db, status="contested")
    original = rows(db, "review_case_theory_link")
    # fixture-write: delete the synthetic claim while its source passages remain present.
    db.execute("DELETE FROM review_case_theory_claim WHERE matter_id='m-a' AND claim_id=%s", (claim,))
    assert rows(db, "review_case_theory_link") == []
    assert rows(db, "review_case_theory_claim") == []
    assert len(rows(db, "review_chunk")) == 2
    assert_audits(db, original, "claim_deleted")
