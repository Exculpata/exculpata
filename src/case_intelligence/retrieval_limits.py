"""Shared model-request bounds, separate from canonical extracted evidence."""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

MAX_TEXT_CHARS = 20_000
MAX_BODY_BYTES = 1_048_576
MAX_EMBEDDING_TEXTS = 48
MAX_RERANK_TEXTS = 40
MAX_QUERY_CHARS = 512
INPUT_BLOCKED_STAGE = "Search input needs attention"


class RetrievalInputProblem(RuntimeError):
    """Permanent input rejection; never expose the service response or input."""

    retryable = False

    def __init__(self):
        super().__init__(
            "Search preparation rejected this input. Ask an administrator to check "
            "search-service limits, then remove and import the source again."
        )


def text_fragments(texts: Iterable[str]) -> Iterator[tuple[int, str]]:
    """Preserve ordered text spans without changing source units or citations.

    Whitespace-only spans have no model input; they remain in canonical evidence.
    The transport split is deliberately independent of extraction/storage limits.
    """
    for ordinal, text in enumerate(texts):
        if not isinstance(text, str) or not text.strip():
            raise RetrievalInputProblem()
        for start in range(0, len(text), MAX_TEXT_CHARS):
            fragment = text[start:start + MAX_TEXT_CHARS]
            if fragment.strip():
                yield ordinal, fragment


def request_batches(
    fragments: Iterable[tuple[int, str]], *, maximum: int, query: str | None = None,
) -> Iterator[tuple[list[tuple[int, str]], bytes]]:
    """Account for the exact UTF-8 JSON bytes that will be transmitted."""
    if maximum < 1:
        raise RetrievalInputProblem()
    if query is not None and (not isinstance(query, str) or not query.strip()
                              or len(query) > MAX_QUERY_CHARS):
        raise RetrievalInputProblem()
    try:
        prefix = b'{"texts":[' if query is None else (
            b'{"query":' + json.dumps(query, ensure_ascii=False).encode("utf-8") + b',"texts":[')
    except UnicodeEncodeError:
        raise RetrievalInputProblem() from None
    suffix = b']}'
    batch: list[tuple[int, str]] = []
    encoded: list[bytes] = []
    size = len(prefix) + len(suffix)
    for item in fragments:
        try:
            value = json.dumps(item[1], ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:
            raise RetrievalInputProblem() from None
        if len(prefix) + len(value) + len(suffix) > MAX_BODY_BYTES:
            raise RetrievalInputProblem()
        if batch and (len(batch) >= maximum or size + 1 + len(value) > MAX_BODY_BYTES):
            yield batch, prefix + b','.join(encoded) + suffix
            batch, encoded = [], []
            size = len(prefix) + len(suffix)
        size += len(value) + bool(batch)
        batch.append(item)
        encoded.append(value)
    if batch:
        yield batch, prefix + b','.join(encoded) + suffix
