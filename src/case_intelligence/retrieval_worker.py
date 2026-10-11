"""Localhost-only compact embedding and reranking worker for the V2 demo."""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import os
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from .compat_env import env

EMBEDDING_MODEL = os.getenv(
    "CASE_REVIEW_EMBEDDING_MODEL", "ibm-granite/granite-embedding-english-r2"
)
EMBEDDING_REVISION = os.getenv(
    "CASE_REVIEW_EMBEDDING_REVISION", "47ea694b257b703fee9253d75c2b1f2985180498"
)
RERANKER_MODEL = os.getenv(
    "CASE_REVIEW_RERANKER_MODEL", "Alibaba-NLP/gte-reranker-modernbert-base"
)
RERANKER_REVISION = os.getenv(
    "CASE_REVIEW_RERANKER_REVISION", "f7481e6055501a30fb19d090657df9ec1f79ab2c"
)
DEVICE = os.getenv("CASE_REVIEW_MODEL_DEVICE", "cuda")
MAX_BODY_BYTES = 1_048_576


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Reject oversized fixed or streamed bodies before JSON materialization."""

    def __init__(self, app, max_body_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", ())}
        try:
            content_length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            content_length = 0
        response = JSONResponse({"detail": "request body too large"}, status_code=413)
        if content_length > self.max_body_bytes:
            await response(scope, receive, send)
            return
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await response(scope, receive, send)


class EmbedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    texts: list[str]


class RerankPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    texts: list[str]

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Compose readiness requires both offline models before the app may start.
    # Loading only on the first application request creates a startup deadlock.
    _embedding_model()
    _reranker_model()
    yield


app = FastAPI(title="Case Review Retrieval Worker", lifespan=_lifespan)
app.add_middleware(RequestBodyLimitMiddleware)
_embedding: Any = None
_reranker: Any = None


def _validate_texts(values: object, *, maximum: int) -> list[str]:
    if not isinstance(values, list) or not values or len(values) > maximum:
        raise HTTPException(422, "invalid text batch")
    if any(not isinstance(value, str) or not value.strip() or len(value) > 20_000 for value in values):
        raise HTTPException(422, "invalid text batch")
    return values


def _embedding_model():
    global _embedding
    if _embedding is None:
        from sentence_transformers import SentenceTransformer

        _embedding = SentenceTransformer(
            EMBEDDING_MODEL,
            revision=EMBEDDING_REVISION,
            device=DEVICE,
            local_files_only=True,
            trust_remote_code=False,
        )
    return _embedding


def _reranker_model():
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder

        _reranker = CrossEncoder(
            RERANKER_MODEL,
            revision=RERANKER_REVISION,
            device=DEVICE,
            local_files_only=True,
            trust_remote_code=False,
        )
    return _reranker


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "embedding_loaded": _embedding is not None,
        "reranker_loaded": _reranker is not None,
    }


@app.post("/embed")
def embed(payload: EmbedPayload) -> dict[str, object]:
    texts = _validate_texts(payload.texts, maximum=48)
    vectors = _embedding_model().encode(texts, normalize_embeddings=True)
    return {"vectors": [[float(value) for value in row] for row in vectors]}


@app.post("/rerank")
def rerank(payload: RerankPayload) -> dict[str, object]:
    query = payload.query
    if not isinstance(query, str) or not query.strip() or len(query) > 512:
        raise HTTPException(422, "invalid query")
    texts = _validate_texts(payload.texts, maximum=40)
    scores = _reranker_model().predict([(query, text) for text in texts])
    return {"scores": [float(score) for score in scores]}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local retrieval-model worker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    if args.host != "127.0.0.1" and not (
        args.host == "0.0.0.0"
        and env("RECORDBENCH_ALLOW_CONTAINER_BIND", "") == "1"
    ):
        parser.error(
            "non-loopback binding is allowed only in the isolated container profile"
        )
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
