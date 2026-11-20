"""Explicit, transactional replacement of the derived embedding projection."""
import argparse
from itertools import groupby
import json
from operator import itemgetter
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .embedding_fingerprint import MISMATCH
from .embedding_index import (
    PROJECTION_FORMAT, TRANSFER_FORMAT, configured_embedding, embedding_change_warning, embedding_index_path,
    load_embedding_index, record_embedding_index,
)
from .oidc_network_policy import issuer_authority
from .retrieval_endpoints import (
    configured_endpoint, endpoint_batch, endpoint_fingerprint, require_listed_model, verify_fingerprint, worker_fingerprint,
)
from .review_bench_v2 import RemoteEmbeddingAdapter, _local_model_request


MAX_PLAN_BYTES = 4 * 1024 * 1024
MAX_PLAN_MATTERS = 50_000


class RebuildApprovalError(RuntimeError):
    """An operator-facing error containing no supplied plan or database values."""


def _unique_fields(pairs):
    fields = {}
    for name, value in pairs:
        if name in fields:
            raise RebuildApprovalError("The approval file contains duplicate fields. Generate a fresh --plan.")
        fields[name] = value
    return fields


def _safe_text(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit and value.strip() == value and value.isprintable()


def _origin(url):
    try:
        host, port = issuer_authority(url)
    except (TypeError, ValueError):
        raise RebuildApprovalError("Invalid embedding origin. Generate a fresh --plan.") from None
    authority = f"[{host}]" if ":" in host else host
    return f"https://{authority}" if port == 443 else f"https://{authority}:{port}"


def _validated_plan(plan):
    if (not isinstance(plan, dict) or set(plan) != {"capability", "endpoint", "model", "matter_ids"}
            or plan["capability"] != "embedding" or not _safe_text(plan["endpoint"], 2048)
            or not _safe_text(plan["model"], 200)):
        raise RebuildApprovalError("Invalid embedding approval fields. Generate a fresh --plan.")
    if plan["endpoint"] != _origin(plan["endpoint"]):
        raise RebuildApprovalError("The plan must use the canonical HTTPS embedding origin. Generate a fresh --plan.")
    matters = plan["matter_ids"]
    if (not isinstance(matters, list) or len(matters) > MAX_PLAN_MATTERS
            or any(not _safe_text(matter, 256) for matter in matters) or len(set(matters)) != len(matters)):
        raise RebuildApprovalError("Invalid or duplicate matter IDs in embedding approval. Generate a fresh --plan.")
    if len(json.dumps(plan)) + 1 > MAX_PLAN_BYTES:
        raise RebuildApprovalError("The embedding approval exceeds the size limit.")
    return {**plan, "matter_ids": sorted(matters)}


def load_approved_plan(path):
    try:
        with Path(path).open("rb") as source:
            raw = source.read(MAX_PLAN_BYTES + 1)
        if len(raw) > MAX_PLAN_BYTES:
            raise RebuildApprovalError("The embedding approval file exceeds the size limit.")
        plan = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields)
    except (OSError, ValueError, RecursionError):
        raise RebuildApprovalError("Cannot read the approval file as UTF-8 JSON. Generate a fresh --plan.") from None
    return _validated_plan(plan)


def _plan_for_cursor(cursor, config):
    cursor.execute("SELECT DISTINCT matter_id FROM review_chunk ORDER BY matter_id LIMIT %s",
                   (MAX_PLAN_MATTERS + 1,))
    matters = []
    while batch := cursor.fetchmany(256):
        matters.extend(row[0] for row in batch)
    return _validated_plan({"capability": "embedding", "endpoint": _origin(config.url),
                            "model": config.model, "matter_ids": matters})


def rebuild_plan(connection):
    """Preview an external rebuild without reading chunk text or contacting a model."""
    config = configured_endpoint("EMBEDDING")
    if config is None:
        raise RebuildApprovalError("A preview requires a configured external embedding endpoint.")
    with connection.cursor() as cursor:
        return _plan_for_cursor(cursor, config)


def rebuild(connection, *, approved_plan=None):
    if not connection.autocommit:
        raise RuntimeError("Rebuild requires its own committed transaction.")
    path = embedding_index_path()
    if path is None:
        raise RuntimeError("Configure CASE_INTELLIGENCE_EMBEDDING_INDEX_RECORD before rebuilding.")
    config = configured_endpoint("EMBEDDING")
    if config is None:
        if approved_plan is not None:
            raise RebuildApprovalError("An approved plan requires its configured external embedding endpoint.")
        model, revision = configured_embedding()
    else:
        if approved_plan is None:
            raise RebuildApprovalError("Preview with --plan and supply the reviewed file with --approved-plan.")
        approved_plan = _validated_plan(approved_plan)
        model, revision = config.model, config.identity

    def request(base_url, operation, body, timeout):
        return (endpoint_batch(config, operation, json.loads(body)) if config else
                _local_model_request(base_url, operation, body, timeout))
    adapter = RemoteEmbeddingAdapter(os.getenv("CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL", ""), request=request)
    count = 0
    with connection.transaction(), connection.cursor() as writer:
        writer.execute("LOCK TABLE review_chunk IN ACCESS EXCLUSIVE MODE")
        if config is not None and _plan_for_cursor(writer, config) != approved_plan:
            raise RebuildApprovalError("The endpoint, model, or affected matters differ from the approved plan. "
                                       "Generate and review a fresh --plan.")
        with connection.cursor(name="embedding_rebuild") as reader:
            reader.execute("SELECT matter_id,document_id,chunk_id,text FROM review_chunk ORDER BY matter_id,document_id,chunk_id")
            while rows := reader.fetchmany(48):
                for _, group in groupby(rows, key=itemgetter(0)):
                    batch = list(group)
                    vectors = adapter.embed([row[3] for row in batch])
                    if len(vectors) != len(batch) or any(len(vector) != 768 for vector in vectors):
                        raise RuntimeError("The index requires a 768-dimensional embedding for every chunk.")
                    writer.executemany(
                        "UPDATE review_chunk SET embedding=%s::vector WHERE matter_id=%s AND document_id=%s AND chunk_id=%s",
                        [(json.dumps(vector), *row[:3]) for row, vector in zip(batch, vectors)],
                    )
                    count += len(batch)
        path.unlink(missing_ok=True)
    record_embedding_index(path, model=model, revision=revision, populated=False)
    if count:
        record_embedding_index(path, model=model, revision=revision, populated=True)
    return count


def migrate_index(connection):
    """Compare the recorded worker and destination without reading or sending any passages."""
    path = embedding_index_path()
    config = configured_endpoint("EMBEDDING")
    worker = os.getenv("CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL", "")
    if not (connection.autocommit and path and config):
        raise ValueError("Configure the replacement endpoint and index record before migration.")
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("LOCK TABLE review_chunk IN ACCESS EXCLUSIVE MODE")
        cursor.execute("SELECT EXISTS (SELECT 1 FROM review_chunk WHERE embedding IS NOT NULL), "
                       "EXISTS (SELECT 1 FROM review_chunk WHERE embedding IS NULL)")
        record = load_embedding_index(path)
        if cursor.fetchone() != (True, False) or not record:
            raise ValueError("A verified migration requires a complete populated index with known provenance.")
        if record.get("projection_format") == TRANSFER_FORMAT:
            if embedding_change_warning(record, model=config.model, revision=config.identity):
                raise ValueError("The configured destination differs from the recorded migration.")
            require_listed_model(config)
            verify_fingerprint(config, record)
            return
        if not worker or record.get("projection_format") != PROJECTION_FORMAT:
            raise ValueError("A verified migration requires the recorded worker and known provenance.")
        expected = worker_fingerprint(worker, record)
        require_listed_model(config)
        if endpoint_fingerprint(config) != expected:
            raise ValueError(MISMATCH)
        replacement = {"model": config.model, "revision": config.identity, "projection_format": TRANSFER_FORMAT,
                       "synthetic_fingerprint": expected, "populated": True,
                       "producing_model": record["model"], "producing_revision": record["revision"]}
        with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write((json.dumps(replacement) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true",
                      help="Preview the external embedding origin, model and affected matter IDs as JSON")
    mode.add_argument("--confirm-stopped", action="store_true",
                      help="Confirm the app and other index writers are stopped and a backup is retained")
    parser.add_argument("--approved-plan", metavar="FILE", help="Reviewed JSON preview for this external rebuild")
    parser.add_argument("--verify-migration", action="store_true",
                        help="Compare fixed synthetic worker and endpoint embeddings to retain a complete existing index")
    args = parser.parse_args()
    if args.verify_migration and (not args.confirm_stopped or args.approved_plan):
        parser.error("--verify-migration requires --confirm-stopped and takes no approval file")
    if args.plan and args.approved_plan is not None:
        parser.error("--approved-plan is used with --confirm-stopped")
    from .workbench import CaseIntelligenceWorkbench
    import psycopg
    try:
        approval = load_approved_plan(args.approved_plan) if args.approved_plan is not None else None
        with psycopg.connect(CaseIntelligenceWorkbench._postgres_dsn_from_environment(), autocommit=True) as connection:
            if args.plan:
                print(json.dumps(rebuild_plan(connection)))
                return
            if args.verify_migration:
                migrate_index(connection)
                print("Verified the synthetic embedding fingerprint and retained the existing vectors. Restart the app.")
                return
            count = rebuild(connection, approved_plan=approval)
        print(f"Rebuilt embeddings for {count} chunks. Restart the app and run retrieval diagnostics.")
    except RebuildApprovalError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        if args.verify_migration:
            raise SystemExit("Cannot verify this migration. Check worker provenance and endpoint compatibility, "
                             "or use the approved rebuild. Keep writers stopped until the index is verified.") from None
        if args.plan:
            raise SystemExit("Cannot preview the rebuild. Check the database connection and embedding configuration.") from None
        raise SystemExit("Rebuild failed; keep app writers stopped, check configuration and rerun diagnostics.") from None


if __name__ == "__main__":
    main()
