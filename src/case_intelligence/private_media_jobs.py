"""Identity checks shared by dispatch, retry and cleanup of private media jobs."""
from __future__ import annotations

import re
from typing import Mapping

from .media_processor_errors import MediaProcessorError
from .store_records import MediaJobRecord


def matched_external_id(view: Mapping[str, object], job: MediaJobRecord) -> str:
    if view.get("source_sha256") != job.source_sha256 or view.get("size_bytes") != job.byte_size:
        raise MediaProcessorError("The transcription service did not preserve the submitted source identity.")
    external_id = str(view.get("job_id") or view.get("id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", external_id):
        raise MediaProcessorError("The transcription service returned an invalid job identity.")
    return external_id


def processor_owner(media_job_id: str) -> str:
    if not re.fullmatch(r"media-job-[0-9a-f]{32}", media_job_id or ""):
        raise ValueError("invalid media job")
    return "ci-" + media_job_id
