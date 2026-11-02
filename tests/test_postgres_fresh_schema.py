"""An applied migration from another branch must not define the test schema."""

import os
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from case_intelligence.review_bench_v2 import apply_postgres_migrations
from tests.postgres_schema import fresh_postgres_schema


pytestmark = pytest.mark.skipif(
    not os.getenv("CASE_REVIEW_POSTGRES_TEST_DSN"),
    reason="set CASE_REVIEW_POSTGRES_TEST_DSN for the isolated development database",
)


@pytest.fixture(scope="session")
def postgres_base_dsn():
    dsn = os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"]
    schema = f"stale_test_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        # fixture-write: Create only this regression's synthetic stale schema.
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            stale_dsn = make_conninfo(dsn, options=f"-csearch_path={schema},public")
            with psycopg.connect(stale_dsn, autocommit=True) as stale:
                # fixture-write: Reproduce the real applied-version table in the owned stale schema.
                stale.execute("CREATE TABLE review_schema_migration ("
                              "version integer PRIMARY KEY, "
                              "applied_at timestamptz NOT NULL DEFAULT now())")
                # fixture-write: Mark the obsolete branch schema as already migrated through version 6.
                stale.execute("INSERT INTO review_schema_migration(version) "
                              "SELECT generate_series(1, 6)")
                # fixture-write: Reproduce the old audit shape with the absent source-version column.
                stale.execute("CREATE TABLE review_case_theory_link_audit ("
                              "matter_id text, claim_id uuid, link_ordinal integer, "
                              "assertion_id text, document_id text, chunk_id text, "
                              "sha256 text, removed_at timestamptz, reason text)")
            yield stale_dsn
        finally:
            # fixture-write: Remove only the stale schema created by this regression, including on failure.
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_applied_stale_schema_does_not_hide_current_columns(postgres_test_dsn, postgres_base_dsn):
    with psycopg.connect(postgres_test_dsn) as connection:
        schema = connection.execute("SELECT current_schema()").fetchone()[0]
        # fixture-write: Identifier-quoted read of this test's independently owned PostgreSQL schema.
        assert connection.execute(
            sql.SQL("SELECT source_version_id FROM {}.review_case_theory_link_audit").format(
                sql.Identifier(schema)
            )
        ).fetchone() is None
        assert connection.execute(
            "SELECT count(*) FROM review_schema_migration WHERE version=6"
        ).fetchone() == (1,)
        # fixture-write: Reopening the already migrated synthetic schema must remain idempotent.
        with connection.cursor() as cursor:
            assert apply_postgres_migrations(cursor) == (1, 2, 3, 4, 5, 6)
    with psycopg.connect(postgres_base_dsn) as stale:
        assert stale.execute("SELECT current_schema()").fetchone()[0] != schema
        assert stale.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema=current_schema() "
            "AND table_name='review_case_theory_link_audit' AND column_name='source_version_id'"
        ).fetchone() == (0,)


def test_simultaneous_schemas_are_distinct_and_removed(postgres_base_dsn):
    with fresh_postgres_schema(postgres_base_dsn) as first_dsn:
        with fresh_postgres_schema(postgres_base_dsn) as second_dsn:
            with psycopg.connect(first_dsn) as first, psycopg.connect(second_dsn) as second:
                first_schema = first.execute("SELECT current_schema()").fetchone()[0]
                second_schema = second.execute("SELECT current_schema()").fetchone()[0]
                assert first_schema != second_schema
                # fixture-write: A synthetic row in one schema must be invisible in the other.
                first.execute("INSERT INTO review_matter(matter_id, display_name) "
                              "VALUES ('schema-canary', 'Synthetic schema canary')")
                # fixture-write: Publish the synthetic canary before checking the other schema's isolation.
                first.commit()
                assert second.execute("SELECT count(*) FROM review_matter").fetchone() == (0,)
    with psycopg.connect(postgres_base_dsn) as admin:
        for schema in (first_schema, second_schema):
            assert admin.execute("SELECT to_regnamespace(%s)", (schema,)).fetchone() == (None,)


@pytest.mark.parametrize("failure_phase", ["migration", "body"])
def test_failure_still_removes_schema(postgres_base_dsn, monkeypatch, failure_phase):
    schemas = []

    def migrate_then_fail(cursor):
        schemas.append(cursor.connection.execute("SELECT current_schema()").fetchone()[0])
        apply_postgres_migrations(cursor)
        if failure_phase == "migration":
            raise RuntimeError("synthetic migration failure")

    monkeypatch.setattr("tests.postgres_schema.apply_postgres_migrations", migrate_then_fail)
    with pytest.raises(RuntimeError, match="synthetic .* failure"):
        with fresh_postgres_schema(postgres_base_dsn):
            raise RuntimeError("synthetic body failure")
    assert len(schemas) == 1
    with psycopg.connect(postgres_base_dsn) as admin:
        assert admin.execute("SELECT to_regnamespace(%s)", (schemas[0],)).fetchone() == (None,)
