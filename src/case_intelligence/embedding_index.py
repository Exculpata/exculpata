"""Remember which embedding model built a populated index."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

DEFAULT_EMBEDDING_MODEL = "ibm-granite/granite-embedding-english-r2"
DEFAULT_EMBEDDING_REVISION = "47ea694b257b703fee9253d75c2b1f2985180498"
WARNING = (
    "The embedding model or revision changed on an index that already has data. "
    "Existing indexes must be rebuilt before meaning search can use the new vectors. "
    "Exculpata does not rebuild them automatically."
)


def embedding_index_path() -> Path | None:
    configured = os.getenv("CASE_INTELLIGENCE_EMBEDDING_INDEX_RECORD", "").strip()
    if configured:
        return Path(configured)
    default = Path("/var/lib/recordbench/state/embedding-index.json")
    if default.parent.is_dir():
        return default
    return None


def load_embedding_index(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def embedding_change_warning(record: Mapping[str, object] | None, *, model: str, revision: str) -> str:
    if not record or record.get("populated") is not True:
        return ""
    if record.get("model") != model or record.get("revision") != revision:
        return WARNING
    return ""


def record_embedding_index(path: Path, *, model: str, revision: str, populated: bool) -> str:
    """Store the model that produced current vectors. A conflict is left in place."""

    current = load_embedding_index(path)
    warning = embedding_change_warning(current, model=model, revision=revision) if populated else ""
    if warning:
        return warning
    document = {"model": model, "revision": revision, "populated": bool(populated)}
    if current == document:
        return ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    return ""


def configured_embedding() -> tuple[str, str]:
    model = os.getenv("CASE_REVIEW_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip() or DEFAULT_EMBEDDING_MODEL
    revision = os.getenv("CASE_REVIEW_EMBEDDING_REVISION", DEFAULT_EMBEDDING_REVISION).strip() or DEFAULT_EMBEDDING_REVISION
    return model, revision


def note_populated_index(*, populated: bool) -> str:
    path = embedding_index_path()
    if path is None:
        return ""
    model, revision = configured_embedding()
    try:
        return record_embedding_index(path, model=model, revision=revision, populated=populated)
    except OSError:
        return embedding_change_warning(load_embedding_index(path), model=model, revision=revision)
