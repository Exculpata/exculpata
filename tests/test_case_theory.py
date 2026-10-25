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
def theory_store():
    import psycopg

    connection = psycopg.connect(os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"])
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
