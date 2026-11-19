"""Select available retrieval providers and bind external calls to their matter."""
from functools import cache
import os

from .embedding_index import MISSING_EMBEDDINGS, note_populated_index
from .retrieval_endpoints import INVALID_CONFIGURATION
from .review_bench_v2 import RemoteEmbeddingAdapter, RemoteRerankerAdapter


class WorkbenchRetrieval:
    def _configure_retrieval(self, embedding, reranker, enabled: bool | None) -> None:
        from .retrieval_endpoints import configured_endpoint, probe_endpoint

        worker_url = os.getenv("CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL", "").strip()

        @cache
        def worker_ready() -> bool:
            return bool(worker_url and self._retrieval_worker_ready(worker_url))

        def provider(role, supplied, adapter):
            if enabled is False:
                return None, "disabled", False
            if supplied is not None:
                return supplied, "provided by application", True
            try:
                endpoint = configured_endpoint(role)
            except ValueError as exc:
                return None, str(exc), False
            if endpoint is not None:
                result = probe_endpoint(endpoint)
                if result == "ok":
                    return adapter(""), "endpoint verified", True
                return None, result, True
            if worker_ready():
                return adapter(worker_url), "bundled worker ready", True
            return None, "bundled worker unavailable" if worker_url else "not configured", False

        self.embedding, self.embedding_status, self._embedding_selected = provider(
            "EMBEDDING", embedding, RemoteEmbeddingAdapter,
        )
        self.reranker, self.rerank_status, _ = provider("RERANK", reranker, RemoteRerankerAdapter)

    def _retrieval_endpoint_failed(self, role: str, reason: str) -> None:
        with self._postgres_lock:
            if role == "EMBEDDING":
                self.embedding, self.embedding_status = None, reason
            elif role == "RERANK":
                self.reranker, self.rerank_status = None, reason
            else:
                raise ValueError("Unknown retrieval role")

    def _embedding_blocker(self) -> str:
        from .retrieval_endpoints import require_index_identity

        if self.embedding_index_warning:
            return self.embedding_index_warning
        try:
            require_index_identity(require_record=True)
        except ValueError:
            self.embedding_index_warning = INVALID_CONFIGURATION
        except RuntimeError as exc:
            self.embedding_index_warning = str(exc)
        return self.embedding_index_warning

    def _active_embedding(self):
        if self.learned_retrieval and self.embedding is not None and not self._embedding_blocker():
            return self.embedding
        return None

    def _matter_retrieval_provider(self, provider, matter_id):
        if isinstance(provider, (RemoteEmbeddingAdapter, RemoteRerankerAdapter)):
            return provider.for_matter(matter_id, self.workspace.model_grant_allows)
        return provider

    def _matter_embedding(self, matter_id):
        from .retrieval_endpoints import configured_endpoint, require_index_identity

        provider = self._active_embedding()
        if (provider is None and self.learned_retrieval and isinstance(self.embedding, RemoteEmbeddingAdapter)
                and self.embedding_index_warning == MISSING_EMBEDDINGS):
            # A matter can have a complete index even when another matter withheld approval.
            try:
                if configured_endpoint("EMBEDDING") is None:
                    return None
                require_index_identity(require_record=True)
                with self._postgres_lock, self.postgres_connection.transaction(), self.postgres_connection.cursor() as cursor:
                    cursor.execute("SELECT EXISTS (SELECT 1 FROM review_chunk WHERE matter_id=%s AND embedding IS NULL)",
                                   (matter_id,))
                    complete = cursor.fetchone()[0] is False
            except Exception:
                return None
            if complete:
                provider = self.embedding
        return self._matter_retrieval_provider(provider, matter_id)

    def _record_embedding_projection(self, has_vectors):
        if has_vectors:
            try:
                warning = note_populated_index(populated=True)
            except ValueError:
                warning = INVALID_CONFIGURATION
            if warning or self.embedding_index_warning != MISSING_EMBEDDINGS:
                self.embedding_index_warning = warning
        elif not self.embedding_index_warning:
            self.embedding_index_warning = MISSING_EMBEDDINGS
