from __future__ import annotations

import os
import uuid

import pytest

from case_intelligence.case_theory import CaseTheoryError, CaseTheoryRepository, CaseTheoryService
from case_intelligence.pilot_uploads import PilotUnit
from case_intelligence.review_bench_v2 import delete_postgres_matter, upsert_postgres_document


pytestmark = pytest.mark.skipif(
    not os.getenv("CASE_REVIEW_POSTGRES_TEST_DSN"),
    reason="set CASE_REVIEW_POSTGRES_TEST_DSN for the isolated development database",
)


@pytest.fixture
def theory_store(isolated_theory_dsn):
    import psycopg

    connection = psycopg.connect(isolated_theory_dsn)
    matter_a = f"ci-matter-{uuid.uuid4().hex}"
    matter_b = f"ci-matter-{uuid.uuid4().hex}"
    for matter, document, marker in (
        (matter_a, "source-1", "amber"),
        (matter_b, "source-1", "blue"),
        (matter_b, "source-b-only", "blue"),
    ):
        upsert_postgres_document(
            connection, matter_id=matter, matter_name=f"Synthetic {marker}",
            document_id=document, display_name=f"Synthetic {marker}.txt",
            media_type="text/plain", units=(PilotUnit(1, f"The {marker} item was logged.", 1, 1),),
        )
    with connection.cursor() as cursor:
        # fixture-write: configure a synthetic source restriction for export coverage.
        cursor.execute(
            "UPDATE review_document SET protective_order=%s WHERE matter_id=%s",
            ("limited-to-case-team", matter_a),
        )
    # fixture-write: finish the synthetic restriction setup.
    connection.commit()
    assertions = {"assertion-a": matter_a, "assertion-b": matter_b}
    service = CaseTheoryService(
        CaseTheoryRepository(connection),
        authorize=lambda requested: requested in {matter_a, matter_b},
        assertion_matter=lambda assertion_id: assertions.get(assertion_id),
    )
    yield connection, service, matter_a, matter_b
    # fixture-write: roll back a failed probe on the independent synthetic PostgreSQL connection.
    connection.rollback()
    for matter in (matter_a, matter_b):
        delete_postgres_matter(connection, matter_id=matter)
    connection.close()


def source_link():
    return {"kind": "source_passage", "document_id": "source-1", "chunk_id": "chunk-1"}


def test_versioning_status_values_and_cited_claims(theory_store):
    _, service, matter, _ = theory_store
    first = service.create_version(matter, [{"text": "A synthetic item was logged.", "status": "supported", "links": [source_link()]}])
    second = service.create_version(matter, [{"text": "The timing remains disputed.", "status": "contested", "links": [{"kind": "assertion", "assertion_id": "assertion-a"}]}])

    assert (first["version"], second["version"]) == (1, 2)
    assert service.current(matter) == second
    assert service.for_report(matter) == second
    assert service.version(matter, 1) == first
    assert service.list_claims(matter, 1)[0]["links"] == [
        {**source_link(), "protective_order": "limited-to-case-team"}
    ]
    with pytest.raises(CaseTheoryError, match="at least one citation"):
        service.create_version(matter, [{"text": "Uncited", "status": "unsupported", "links": []}])
    with pytest.raises(CaseTheoryError, match="status"):
        service.create_version(matter, [{"text": "Invalid", "status": "confirmed", "links": [source_link()]}])


def test_matter_isolation_export_restrictions_and_purge(theory_store):
    connection, service, matter_a, matter_b = theory_store
    with pytest.raises(CaseTheoryError, match="another matter"):
        service.create_version(matter_a, [{"text": "Wrong assertion", "status": "contested", "links": [{"kind": "assertion", "assertion_id": "assertion-b"}]}])
    with pytest.raises(CaseTheoryError, match="another matter"):
        service.create_version(matter_a, [{"text": "Wrong passage", "status": "unsupported", "links": [
            {**source_link(), "document_id": "source-b-only"}
        ]}])

    service.create_version(matter_a, [{"text": "Exported claim", "status": "supported", "links": [source_link()]}])
    exported = service.export(matter_a)
    assert exported["versions"][0]["claims"][0]["links"][0]["protective_order"] == "limited-to-case-team"
    delete_postgres_matter(connection, matter_id=matter_a)
    with pytest.raises(CaseTheoryError, match="not found"):
        service.current(matter_a)
    assert service.export(matter_a)["versions"] == []
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT (SELECT count(*) FROM review_case_theory_claim WHERE matter_id=%s), "
            "(SELECT count(*) FROM review_case_theory_link WHERE matter_id=%s)",
            (matter_a, matter_a),
        )
        assert cursor.fetchone() == (0, 0)


def test_reused_source_ids_keep_matter_specific_restrictions_on_every_read(theory_store):
    _, service, matter_a, matter_b = theory_store
    for matter, expected in (
        (matter_a, {**source_link(), "protective_order": "limited-to-case-team"}),
        (matter_b, source_link()),
    ):
        created = service.create_version(matter, [{
            "text": "A synthetic local passage.", "status": "supported", "links": [source_link()],
        }])
        for snapshot in (
            created, service.current(matter), service.version(matter, 1),
            service.for_report(matter), service.export(matter)["versions"][0],
        ):
            assert snapshot["claims"][0]["links"] == [expected]
        assert service.list_claims(matter)[0]["links"] == [expected]


@pytest.mark.parametrize("link", [
    {"kind": "source_passage", "document_id": "source-b-only", "chunk_id": "chunk-1"},
    {"kind": "source_passage", "document_id": "source-1", "chunk_id": "missing-chunk"},
])
def test_invalid_passage_is_rejected_by_service_and_database(theory_store, link):
    import psycopg

    connection, service, matter, _ = theory_store
    claims = [{"text": "Invalid synthetic citation.", "status": "unsupported", "links": [link]}]
    with pytest.raises(CaseTheoryError, match="another matter or an unavailable passage"):
        service.create_version(matter, claims)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        CaseTheoryRepository(connection).create(matter, claims, "case-team")
    assert service.export(matter)["versions"] == []
    valid = service.create_version(matter, [{
        "text": "Valid local citation.", "status": "supported", "links": [source_link()],
    }])
    assert valid["version"] == 1


@pytest.fixture
def isolated_theory_dsn():
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    database = f"theory_{uuid.uuid4().hex}"
    with psycopg.connect(os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"], autocommit=True) as admin:
        # fixture-write: create an isolated synthetic database for destructive maintenance.
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        try:
            yield make_conninfo(os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"], dbname=database)
        finally:
            # fixture-write: remove only this test's synthetic database and connections.
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


def assert_link_audit(connection, matter, claim, document, reason):
    import json

    # fixture-write: this independent PostgreSQL cursor reads only the synthetic unlink audit.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT row_to_json(a) FROM review_case_theory_link_audit a WHERE matter_id=%s",
            (matter,),
        )
        rows = [row[0] for row in cursor.fetchall()]
    assert rows == [{
        "matter_id": matter, "claim_id": claim, "document_id": document,
        "chunk_id": "chunk-1", "link_ordinal": 1, "assertion_id": None, "reason": reason,
    }]
    assert "The amber item was logged." not in json.dumps(rows)
    assert "Synthetic claim text." not in json.dumps(rows)


@pytest.mark.parametrize("operation", ["reindex", "delete", "rebuild", "startup"])
def test_cited_passage_maintenance_keeps_postgres_ready(
    isolated_theory_dsn, tmp_path, monkeypatch, operation,
):
    import io
    import psycopg
    from case_intelligence.review_bench import ReviewBench
    from case_intelligence.workbench import CaseIntelligenceWorkbench

    monkeypatch.setenv("CASE_REVIEW_POSTGRES_DSN", isolated_theory_dsn)
    monkeypatch.setenv("CASE_REVIEW_ENABLE_MODELS", "0")
    bench = ReviewBench(tmp_path / "bench")
    try:
        assert bench.postgres_ready
        document, _ = bench.pilot.store_stream(
            "Synthetic.txt", "text/plain", io.BytesIO(b"The amber item was logged."), request_used=0,
        )
        bench.pilot.process(document.document_id)
        bench.index_pilot_document(document.document_id)
        assert document.state == "ready"
        matter = "matter-pilot" if operation != "startup" else f"ci-matter-{uuid.uuid4().hex}"
        if operation == "startup":
            upsert_postgres_document(
                bench.postgres_connection, matter_id=matter, matter_name="Synthetic",
                document_id=document.document_id, display_name="Synthetic.txt", media_type="text/plain",
                units=document.parsed_units(),
            )
        service = CaseTheoryService(
            CaseTheoryRepository(bench.postgres_connection), authorize=lambda _: True,
            assertion_matter=lambda _: None,
        )
        snapshot = service.create_version(matter, [{
            "text": "Synthetic claim text.", "status": "supported",
            "links": [{**source_link(), "document_id": document.document_id}],
        }])
        claim = snapshot["claims"][0]["claim_id"]
        if operation == "reindex":
            bench.index_pilot_document(document.document_id)
            assert document.state == "ready"
        elif operation == "delete":
            bench._delete_pilot_postgres(document.document_id)
        elif operation == "rebuild":
            bench._refresh_pilot_retrieval(index_postgres=True)
            assert document.state == "ready"
        else:
            with psycopg.connect(isolated_theory_dsn) as connection:
                workspace = CaseIntelligenceWorkbench(tmp_path / "workspace", postgres_connection=connection)
                try:
                    assert workspace.postgres_ready
                finally:
                    workspace.close()
        assert bench.postgres_ready
        with bench.postgres_connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM review_case_theory_link WHERE matter_id=%s", (matter,))
            assert cursor.fetchone() == (0,)
            cursor.execute("SELECT count(*) FROM review_chunk WHERE matter_id=%s AND document_id=%s",
                           (matter, document.document_id))
            assert cursor.fetchone() == ((1,) if operation in {"reindex", "rebuild"} else (0,))
        assert_link_audit(bench.postgres_connection, matter, claim, document.document_id,
                          "matter_deleted" if operation == "startup" else "passage_deleted")
    finally:
        bench.close()


def test_explicit_unlink_audits_only_ids_and_reason(theory_store):
    connection, service, matter, _ = theory_store
    snapshot = service.create_version(matter, [{
        "text": "Synthetic claim text.", "status": "supported", "links": [source_link()],
    }])
    with connection.cursor() as cursor:
        # fixture-write: exercise direct SQL unlink on synthetic case theory data.
        cursor.execute("DELETE FROM review_case_theory_link WHERE matter_id=%s", (matter,))
    assert_link_audit(connection, matter, snapshot["claims"][0]["claim_id"], "source-1", "explicit_unlink")


@pytest.mark.parametrize("boundary", ["repository", "sql", "update"])
def test_foreign_assertion_rejected_by_database(theory_store, boundary):
    import psycopg

    connection, service, matter_a, matter_b = theory_store
    foreign_link = {"kind": "assertion", "assertion_id": "assertion-b"}
    service.create_version(matter_b, [{
        "text": "Foreign synthetic assertion.", "status": "supported", "links": [foreign_link],
    }])
    claims = [{"text": "Invalid assertion.", "status": "contested", "links": [foreign_link]}]
    local = service.create_version(matter_a, [{
        "text": "Local assertion.", "status": "supported",
        "links": [{"kind": "assertion", "assertion_id": "assertion-a"}],
    }])
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        if boundary == "repository":
            CaseTheoryRepository(connection).create(matter_a, claims, "case-team")
        else:
            with connection.transaction(), connection.cursor() as cursor:
                if boundary == "sql":
                    # fixture-write: bypass the repository to challenge the database matter fence.
                    cursor.execute(
                        "INSERT INTO review_case_theory_link VALUES (%s,%s,2,'assertion',%s,NULL,NULL)",
                        (matter_a, local["claims"][0]["claim_id"], "assertion-b"),
                    )
                else:
                    # fixture-write: attempt to rebind a synthetic claim's existing citation.
                    cursor.execute(
                        "UPDATE review_case_theory_link SET assertion_id=%s WHERE matter_id=%s",
                        ("assertion-b", matter_a),
                    )


def test_link_lifecycle_upgrade_and_clean_restore(isolated_theory_dsn):
    import subprocess
    from importlib import resources
    import psycopg
    from case_intelligence.review_bench_v2 import apply_postgres_migrations, delete_postgres_document

    with psycopg.connect(isolated_theory_dsn, autocommit=True) as connection:
        migrations = resources.files("case_intelligence").joinpath("migrations/postgresql")
        for migration in sorted(migrations.iterdir(), key=lambda item: item.name):
            if migration.name.endswith(".sql") and int(migration.name[:4]) <= 6:
                # fixture-write: build a stopped version 6 database for the forward migration drill.
                connection.execute(migration.read_text())
        matter = f"ci-matter-{uuid.uuid4().hex}"
        upsert_postgres_document(
            connection, matter_id=matter, matter_name="Synthetic", document_id="source-1",
            display_name="Synthetic.txt", media_type="text/plain",
            units=(PilotUnit(1, "The amber item was logged.", 1, 1),), ensure_schema=False,
        )
        repository = CaseTheoryRepository(connection)
        snapshot = repository.create(matter, [{
            "text": "Synthetic claim text.", "status": "supported", "links": [source_link()],
        }], "case-team")
        claim = snapshot["claims"][0]["claim_id"]
        # fixture-write: preserve a legacy assertion citation through the schema upgrade.
        connection.execute(
            "INSERT INTO review_case_theory_link VALUES (%s,%s,2,'assertion','legacy-assertion',NULL,NULL)",
            (matter, claim),
        )
        with connection.cursor() as cursor:
            assert 7 in apply_postgres_migrations(cursor)
            assert apply_postgres_migrations(cursor) == tuple(range(1, 8))
        assert repository.read(matter)["claims"][0]["links"] == [
            source_link(), {"kind": "assertion", "assertion_id": "legacy-assertion"},
        ]
        backup = subprocess.run(
            ["pg_dump", "--dbname", isolated_theory_dsn, "--no-owner", "--no-privileges"],
            capture_output=True, check=True,
        ).stdout
        # fixture-write: remove the isolated synthetic schema to prove restoration into a clean target.
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        # fixture-write: restore only this test's synthetic dump, including constraints and triggers.
        subprocess.run(["psql", "--dbname", isolated_theory_dsn, "-X", "-v", "ON_ERROR_STOP=1"],
                       input=backup, capture_output=True, check=True)
        assert repository.read(matter)["claims"][0]["links"][1]["assertion_id"] == "legacy-assertion"
        delete_postgres_document(connection, matter_id=matter, document_id="source-1")
        assert_link_audit(connection, matter, claim, "source-1", "passage_deleted")
        assert repository.read(matter)["claims"][0]["links"] == [
            {"kind": "assertion", "assertion_id": "legacy-assertion"},
        ]
