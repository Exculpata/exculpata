import os
import shlex
import uuid
from contextlib import contextmanager
from hashlib import sha256
from importlib import resources
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from case_intelligence.review_bench_v2 import apply_postgres_migrations
MIGRATIONS = resources.files("case_intelligence").joinpath("migrations/postgresql")

def postgres_client_command(binary, *arguments):
    return [*shlex.split(os.getenv("CASE_REVIEW_POSTGRES_CLIENT_PREFIX", "")), binary, *arguments]

@contextmanager
def isolated_database():
    database = f"theory_{uuid.uuid4().hex}"
    with psycopg.connect(os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"], autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))  # fixture-write: create an isolated synthetic database for maintenance and restore.
        try:
            yield make_conninfo(os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"], dbname=database)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))  # fixture-write: remove only this test's synthetic database and connections.

@pytest.fixture
def isolated_theory_dsn():
    with isolated_database() as dsn:
        yield dsn

def apply_through(connection, version):
    for migration in sorted(MIGRATIONS.iterdir(), key=lambda item: item.name):
        if migration.name.endswith(".sql") and int(migration.name[:4]) <= version:
            connection.execute(migration.read_text())  # fixture-write: apply packaged schema to an empty synthetic database.

def seed_source(connection, matter="m-a", document="doc-1"):
    with connection.transaction():
        connection.execute("INSERT INTO review_matter VALUES (%s,'Synthetic') ON CONFLICT DO NOTHING", (matter,))  # fixture-write: seed synthetic matter and source rows within their deferred FK transaction.
        connection.execute("INSERT INTO review_document VALUES (%s,%s,'Synthetic.txt','text/plain',1,'synthetic-v1')", (matter, document))  # fixture-write: seed a synthetic document and its current version.
        connection.execute("INSERT INTO review_source_version(matter_id,document_id,source_version_id,content_digest,byte_size,media_type,storage_key,status) VALUES (%s,%s,'synthetic-v1','synthetic',16,'text/plain','synthetic','ready')", (matter, document))  # fixture-write: register a synthetic source version before its deferred FK is checked.
        connection.execute("INSERT INTO review_page(matter_id,document_id,page_id,page_number,text,source_version_id) VALUES (%s,%s,'page-1',1,'S1: Yes. S2: Yes.','synthetic-v1')", (matter, document))  # fixture-write: seed the synthetic page containing two distinct passages.
        for number in (1, 2):
            connection.execute("INSERT INTO review_chunk(matter_id,document_id,page_id,chunk_id,ordinal,text,source_version_id) VALUES (%s,%s,'page-1',%s,%s,%s,'synthetic-v1')", (matter, document, f"chunk-{number}", number, f"S{number}: Yes."))  # fixture-write: seed synthetic passages with stable source identities.

def seed_theory(connection, matter="m-a", document="doc-1", status="supported", links=1):
    with connection.cursor() as cursor:  # fixture-write: apply missing packaged migrations only when a test body seeds theory rows.
        apply_postgres_migrations(cursor)
    claim = uuid.uuid4()
    connection.execute("INSERT INTO review_case_theory_version(matter_id,version,created_by) VALUES (%s,1,'case-team')", (matter,))  # fixture-write: seed a synthetic version and claim without a repository.
    connection.execute("INSERT INTO review_case_theory_claim(matter_id,version,ordinal,claim_id,text,status) VALUES (%s,1,1,%s,'Synthetic claim',%s)", (matter, claim, status))  # fixture-write: omit needs_review to exercise its default.
    for number in range(1, links + 1):
        connection.execute("INSERT INTO review_case_theory_link(matter_id,claim_id,ordinal,kind,document_id,chunk_id,source_version_id,sha256) SELECT matter_id,%s,%s,'source_passage',document_id,chunk_id,source_version_id,%s FROM review_chunk WHERE matter_id=%s AND document_id=%s AND chunk_id=%s", (claim, number, sha256(f"S{number}: Yes.".encode()).hexdigest(), matter, document, f"chunk-{number}"))  # fixture-write: cite existing synthetic chunks with their source version and text digest.
    connection.commit()  # fixture-write: expose synthetic citations to independent maintenance connections.
    return claim
