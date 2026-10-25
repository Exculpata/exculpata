"""Explicit, transactional replacement of the derived embedding projection."""
import argparse
import json
import os

from .embedding_index import configured_embedding, embedding_index_path, record_embedding_index
from .retrieval_endpoints import configured_endpoint, endpoint_batch
from .review_bench_v2 import RemoteEmbeddingAdapter, _local_model_request


def rebuild(connection):
    if not connection.autocommit:
        raise RuntimeError("Rebuild requires its own committed transaction.")
    path = embedding_index_path()
    if path is None:
        raise RuntimeError("Configure CASE_INTELLIGENCE_EMBEDDING_INDEX_RECORD before rebuilding.")
    config = configured_endpoint("EMBEDDING")
    model, revision = configured_embedding()
    def request(base_url, operation, body, timeout):
        return (endpoint_batch(config, operation, json.loads(body)) if config else
                _local_model_request(base_url, operation, body, timeout))
    adapter = RemoteEmbeddingAdapter(os.getenv("CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL", ""), request=request)
    count = 0
    with connection.transaction(), connection.cursor() as writer:
        writer.execute("LOCK TABLE review_chunk IN ACCESS EXCLUSIVE MODE")
        with connection.cursor(name="embedding_rebuild") as reader:
            reader.execute("SELECT matter_id,document_id,chunk_id,text FROM review_chunk ORDER BY matter_id,document_id,chunk_id")
            while rows := reader.fetchmany(48):
                vectors = adapter.embed([row[3] for row in rows])
                if any(len(vector) != 768 for vector in vectors):
                    raise RuntimeError("The index requires 768-dimensional embeddings.")
                writer.executemany("UPDATE review_chunk SET embedding=%s::vector WHERE matter_id=%s AND document_id=%s AND chunk_id=%s",
                                   [(json.dumps(vector), *row[:3]) for row, vector in zip(rows, vectors)])
                count += len(rows)
        path.unlink(missing_ok=True)
    record_embedding_index(path, model=model, revision=revision, populated=False)
    if count:
        record_embedding_index(path, model=model, revision=revision, populated=True)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-stopped", action="store_true", required=True,
                        help="Confirm the app and other index writers are stopped and a backup is retained")
    parser.parse_args()
    from .workbench import CaseIntelligenceWorkbench
    import psycopg
    try:
        with psycopg.connect(CaseIntelligenceWorkbench._postgres_dsn_from_environment(), autocommit=True) as connection:
            count = rebuild(connection)
        print(f"Rebuilt embeddings for {count} chunks. Restart the app and run retrieval diagnostics.")
    except Exception:
        raise SystemExit("Rebuild failed; keep app writers stopped, check configuration and rerun diagnostics.") from None


if __name__ == "__main__":
    main()
