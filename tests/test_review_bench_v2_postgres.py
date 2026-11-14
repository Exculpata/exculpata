from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from case_intelligence.pilot_uploads import DOCUMENT_MEDIA_TYPES, MEDIA_TYPES, PilotUnit

from case_intelligence.review_bench_v2 import (
    DeterministicEmbeddingAdapter,
    DeterministicReranker,
    HybridRetriever,
    PdfPage,
    PostgresHybridBackend,
    RemoteEmbeddingAdapter,
    RemoteRerankerAdapter,
    extract_pdf_pages,
    delete_postgres_document,
    delete_postgres_matter,
    delete_postgres_matter_documents,
    initialize_postgres_demo,
    retain_postgres_workbench_matters,
    upsert_postgres_document,
)

pytestmark = [pytest.mark.usefixtures("postgres_schema"), pytest.mark.skipif(
    not os.getenv("CASE_REVIEW_POSTGRES_TEST_DSN"),
    reason="set CASE_REVIEW_POSTGRES_TEST_DSN for the isolated development database",
)]


def test_live_postgres_migration_reopen_and_hybrid_page_citation():
    import psycopg

    dsn = os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"]
    pages = {
        "matter-alpha": (
            PdfPage(1, "A routine synthetic cover page."),
            PdfPage(2, "The shared red bicycle was logged at the north entrance."),
        ),
        "matter-bravo": (
            PdfPage(1, "A separate synthetic cover page."),
            PdfPage(2, "The shared red bicycle was logged at the south entrance."),
        ),
    }
    with psycopg.connect(dsn) as connection:
        initialize_postgres_demo(connection, pages)
    with psycopg.connect(dsn) as reopened:
        with reopened.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM review_schema_migration WHERE version=1")
            assert cursor.fetchone() == (1,)
        retriever = HybridRetriever(
            PostgresHybridBackend(reopened),
            DeterministicEmbeddingAdapter(),
            DeterministicReranker(),
        )
        alpha = retriever.search("matter-alpha", "bicycle north entrance")
        bravo = retriever.search("matter-bravo", "bicycle south entrance")
        assert alpha[0].citation == "Page 2"
        assert bravo[0].citation == "Page 2"
        assert all(item.matter_id == "matter-alpha" for item in alpha)
        assert all(item.matter_id == "matter-bravo" for item in bravo)


def test_live_postgres_generic_txt_document_upsert_is_matter_scoped():
    import psycopg

    with psycopg.connect(os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"]) as connection:
        upsert_postgres_document(
            connection,
            matter_id="matter-pilot",
            matter_name="Pilot Matter",
            document_id="synthetic-generic-document",
            display_name="Synthetic notes.txt",
            media_type="text/plain",
            units=(PilotUnit(1, "The amber notebook was stored in cabinet twelve.", 1, 1),),
        )
        backend = PostgresHybridBackend(connection)
        rows = backend.lexical("matter-pilot", "amber notebook cabinet", 10)
        assert len(rows) == 1
        assert rows[0].citation == "Line 1"
        assert backend.lexical("matter-alpha", "amber notebook cabinet", 10) == ()


@pytest.fixture
def lexical_backend(postgres_schema):
    import psycopg

    matter_id = f"ci-matter-{uuid.uuid4().hex}"
    with psycopg.connect(os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"]) as connection:
        try:
            for document_id, text in (
                ("most-terms", "The red bicycles arrived at the north entrance."),
                ("one-term", "The bicycle was stored beside the empty cabinet."),
                ("separated-phrase", "The red cabinet held a bicycle."),
                ("excluded", "The red bicycle was damaged at the north entrance."),
                ("irrelevant", "An amber notebook remained inside the locker."),
            ):
                upsert_postgres_document(
                    connection, matter_id=matter_id, matter_name="Synthetic lexical ranking",
                    document_id=document_id, display_name=f"{document_id}.txt",
                    media_type="text/plain", units=(PilotUnit(1, text, 1, 1),),
                )
            yield PostgresHybridBackend(connection), matter_id
        finally:
            delete_postgres_matter(connection, matter_id=matter_id)


def test_live_postgres_lexical_matches_partial_question_and_ranks_more_terms_first(lexical_backend):
    backend, matter_id = lexical_backend
    query = "Where did the red bicycle arrive near the north entrance yesterday?"
    rows = backend.lexical(matter_id, query, 10)
    by_document = {row.document_id: row for row in rows}
    assert {"most-terms", "one-term"} <= by_document.keys()
    assert "irrelevant" not in by_document
    assert by_document["most-terms"].score > by_document["one-term"].score
    ordered = [row.document_id for row in rows]
    assert ordered.index("most-terms") < ordered.index("one-term")
    assert backend.lexical(matter_id, query, 1) == rows[:1]
    assert backend.lexical(f"{matter_id}-other", query, 10) == ()
    scoped = PostgresHybridBackend(backend.connection, document_ids=frozenset({"one-term"}))
    assert [row.document_id for row in scoped.lexical(matter_id, query, 10)] == ["one-term"]


@pytest.mark.parametrize(
    ("query", "expected"),
    (
        ('"red bicycle"', {"most-terms", "excluded"}),
        ('"red bicycle" north', {"most-terms", "excluded"}),
        ("red bicycle -damaged", {"most-terms", "separated-phrase"}),
        ("bicycle -north -cabinet", set()),
        ("the and or", set()),
        ("' ; : & | ( )", set()),
        ("", set()),
    ),
)
def test_live_postgres_lexical_preserves_websearch_and_empty_queries(lexical_backend, query, expected):
    backend, matter_id = lexical_backend
    assert {row.document_id for row in backend.lexical(matter_id, query, 10)} == expected


def test_live_postgres_media_types_and_zero_timestamp_projection():
    import psycopg

    matter_id = f"ci-matter-{uuid.uuid4().hex}"
    dsn = os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"]
    with psycopg.connect(dsn) as connection:
        try:
            for ordinal, media_type in enumerate(sorted(set(MEDIA_TYPES.values())), 1):
                document_id = f"generated-media-{ordinal}"
                upsert_postgres_document(
                    connection,
                    matter_id=matter_id,
                    matter_name="Generated media projection fixture",
                    document_id=document_id,
                    display_name=f"Generated recording {ordinal}",
                    media_type=media_type,
                    units=(
                        PilotUnit(
                            1,
                            "MEDIA TIMESTAMP PROJECTION CANARY",
                            line_start=0,
                            line_end=2_500,
                            start_ms=0,
                            end_ms=2_500,
                        ),
                    ),
                    source_version_id=f"generated-v{ordinal}",
                )
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT array_agg(DISTINCT media_type ORDER BY media_type) "
                    "FROM review_document WHERE matter_id=%s",
                    (matter_id,),
                )
                assert cursor.fetchone()[0] == sorted(set(MEDIA_TYPES.values()))
                cursor.execute(
                    "SELECT count(*) FROM review_chunk "
                    "WHERE matter_id=%s AND line_start=0 AND line_end=2500",
                    (matter_id,),
                )
                assert cursor.fetchone() == (len(set(MEDIA_TYPES.values())),)
            assert PostgresHybridBackend(connection).lexical(
                matter_id, "timestamp projection canary", 20
            )
        finally:
            delete_postgres_matter(connection, matter_id=matter_id)


def test_live_postgres_indexes_every_canonical_document_media_type():
    import psycopg

    matter_id = f"ci-matter-{uuid.uuid4().hex}"
    media_types = sorted(set(DOCUMENT_MEDIA_TYPES.values()))
    with psycopg.connect(os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"]) as connection:
        try:
            for ordinal, media_type in enumerate(media_types, 1):
                upsert_postgres_document(
                    connection,
                    matter_id=matter_id,
                    matter_name="Generated extended-source projection fixture",
                    document_id=f"generated-extended-{ordinal}",
                    display_name=f"Generated source {ordinal}",
                    media_type=media_type,
                    units=(
                        PilotUnit(
                            1,
                            f"EXTENDED SOURCE PROJECTION CANARY {ordinal}",
                            1,
                            1,
                        ),
                    ),
                    source_version_id=f"generated-v{ordinal}",
                )
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT array_agg(DISTINCT media_type ORDER BY media_type) "
                    "FROM review_document WHERE matter_id=%s",
                    (matter_id,),
                )
                assert cursor.fetchone()[0] == media_types
            assert len(
                PostgresHybridBackend(connection).lexical(
                    matter_id, "extended source projection canary", 100
                )
            ) == len(media_types)
        finally:
            delete_postgres_matter(connection, matter_id=matter_id)


def test_live_postgres_delete_and_rebuild_are_matter_scoped_and_leave_no_stale_chunks():
    import psycopg

    dsn = os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"]
    with psycopg.connect(dsn) as connection:
        alpha = (PilotUnit(1, "ALPHA DELETE CANARY", 1, 1),)
        bravo = (PilotUnit(1, "BRAVO PRESERVE CANARY", 1, 1),)
        upsert_postgres_document(
            connection, matter_id="matter-pilot", matter_name="Pilot Matter",
            document_id="shared-id", display_name="Pilot.txt", media_type="text/plain",
            units=alpha, source_version_id="pilot-v1",
        )
        upsert_postgres_document(
            connection, matter_id="matter-bravo", matter_name="South Entrance Review",
            document_id="shared-id", display_name="Bravo.txt", media_type="text/plain",
            units=bravo, source_version_id="bravo-v1",
        )
        delete_postgres_document(
            connection, matter_id="matter-pilot", document_id="shared-id"
        )
        backend = PostgresHybridBackend(connection)
        assert backend.lexical("matter-pilot", "ALPHA DELETE CANARY", 10) == ()
        assert backend.lexical("matter-bravo", "BRAVO PRESERVE CANARY", 10)

        upsert_postgres_document(
            connection, matter_id="matter-pilot", matter_name="Pilot Matter",
            document_id="stale-doc", display_name="Stale.txt", media_type="text/plain",
            units=(PilotUnit(1, "STALE REBUILD CANARY", 1, 1),),
            source_version_id="stale-v1",
        )
        delete_postgres_matter_documents(connection, matter_id="matter-pilot")
        assert backend.lexical("matter-pilot", "STALE REBUILD CANARY", 10) == ()
        assert backend.lexical("matter-bravo", "BRAVO PRESERVE CANARY", 10)


def test_live_postgres_projection_removes_matters_absent_from_workspace():
    import psycopg

    with psycopg.connect(os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"]) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT matter_id FROM review_matter "
                "WHERE matter_id ~ '^ci-matter-[0-9a-f]{32}$'"
            )
            existing = tuple(row[0] for row in cursor.fetchall())
        retained_id = f"ci-matter-{uuid.uuid4().hex}"
        stale_id = f"ci-matter-{uuid.uuid4().hex}"
        try:
            for matter_id, canary in (
                (retained_id, "RETAINED PROJECTION CANARY"),
                (stale_id, "STALE PROJECTION CANARY"),
            ):
                upsert_postgres_document(
                    connection,
                    matter_id=matter_id,
                    matter_name=matter_id,
                    document_id="projection.txt",
                    display_name="Projection.txt",
                    media_type="text/plain",
                    units=(PilotUnit(1, canary, 1, 1),),
                )
            retain_postgres_workbench_matters(
                connection,
                matter_ids=(*existing, retained_id),
            )
            backend = PostgresHybridBackend(connection)
            assert backend.lexical(retained_id, "RETAINED PROJECTION CANARY", 10)
            assert backend.lexical(stale_id, "STALE PROJECTION CANARY", 10) == ()
        finally:
            with connection.cursor() as cursor:
                # fixture-write: Clean up the disposable PostgreSQL projection on its independent connection.
                cursor.execute(
                    "DELETE FROM review_matter WHERE matter_id = ANY(%s)",
                    ([retained_id, stale_id],),
                )
            # fixture-write: Commit cleanup on the disposable PostgreSQL connection, not the shared SQLite workspace.
            connection.commit()


def test_live_postgres_whole_matter_delete_cascades_without_touching_neighbor():
    import psycopg

    first_id = f"ci-matter-{uuid.uuid4().hex}"
    second_id = f"ci-matter-{uuid.uuid4().hex}"
    with psycopg.connect(os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"]) as connection:
        try:
            for matter_id, canary in (
                (first_id, "WHOLE MATTER DELETE CANARY"),
                (second_id, "WHOLE MATTER PRESERVE CANARY"),
            ):
                upsert_postgres_document(
                    connection,
                    matter_id=matter_id,
                    matter_name="Generated projection fixture",
                    document_id="generated.txt",
                    display_name="Generated.txt",
                    media_type="text/plain",
                    units=(PilotUnit(1, canary, 1, 1),),
                )
            delete_postgres_matter(connection, matter_id=first_id)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT matter_id FROM review_matter WHERE matter_id = ANY(%s) ORDER BY matter_id",
                    ([first_id, second_id],),
                )
                assert cursor.fetchall() == [(second_id,)]
                cursor.execute(
                    "SELECT COUNT(*) FROM review_chunk WHERE matter_id=%s", (first_id,)
                )
                assert cursor.fetchone() == (0,)
        finally:
            with connection.cursor() as cursor:
                # fixture-write: Clean up the disposable PostgreSQL projection on its independent connection.
                cursor.execute(
                    "DELETE FROM review_matter WHERE matter_id = ANY(%s)",
                    ([first_id, second_id],),
                )
            # fixture-write: Commit cleanup on the disposable PostgreSQL connection, not the shared SQLite workspace.
            connection.commit()


def test_live_learned_dense_only_paraphrase_and_unsupported_abstention():
    worker_url = os.getenv("CASE_REVIEW_MODEL_WORKER_URL")
    if not worker_url:
        pytest.skip("set CASE_REVIEW_MODEL_WORKER_URL for the pinned learned-model worker")
    import psycopg

    pages = extract_pdf_pages(
        Path(__file__).parents[1]
        / "src/case_intelligence/demo_data/synthetic_case_report.pdf"
    )
    embedder = RemoteEmbeddingAdapter(worker_url)
    reranker = RemoteRerankerAdapter(worker_url)
    with psycopg.connect(os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"]) as connection:
        initialize_postgres_demo(connection, {"matter-alpha": pages}, embedder=embedder)
        backend = PostgresHybridBackend(connection)
        assert backend.lexical("matter-alpha", "cycle appear", 100) == ()
        retriever = HybridRetriever(
            backend,
            embedder,
            reranker,
            minimum_dense_score=0.72,
            minimum_rerank_score=0.80,
        )
        semantic = retriever.search("matter-alpha", "where did the cycle appear")
        assert semantic and semantic[0].page_number == 2
        assert retriever.search("matter-alpha", "unfindable zephyr term") == ()
        assert retriever.search("matter-alpha", "blood alcohol level") == ()


@pytest.fixture
def load_file_projection_upload_storage(monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")


def test_load_file_empty_projection_postgres(tmp_path, load_file_projection_upload_storage):
    import hashlib
    import time

    import psycopg
    from fastapi.testclient import TestClient

    from case_intelligence.generation import UnavailableGenerator
    from case_intelligence.malware_scan import MalwareScanResult, MalwareScannerStatus
    from case_intelligence.managed_storage import StoragePolicy
    from case_intelligence.source_locations import SourceLocationRegistry
    from case_intelligence.workbench import create_workbench_app

    class CleanProjectionScanner:
        def __init__(self):
            self.calls = 0

        def status(self, *, force=False):
            return MalwareScannerStatus('ready', 'synthetic')

        def scan(self, path):
            assert path.is_file()
            self.calls += 1
            return MalwareScanResult('clean', 'synthetic')

    scanner = CleanProjectionScanner()
    dsn = os.environ['CASE_REVIEW_POSTGRES_TEST_DSN']
    runtime = tmp_path / 'runtime'
    sources = (
        ('generated.DAT', b'Control\x14Description\nGEN-1\x14Metadata cedar canary\n'),
        ('generated.OPT', b'GEN-1,VOL,Images/page.tif,Y,,,1\n'),
        ('generated.txt', b'Generated searchable turquoise lantern passage.\n'),
    )
    actor = 'development-taylor-morgan'
    assert DeterministicEmbeddingAdapter().embed(()) == ()

    def application(connection):
        return create_workbench_app(
            runtime, generator=UnavailableGenerator(), auth_mode='test',
            postgres_connection=connection, learned_retrieval=False,
            embedding=DeterministicEmbeddingAdapter(), reranker=DeterministicReranker(),
            source_registry=SourceLocationRegistry(), background_ingestion=True,
            ingestion_workers=1, malware_scanner=scanner, malware_scan_mode='extended',
            storage_policy=StoragePolicy(reserve_bytes=0),
        )

    def assert_projection(connection, matter_id, identities):
        # fixture-write: Read zero-unit counts and identities from the isolated PostgreSQL projection; no writes.
        with connection.cursor() as cursor:
            for name, raw in sources:
                document_id, version_id = identities[name]
                is_load_file = name != 'generated.txt'
                cursor.execute(
                    'SELECT media_type,page_count,current_source_version_id '
                    'FROM review_document WHERE matter_id=%s AND document_id=%s',
                    (matter_id, document_id),
                )
                row = cursor.fetchone()
                assert row is not None and row[0] == 'text/plain' and row[2] == version_id
                assert row[1] == (0 if is_load_file else 1)
                cursor.execute(
                    'SELECT count(*) FROM review_page WHERE matter_id=%s AND document_id=%s',
                    (matter_id, document_id),
                )
                assert cursor.fetchone() == ((0 if is_load_file else 1),)
                cursor.execute(
                    'SELECT count(*) FROM review_chunk WHERE matter_id=%s AND document_id=%s',
                    (matter_id, document_id),
                )
                assert cursor.fetchone() == ((0 if is_load_file else 1),)
                cursor.execute(
                    'SELECT content_digest,byte_size,status FROM review_source_version '
                    'WHERE matter_id=%s AND document_id=%s AND source_version_id=%s',
                    (matter_id, document_id, version_id),
                )
                assert cursor.fetchone() == (hashlib.sha256(raw).hexdigest(), len(raw), 'ready')
                cursor.execute(
                    'SELECT state,completed_units,total_units FROM review_processing_state '
                    'WHERE matter_id=%s AND document_id=%s AND stage=%s',
                    (matter_id, document_id, 'native extraction'),
                )
                expected_units = 0 if is_load_file else 1
                assert cursor.fetchone() == ('ready', expected_units, expected_units)
        backend = PostgresHybridBackend(connection)
        assert backend.lexical(matter_id, 'metadata cedar canary', 10) == ()
        found = backend.lexical(matter_id, 'turquoise lantern passage', 10)
        assert len(found) == 1 and found[0].document_id == identities['generated.txt'][0]
        assert backend.lexical('generated-unrelated-matter', 'turquoise lantern passage', 10) == ()

    matter_id = None
    try:
        with TestClient(application(psycopg.connect(dsn))) as client:
            bench = client.app.state.workbench
            assert bench.postgres_ready
            created = client.post('/matters', data={'name': 'Generated load-file projection'},
                                  follow_redirects=False)
            assert created.status_code == 303
            slug = created.headers['location'].split('/')[2]
            matter = bench.matter(slug, actor)
            matter_id = matter.matter_id
            identities = {}
            for ordinal, (name, raw) in enumerate(sources, 1):
                session = client.post(f'/matters/{slug}/upload-sessions', json={'files': [
                    {'name': name, 'relative_path': name, 'size': len(raw),
                     'media_type': 'text/plain'}]})
                assert session.status_code in (200, 201), session.text
                item = session.json()['items'][0]
                transferred = client.put(item['chunk_url'], content=raw, headers={
                    'Content-Type': 'application/octet-stream', 'X-Upload-Offset': '0'})
                assert transferred.status_code == 200, transferred.text
                finalized = client.post(item['finalize_url'])
                assert finalized.status_code == 200, finalized.text
                source_store = bench.source_store(matter)
                document = next(value for value in source_store.documents.values()
                                if value.display_name == name)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    job = bench.workspace.ingest_job(matter_id, document.document_id)
                    if job is not None and job.state in {'succeeded', 'failed', 'cancelled'}:
                        break
                    time.sleep(0.02)
                job = bench.workspace.ingest_job(matter_id, document.document_id)
                assert job is not None and job.state == 'succeeded', job
                document = source_store.get(document.document_id)
                assert document.state == 'ready'
                assert document.digest == hashlib.sha256(raw).hexdigest()
                assert (source_store.files / document.stored_name).read_bytes() == raw
                identities[name] = (document.document_id, document.version_id)
                if ordinal <= 2:
                    assert document.parsed_units() == () and document.page_count == 0
                    readiness = bench.workspace.matter_readiness(matter_id)
                    assert readiness.total_count == readiness.saved_count == ordinal
                    assert readiness.searchable_count == readiness.extracted_count == 0
                    assert readiness.processing_count == 0
                    assert readiness.attention_count == readiness.stored_only_count == ordinal
                    assert not readiness.can_query
            assert scanner.calls == 2
            with psycopg.connect(dsn) as inspection:
                assert_projection(inspection, matter_id, identities)

        with TestClient(application(psycopg.connect(dsn))) as client:
            bench = client.app.state.workbench
            assert bench.postgres_ready
            matter = bench.matter(slug, actor)
            source_store = bench.source_store(matter)
            for name, raw in sources:
                document = source_store.get(identities[name][0])
                assert document.version_id == identities[name][1]
                assert document.state == 'ready'
                assert (source_store.files / document.stored_name).read_bytes() == raw
                job = bench.workspace.ingest_job(matter_id, document.document_id)
                assert job is not None and job.state == 'succeeded'
            readiness = bench.workspace.matter_readiness(matter_id)
            assert readiness.total_count == readiness.saved_count == 3
            assert readiness.extracted_count == readiness.searchable_count == 1
            assert readiness.processing_count == 0
            assert readiness.stored_only_count == readiness.attention_count == 2
            assert readiness.can_query and readiness.partial_query
            with psycopg.connect(dsn) as inspection:
                assert_projection(inspection, matter_id, identities)
            assert scanner.calls == 2
    finally:
        if matter_id is not None:
            with psycopg.connect(dsn) as cleanup:
                delete_postgres_matter(cleanup, matter_id=matter_id)
