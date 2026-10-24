"""Label answers whose model has no recorded claim-evaluation pass.

Issue #267's defense-workflow harness, when it is present, owns the decision
through ``qualification_for(model_id)``. Until that module exists, a JSON
registry is the seam: ``apply_harness_result`` records a pass or removes one.
This module does not score answers and does not download a model.
"""
from __future__ import annotations

import json
import re
from importlib.resources import files
from pathlib import Path
from typing import Mapping

UNVALIDATED_MODEL_LABEL = "unvalidated model"
_REGISTRY = files("case_intelligence").joinpath("config", "model_qualifications.json")
_SCHEMA = files("case_intelligence").joinpath("config", "model_qualifications.schema.json")


def qualification_schema(path: Path | None = None) -> dict[str, object]:
    from jsonschema import Draft202012Validator

    schema = json.loads((path or _SCHEMA).read_text(encoding="utf-8"))
    if not isinstance(schema, dict):
        raise ValueError("model qualification schema is invalid")
    Draft202012Validator.check_schema(schema)
    return schema


def load_qualifications(path: Path | None = None) -> dict[str, object]:
    from jsonschema import Draft202012Validator

    document = json.loads((path or _REGISTRY).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("model qualification registry is invalid")
    Draft202012Validator(qualification_schema()).validate(document)
    return document


def qualification_label(model_id: str, *, revision: str = "", registry: Mapping[str, object] | None = None) -> str:
    """Return an empty string when a recorded pass covers this exact model id."""

    model = model_id.strip()
    if not model:
        return ""
    # Only a runtime-attested immutable digest can inherit a qualification.
    # Current BYOM adapters do not attest revisions, so they retain the warning.
    if re.fullmatch(r"sha256:[0-9a-f]{64}", revision) is None:
        return UNVALIDATED_MODEL_LABEL
    harness_label = _harness_label(model, revision)
    if harness_label is not None:
        return harness_label
    document = dict(registry) if registry is not None else load_qualifications()
    rows = document.get("qualifications", [])
    if isinstance(rows, list):
        for row in rows:
            if (
                isinstance(row, dict)
                and row.get("model_id") == model
                and row.get("revision") == revision
                and row.get("passed") is True
            ):
                return ""
    return UNVALIDATED_MODEL_LABEL


def apply_harness_result(
    registry: Mapping[str, object],
    *,
    model_id: str,
    harness: str,
    passed: bool,
    evidence: str = "",
    revision: str = "",
) -> dict[str, object]:
    """Return a new registry. A failed result removes any pass for that model.

    Issue #267 should call this with the scorecard's model id. The evidence
    string is an operator note such as a synthetic scorecard name, not a
    quality claim by itself.
    """

    from jsonschema import Draft202012Validator

    model = model_id.strip()
    suite = harness.strip()
    if not model or len(model) > 200 or not suite or len(suite) > 80:
        raise ValueError("qualification record is invalid")
    if len(evidence) > 200:
        raise ValueError("qualification evidence is too long")
    document = {"schema_version": 1, "qualifications": []}
    rows = registry.get("qualifications", [])
    kept: list[dict[str, object]] = []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("model_id") != model:
                kept.append(dict(row))
    if passed and re.fullmatch(r"sha256:[0-9a-f]{64}", revision) is None:
        raise ValueError("A passing qualification requires an immutable model digest")
    if passed:
        record: dict[str, object] = {"model_id": model, "harness": suite, "passed": True, "revision": revision}
        if evidence:
            record["evidence"] = evidence
        kept.append(record)
    document["qualifications"] = kept
    Draft202012Validator(qualification_schema()).validate(document)
    return document


def _harness_label(model_id: str, revision: str) -> str | None:
    """Use issue #267 when that module is installed. Absence is not an error."""

    try:
        from . import defense_workflow_eval as harness
    except ImportError:
        return None
    record = getattr(harness, "qualification_for", None)
    if not callable(record):
        return None
    label = record(model_id, revision=revision)
    if label is None:
        return None
    if not isinstance(label, str):
        raise ValueError("qualification_for must return a string")
    return label
