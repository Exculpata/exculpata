"""Validate and project machine segments into the matter's timestamped transcript."""
from __future__ import annotations

import hashlib
from typing import Mapping, Sequence

from .media_processor_errors import MediaProcessorError
from .pilot_uploads import PilotUnit
from .store_records import TranscriptSegmentRecord


def normalize_transcript_payload(
    payload: Mapping[str, object], *, duration_ms: int
) -> tuple[Mapping[str, object], ...]:
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        raise MediaProcessorError("The transcription service returned no transcript.")
    normalized: list[Mapping[str, object]] = []
    for item in raw_segments:
        if not isinstance(item, dict):
            raise MediaProcessorError("The transcription service returned invalid segments.")
        speaker = item.get("speaker")
        if not isinstance(speaker, dict):
            speaker = {}
        try:
            start_ms = round(float(item.get("start")) * 1000)
            end_ms = round(float(item.get("end")) * 1000)
        except (TypeError, ValueError) as exc:
            raise MediaProcessorError(
                "The transcription service returned invalid timestamps."
            ) from exc
        if start_ms < 0 or end_ms <= start_ms or end_ms > duration_ms + 2_000:
            raise MediaProcessorError(
                "The transcription service returned timestamps outside the recording."
            )
        text = str(item.get("model_text") or item.get("text") or "").strip()
        if not text:
            continue
        normalized.append(
            {
                "external_segment_id": str(
                    item.get("segment_id") or item.get("id") or ""
                ),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "speaker_cluster": str(speaker.get("cluster_id") or "UNKNOWN"),
                "speaker_display_name": str(
                    speaker.get("display_name") or speaker.get("cluster_id") or "UNKNOWN"
                ),
                "model_text": text,
                "translated_text": item.get("translated_text"),
                "confidence": item.get("confidence"),
                "low_confidence": bool(item.get("low_confidence")),
                "overlap": bool(item.get("overlap")),
            }
        )
    if not normalized:
        raise MediaProcessorError("The recording produced no searchable transcript.")
    return tuple(normalized)


def transcript_speaker_labels(
    segments: Sequence[TranscriptSegmentRecord],
) -> dict[str, str]:
    """Assign one stable staff-facing label to every transcript speaker cluster."""

    confirmed_labels: dict[str, str] = {}
    anonymous_clusters: set[str] = set()
    for segment in segments:
        display_name = " ".join(segment.speaker_display_name.split()).strip()
        if segment.speaker_identity_state == "confirmed" and display_name:
            confirmed_labels[segment.speaker_cluster] = display_name
        else:
            anonymous_clusters.add(segment.speaker_cluster)
    anonymous_clusters.difference_update(confirmed_labels)
    anonymous_labels = {
        cluster: f"Speaker {ordinal}"
        for ordinal, cluster in enumerate(
            sorted(anonymous_clusters),
            1,
        )
    }
    return {**anonymous_labels, **confirmed_labels}


def transcript_units(
    segments: Sequence[TranscriptSegmentRecord],
) -> tuple[PilotUnit, ...]:
    units: list[PilotUnit] = []
    speaker_labels = transcript_speaker_labels(segments)
    for ordinal, segment in enumerate(segments, 1):
        speaker = speaker_labels[segment.speaker_cluster]
        text = f"{speaker}: {segment.current_text}"
        units.append(
            PilotUnit(
                ordinal,
                text,
                line_start=segment.start_ms,
                line_end=segment.end_ms,
                excerpt_digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                speaker_cluster=segment.speaker_cluster,
            )
        )
    return tuple(units)
