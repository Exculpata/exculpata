"""Equivalent PostgreSQL schema: migration, isolation, constraints and recovery."""
import os
import uuid
import pytest


@pytest.mark.skipif(not os.getenv('CASE_REVIEW_POSTGRES_TEST_DSN'), reason='requires isolated synthetic PostgreSQL')
def test_candidate_review_postgres_migration_and_restore():
    import psycopg
    from case_intelligence.review_bench_v2 import initialize_postgres_demo, PdfPage
    dsn = os.environ['CASE_REVIEW_POSTGRES_TEST_DSN']
    matter_a, matter_b = ['ci-review-' + uuid.uuid4().hex for _ in range(2)]
    with psycopg.connect(dsn) as connection:
        initialize_postgres_demo(connection, {matter_a: (PdfPage(1, 'Synthetic review state fixture.'),),
                                              matter_b: (PdfPage(1, 'Other synthetic review fixture.'),)})
        try:
            connection.execute('INSERT INTO review_candidate_review VALUES (%s,%s,%s,%s,%s,%s,%s)',
                (matter_a, 'inconsistency', 'synthetic-signature', 'reviewed', 'synthetic-reviewer', '2034-04-19T10:00:00Z', 1))
            connection.commit()
            with psycopg.connect(dsn) as reopened:
                rows = reopened.execute('SELECT * FROM review_candidate_review WHERE matter_id=%s', (matter_a,)).fetchall()
                assert len(rows) == 1 and rows[0][3] == 'reviewed'
                assert not reopened.execute('SELECT * FROM review_candidate_review WHERE matter_id=%s', (matter_b,)).fetchall()
                # A clean temporary table tests a data snapshot restore with schema/constraints.
                reopened.execute('CREATE TEMP TABLE synthetic_review_restore (LIKE review_candidate_review INCLUDING ALL)')
                for row in rows:
                    reopened.execute('INSERT INTO synthetic_review_restore VALUES (%s,%s,%s,%s,%s,%s,%s)', row)
                assert reopened.execute('SELECT * FROM synthetic_review_restore').fetchall() == rows
                with pytest.raises(psycopg.errors.CheckViolation), reopened.transaction():
                    reopened.execute("UPDATE synthetic_review_restore SET status='unsupported'")
                assert reopened.execute('SELECT count(*) FROM review_schema_migration WHERE version=6').fetchone()[0] == 1
        finally:
            connection.execute('DELETE FROM review_matter WHERE matter_id IN (%s,%s)', (matter_a, matter_b))
            connection.commit()
            assert not connection.execute('SELECT * FROM review_candidate_review WHERE matter_id=%s', (matter_a,)).fetchall()


@pytest.mark.skipif(not os.getenv('CASE_REVIEW_POSTGRES_TEST_DSN'), reason='requires isolated synthetic PostgreSQL')
def test_candidate_review_postgres_mode_purge(tmp_path, monkeypatch):
    import psycopg
    from psycopg import sql
    from fastapi.testclient import TestClient
    from case_intelligence.generation import UnavailableGenerator
    from case_intelligence.workbench import create_workbench_app
    from tests.test_inconsistency_workflow import seed, WEB_ACTOR
    from tests.case_lifecycle_web_helpers import claim_bench_exit

    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    dsn = os.environ['CASE_REVIEW_POSTGRES_TEST_DSN']
    schema = 'synthetic_review_' + uuid.uuid4().hex
    # Isolate startup reconciliation from every other acceptance fixture.
    with psycopg.connect(dsn, autocommit=True) as control:
        control.execute('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public')
        control.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        try:
            options = f'-c search_path={schema},public'
            with psycopg.connect(dsn, options=options) as connection:
                # Shadow the public ledger so migration discovery cannot borrow
                # another fixture's applied versions through the search path.
                connection.execute('CREATE TABLE review_schema_migration '
                    '(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())')
                connection.commit()
                app = create_workbench_app(tmp_path/'runtime', generator=UnavailableGenerator(),
                    postgres_connection=connection, learned_retrieval=False, auth_mode='test')
                with TestClient(app) as client:
                    bench = app.state.workbench
                    assert bench.postgres_ready and bench._postgres_projection_configured
                    assert connection.execute('SELECT to_regclass(%s) IS NOT NULL',
                        (schema + '.review_candidate_review',)).fetchone()[0]
                    matters = []
                    for _ in range(2):
                        slug, candidate = seed(client)
                        matter = bench.matter(slug, WEB_ACTOR)
                        assert client.post(f'/matters/{slug}/inconsistencies/{candidate["signature"]}/review',
                            data=dict(action='reviewed', revision=0)).status_code == 200
                        connection.execute('INSERT INTO review_candidate_review VALUES (%s,%s,%s,%s,%s,%s,%s)',
                            (matter.matter_id, 'inconsistency', candidate['signature'], 'reviewed',
                             'synthetic-reviewer', '2034-04-19T10:00:00Z', 1))
                        matters.append(matter)
                    connection.commit()
                    target, retained = matters
                    with psycopg.connect(dsn, options=options, autocommit=True) as observer:
                        assert observer.execute('SELECT count(*) FROM review_candidate_review').fetchone()[0] == 2
                        prepared, lifecycle = claim_bench_exit(bench, target)
                        result = bench.execute_matter_purge(prepared, lifecycle)
                        assert result.state == 'deleted'
                        assert not observer.execute('SELECT * FROM review_candidate_review WHERE matter_id=%s',
                            (target.matter_id,)).fetchall()
                        assert not observer.execute('SELECT * FROM review_matter WHERE matter_id=%s',
                            (target.matter_id,)).fetchall()
                        assert observer.execute('SELECT count(*) FROM review_candidate_review WHERE matter_id=%s',
                            (retained.matter_id,)).fetchone()[0] == 1
        finally:
            control.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
