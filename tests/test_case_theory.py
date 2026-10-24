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
    for matter, marker in ((matter_a, "amber"), (matter_b, "blue")):
        upsert_postgres_document(
            connection, matter_id=matter, matter_name=f"Synthetic {marker}",
            document_id="source-1", display_name=f"Synthetic {marker}.txt",
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
    assert service.list_claims(matter, 1)[0]["links"] == [source_link()]
    with pytest.raises(CaseTheoryError, match="at least one citation"):
        service.create_version(matter, [{"text": "Uncited", "status": "unsupported", "links": []}])
    with pytest.raises(CaseTheoryError, match="status"):
        service.create_version(matter, [{"text": "Invalid", "status": "confirmed", "links": [source_link()]}])


def test_matter_isolation_export_restrictions_and_purge(theory_store):
    connection, service, matter_a, matter_b = theory_store
    with pytest.raises(CaseTheoryError, match="another matter"):
        service.create_version(matter_a, [{"text": "Wrong assertion", "status": "contested", "links": [{"kind": "assertion", "assertion_id": "assertion-b"}]}])
    with pytest.raises(CaseTheoryError, match="another matter"):
        service.create_version(matter_b, [{"text": "Wrong passage", "status": "unsupported", "links": [source_link()]}])

    service.create_version(matter_a, [{"text": "Exported claim", "status": "supported", "links": [source_link()]}])
    exported = service.export(matter_a)
    assert exported["versions"][0]["claims"][0]["links"][0]["protective_order"] == "limited-to-case-team"
    delete_postgres_matter(connection, matter_id=matter_a)
    with pytest.raises(CaseTheoryError, match="not found"):
        service.current(matter_a)
