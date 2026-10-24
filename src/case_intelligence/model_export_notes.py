"""Copy model qualification labels into portable exports."""
from __future__ import annotations

from typing import Mapping


def model_labels(payload: object) -> tuple[str, str]:
    from .work_product_exports import _plain

    if not isinstance(payload, Mapping):
        return "", ""
    return _plain(payload.get("model_notice")), _plain(payload.get("schema_enforcement"))


def append_model_notes(blocks: list, payload: object, block) -> None:
    notice, schema = model_labels(payload)
    if notice:
        blocks.append(block(notice, "note"))
    if schema:
        blocks.append(block(f"Schema enforcement: {schema}.", "note"))


def apply_model_fields(target: dict[str, object], payload: object) -> None:
    notice, schema = model_labels(payload)
    if notice:
        target["model_notice"] = notice
    if schema:
        target["schema_enforcement"] = schema
