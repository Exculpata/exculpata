"""Disposable PostgreSQL namespaces for synthetic integration tests."""

from contextlib import contextmanager
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from case_intelligence.review_bench_v2 import apply_postgres_migrations


@contextmanager
def fresh_postgres_schema(dsn):
    schema = f"test_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        # fixture-write: Create this worker's randomly named synthetic schema only.
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            options = conninfo_to_dict(dsn).get("options", "")
            scoped_dsn = make_conninfo(dsn, options=f"{options} -csearch_path={schema},public")
            with psycopg.connect(scoped_dsn, autocommit=True) as connection:
                # fixture-write: Apply packaged migrations inside the owned synthetic schema.
                with connection.cursor() as cursor:
                    apply_postgres_migrations(cursor)
            yield scoped_dsn
        finally:
            # fixture-write: Drop only this helper's schema, including after migration or test failure.
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
