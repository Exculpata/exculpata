"""Content-free admission preview for staff-selected loose files.

The preview evaluates metadata only. Byte-derived facts remain deliberately
unknown until the retained upload/finalization path revalidates the source.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from .managed_storage import format_bytes
from .pilot_uploads import (
    EXTENDED_SOURCE_SUFFIXES,
    PilotStore,
    UploadProblem,
    canonical_media_type,
    maximum_file_bytes,
)


PREFLIGHT_STATES = (
    "valid",
    "needs_attention",
    "unsupported",
    "duplicate_candidate",
    "over_limit",
    "failed",
)
_MEDIA_TYPE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,79}/[a-z0-9][a-z0-9!#$&^_.+-]{0,79}$"
)


def _scan_projection(*, required: bool, ready: bool) -> dict[str, object]:
    if not required:
        return {
            "required": False,
            "capability": "not_required",
            "result": "not_required",
        }
    return {
        "required": True,
        "capability": "ready" if ready else "unavailable",
        "result": "not_run",
    }


def _base_item(index: int) -> dict[str, Any]:
    return {
        "index": index,
        "display_name": f"Selected file {index + 1}",
        "path_safety_validated": False,
        "state": "failed",
        "eligible": False,
        "supplied_type": None,
        "expected_type": None,
        "detected_type": None,
        "size": None,
        "readability": "pending_upload",
        "source_version": "pending_upload",
        "scan": {
            "required": False,
            "capability": "not_required",
            "result": "not_run",
        },
        "duplicate": "not_evaluated",
        "message": "This selection entry could not be read. Choose the file again.",
    }


def evaluate_loose_file_preflight(
    selected: Sequence[object],
    *,
    document_limit: int,
    media_limit: int,
    malware_scan_mode: str,
    scanner_ready: bool,
    duplicate_token_scope: str | None = None,
) -> dict[str, object]:
    """Return one stable, content-free admission result per input descriptor."""

    items: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    counts = {state: 0 for state in PREFLIGHT_STATES}
    eligible_indexes: list[int] = []

    for index, raw_item in enumerate(selected):
        result = _base_item(index)
        if not isinstance(raw_item, Mapping):
            counts["failed"] += 1
            items.append(result)
            continue

        supplied = raw_item.get("media_type")
        if isinstance(supplied, str):
            try:
                supplied_length = len(supplied.encode("utf-8"))
            except UnicodeEncodeError:
                supplied_length = 161
            normalized_supplied = supplied.strip().casefold()
            if supplied_length <= 160 and _MEDIA_TYPE.fullmatch(normalized_supplied):
                result["supplied_type"] = normalized_supplied

        raw_size = raw_item.get("size")
        if not isinstance(raw_size, bool) and isinstance(raw_size, int):
            result["size"] = raw_size

        raw_path = raw_item.get("relative_path") or raw_item.get("name")
        if not isinstance(raw_path, str):
            counts["failed"] += 1
            items.append(result)
            continue
        try:
            relative_path, filename, suffix, path_key = (
                PilotStore.validate_relative_upload_path(raw_path)
            )
        except (UploadProblem, UnicodeError) as exc:
            if str(exc).startswith("Supported sources are"):
                # This error is reached only after the shared path/name safety
                # validator has accepted every component.
                result["path_safety_validated"] = True
                result["display_name"] = PurePosixPath(
                    unicodedata.normalize("NFC", raw_path)
                ).name
                result["state"] = "unsupported"
                result["message"] = (
                    "This file type is not supported. Choose a listed source format."
                )
                counts["unsupported"] += 1
            else:
                result["message"] = (
                    "This selection entry has an unsafe path or filename. Choose it again."
                )
                counts["failed"] += 1
            items.append(result)
            continue

        result["path_safety_validated"] = True
        del relative_path
        result["display_name"] = filename
        expected_type = canonical_media_type(suffix)
        result["expected_type"] = expected_type
        scan_required = malware_scan_mode == "all" or (
            malware_scan_mode == "extended" and suffix in EXTENDED_SOURCE_SUFFIXES
        )
        result["scan"] = _scan_projection(required=scan_required, ready=scanner_ready)
        if isinstance(raw_size, bool) or not isinstance(raw_size, int):
            result["message"] = "This file does not have a valid size. Choose it again."
            counts["failed"] += 1
            items.append(result)
            continue
        result["size"] = raw_size
        if raw_size <= 0:
            result["message"] = "This file is empty. Choose a non-empty source."
            counts["failed"] += 1
            items.append(result)
            continue

        if duplicate_token_scope is not None:
            result["duplicate_token"] = hashlib.sha256(
                f"{duplicate_token_scope}\0{path_key}".encode("utf-8")
            ).hexdigest()

        maximum = maximum_file_bytes(
            suffix,
            document_limit=document_limit,
            media_limit=media_limit,
        )
        if raw_size > maximum:
            result["state"] = "over_limit"
            result["message"] = (
                f"This file is larger than {format_bytes(maximum)}. Choose a smaller file."
            )
            counts["over_limit"] += 1
            items.append(result)
            continue

        if scan_required and not scanner_ready:
            result["state"] = "needs_attention"
            result["message"] = (
                "A security scan is required but has not run because scanning is "
                "unavailable. Retry when security checks are ready."
            )
            counts["needs_attention"] += 1
            items.append(result)
            continue

        if path_key in seen_paths:
            result["state"] = "duplicate_candidate"
            result["duplicate"] = "selection_collision"
            result["message"] = (
                "This relative path appears more than once in the selection. "
                "Keep one copy or rename it before upload."
            )
            counts["duplicate_candidate"] += 1
            items.append(result)
            continue
        seen_paths.add(path_key)

        result["state"] = "valid"
        result["eligible"] = True
        result["message"] = (
            "Ready to upload. File contents will be checked after transfer."
        )
        counts["valid"] += 1
        eligible_indexes.append(index)
        items.append(result)

    eligible_count = len(eligible_indexes)
    status = (
        "blocked"
        if eligible_count == 0
        else "ready"
        if eligible_count == len(items)
        else "partial"
    )
    return {
        "status": status,
        "selected_count": len(items),
        "counts": counts,
        "eligible_indexes": eligible_indexes,
        "items": items,
    }
