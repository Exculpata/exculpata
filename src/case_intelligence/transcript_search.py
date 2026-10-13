"""Recoverable transcript search projections, independent of recording versions.

The append-only workspace revisions are authoritative. A source-registry receipt
is written only after PostgreSQL commits the matching text. Missing receipts on
older registries deliberately trigger one reconciliation; no source ID changes.
"""
from .media_evidence import transcript_summary_basis, transcript_units
from .pilot_uploads import is_media_type

PENDING_NOTICE = (
    "Search update pending. Your transcript changes are saved. Retry the search "
    "update; if it remains pending, ask an administrator to check the search service."
)


def search_pending(bench, document, segments):
    if not segments:
        return False
    try:
        if document.state != "ready" or document.parsed_units() != transcript_units(segments):
            return True
    except Exception:
        return True
    return bool(bench._postgres_projection_configured and (
        not bench.postgres_ready
        or document.transcript_index_basis != transcript_summary_basis(segments)
    ))


def project_transcript(bench, matter, document, degraded):
    store = bench.source_store(matter)
    with store.mutation_guard():
        current = store.get(document.document_id)
        if current.version_id != document.version_id:
            raise RuntimeError("media source version changed during projection")
        # A worker can arrive with a snapshot taken before a human correction.
        segments = bench.workspace.transcript_segments(
            matter.matter_id, document.document_id, document.version_id
        )
        try:
            projected = store.install_media_transcript(
                document.document_id, transcript_units(segments), degraded=degraded
            )
        except Exception:
            # Never serve old local units after acknowledging a saved correction.
            # The authoritative revisions also detect this gap after a crash or
            # a registry write failure, even if this failure state cannot save.
            current.state = "failed"
            current.processing_stage = "Search update pending"
            current.message = PENDING_NOTICE
            store._save((document.document_id,))
            raise
        if not bench.postgres_ready or bench.postgres_connection is None:
            return not bench._postgres_projection_configured
        bench._index_document(matter, projected)
        previous = projected.transcript_index_basis
        projected.transcript_index_basis = transcript_summary_basis(segments)
        try:
            store._save((document.document_id,))
        except Exception:
            projected.transcript_index_basis = previous
            raise
        return True


def refresh_projection(bench, matter, document):
    """The edit already committed: report projection failure without losing it."""
    job = bench.workspace.media_job(matter.matter_id, document.document_id, document.version_id)
    segments = bench.workspace.transcript_segments(
        matter.matter_id, document.document_id, document.version_id
    )
    if job is None or not segments or document.state in {"missing", "changed", "queued", "processing"}:
        raise KeyError(document.document_id)
    try:
        return project_transcript(bench, matter, document, bool(job.degraded))
    except Exception:
        if bench.postgres_connection is not None:
            with bench._postgres_lock:
                try:
                    bench.postgres_connection.rollback()
                except Exception:
                    pass
        return False


def reconcile_transcripts(bench, matter, store):
    for document in tuple(store.documents.values()):
        eligible = document.state == "ready" or (
            document.state == "failed" and document.processing_stage == "Search update pending"
        )
        if not eligible or not is_media_type(document.media_type):
            continue
        with store.mutation_guard():
            segments = bench.workspace.transcript_segments(
                matter.matter_id, document.document_id, document.version_id
            )
            if segments and (document.state != "ready" or search_pending(bench, document, segments)):
                refresh_projection(bench, matter, document)


def saved_notice(subject, refreshed):
    return f"{subject} saved and search refreshed" if refreshed else f"{subject} saved; search update pending"
