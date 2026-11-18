"""Remember which embedding model built a populated index."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from .legacy_names import name_value

DEFAULT_EMBEDDING_MODEL = "ibm-granite/granite-embedding-english-r2"
DEFAULT_EMBEDDING_REVISION = "47ea694b257b703fee9253d75c2b1f2985180498"
PROJECTION_FORMAT = "learned-or-null/v1"
MISSING_EMBEDDINGS = "Some passages have no embeddings. Rebuild the embedding index to enable meaning search."
WARNING = (
    "The embedding model or revision changed on an index that already has data. "
    "Existing indexes must be rebuilt before meaning search can use the new vectors. "
    "Exculpata does not rebuild them automatically."
)
UNKNOWN_WARNING = (
    "The embedding identity of this index is unknown. Any existing vectors must be rebuilt "
    "and their producing model and revision recorded before meaning search can safely use them. "
    "Exculpata does not infer provenance from the current configuration or rebuild automatically."
)
FORMAT_WARNING = (
    "This embedding index uses an earlier or unknown projection format. "
    "Rebuild the index before using meaning search."
)


def embedding_index_path() -> Path | None:
    configured = os.getenv("CASE_INTELLIGENCE_EMBEDDING_INDEX_RECORD", "").strip()
    if configured:
        return Path(configured)
    default = Path(name_value("embedding_index_record"))
    if default.parent.is_dir():
        return default
    return None


def load_embedding_index(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def embedding_change_warning(
    record: Mapping[str, object] | None, *, model: str, revision: str, allow_empty: bool = False,
) -> str:
    if (not record or type(record.get("populated")) is not bool
            or (not record["populated"] and not allow_empty)
            or not record.get("model") or not record.get("revision")):
        return UNKNOWN_WARNING
    if record.get("model") != model or record.get("revision") != revision:
        return WARNING if record["populated"] else UNKNOWN_WARNING
    if record.get("projection_format") != PROJECTION_FORMAT:
        return FORMAT_WARNING
    return ""


def record_embedding_index(path: Path, *, model: str, revision: str, populated: bool) -> str:
    """Record an observed empty index; preserve known or unknown populated provenance."""

    current = load_embedding_index(path)
    if populated:
        warning = embedding_change_warning(current, model=model, revision=revision, allow_empty=True)
        if warning:
            return warning
    document = {"model": model, "revision": revision, "populated": bool(populated),
                "projection_format": PROJECTION_FORMAT}
    if current == document:
        return ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    return ""


def configured_embedding() -> tuple[str, str]:
    from .retrieval_endpoints import configured_endpoint
    endpoint = configured_endpoint("EMBEDDING")
    if endpoint is not None:
        return endpoint.model, endpoint.identity
    model = os.getenv("CASE_REVIEW_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip() or DEFAULT_EMBEDDING_MODEL
    revision = os.getenv("CASE_REVIEW_EMBEDDING_REVISION", DEFAULT_EMBEDDING_REVISION).strip() or DEFAULT_EMBEDDING_REVISION
    return model, revision


def note_populated_index(*, populated: bool) -> str:
    path = embedding_index_path()
    if path is None:
        return UNKNOWN_WARNING if populated else ""
    model, revision = configured_embedding()
    try:
        return record_embedding_index(path, model=model, revision=revision, populated=populated)
    except OSError:
        return embedding_change_warning(load_embedding_index(path), model=model, revision=revision) if populated else ""
