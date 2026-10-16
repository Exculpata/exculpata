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
