"""Deterministic, transient delivery exports for transcription v2 results.

The caller must provide a per-job output directory.  This module never chooses a
repository, user-home, or long-term storage location and never copies the source
media.  The durable worker owns retention and cleanup after delivery.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from pathlib import Path
from typing import Any


_FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_SAFE_BASE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_WINDOWS_RESERVED_NAMES = {
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_XML_NS = "http://www.w3.org/XML/1998/namespace"
_CORE_PROPERTIES_NS = (
    "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
)
_DUBLIN_CORE_NS = "http://purl.org/dc/elements/1.1/"

ET.register_namespace("w", _WORD_NS)
ET.register_namespace("r", _REL_NS)
ET.register_namespace("cp", _CORE_PROPERTIES_NS)
ET.register_namespace("dc", _DUBLIN_CORE_NS)


@dataclass(frozen=True, slots=True)
class ExportBundle:
    """Paths for one caller-owned transient delivery package."""

    output_dir: Path
    files: Mapping[str, Path]
    manifest_path: Path
    zip_path: Path | None
    zip_sha256: str | None

    @property
    def package_path(self) -> Path | None:
        return self.zip_path

    @property
    def archive_path(self) -> Path | None:
        return self.zip_path

    @property
    def archive_sha256(self) -> str | None:
        return self.zip_sha256


def render_json(result: Any, *, include_raw: bool = True) -> str:
    """Render the complete canonical result with stable key ordering."""

    payload = _coerce_result(result)
    if not include_raw:
        payload = _without_raw_fields(payload)
    return _json_text(payload)


def render_txt(result: Any) -> str:
    """Render a review-friendly source transcript and separate translation."""

    payload = _coerce_result(result)
    lines: list[str] = ["MACHINE-GENERATED TRANSCRIPT", ""]
    if _is_mock(payload):
        lines.extend(
            [
                "*** SYNTHETIC MOCK OUTPUT — NOT A TRANSCRIPT — NOT FOR EVIDENTIARY USE ***",
                "",
            ]
        )
    lines.extend(
        [
            f"Job ID: {payload.get('job_id', '')}",
            f"Status: {payload.get('status', 'unknown')}",
            f"Source file: {_source(payload).get('file_name', '')}",
            f"Source SHA-256: {_source(payload).get('sha256', '')}",
            f"Source language: {payload.get('source_language') or 'unknown'}",
            f"Profile: {_profile(payload).get('name', '')}",
            "",
            "SOURCE-LANGUAGE TRANSCRIPT",
            "==========================",
        ]
    )
    source_segments = _segments(payload)
    if source_segments:
        lines.extend(_txt_segment(segment) for segment in source_segments)
    else:
        lines.append("[No source speech segments available]")

    translation = payload.get("translation")
    if isinstance(translation, Mapping):
        target = str(translation.get("target_language") or "unknown").upper()
        lines.extend(["", f"TRANSLATION ({target})", "=" * (14 + len(target))])
        lines.append(f"Translation status: {translation.get('status', 'unknown')}")
        lines.append(
            "This translation is a separate artifact; the source transcript above is unchanged."
        )
        translated_segments = _segments(payload, translation=True)
        if translated_segments:
            lines.extend(_txt_segment(segment, show_speaker=False) for segment in translated_segments)
        elif translation.get("text"):
            lines.append(str(translation["text"]).strip())
        else:
            error = translation.get("error")
            if isinstance(error, Mapping):
                lines.append(
                    f"[Translation unavailable: {error.get('type', 'error')}: "
                    f"{error.get('message', '')}]"
                )
            else:
                lines.append("[Translation unavailable]")

    warnings = payload.get("warnings", [])
    errors = payload.get("errors", [])
    if warnings:
        lines.extend(["", "WARNINGS", "========"])
        for warning in warnings:
            if isinstance(warning, Mapping):
                lines.append(
                    f"- [{warning.get('stage', 'pipeline')}/"
                    f"{warning.get('code', 'warning')}] {warning.get('message', '')}"
                )
            else:
                lines.append(f"- {warning}")
    if errors:
        lines.extend(["", "ERRORS", "======"])
        for error in errors:
            if isinstance(error, Mapping):
                lines.append(
                    f"- [{error.get('stage', 'pipeline')}] "
                    f"{error.get('type', 'Error')}: {error.get('message', '')}"
                )
            else:
                lines.append(f"- {error}")
    return "\n".join(lines).rstrip() + "\n"


def render_srt(result: Any, *, translation: bool = False) -> str:
    payload = _coerce_result(result)
    cues: list[str] = []
    for cue_number, segment in enumerate(
        _timed_segments(_segments(payload, translation=translation)), start=1
    ):
        cues.extend(
            [
                str(cue_number),
                f"{format_srt_timestamp(segment['start'])} --> "
                f"{format_srt_timestamp(segment['end'])}",
                _cue_text(segment, show_speaker=not translation),
                "",
            ]
        )
    return "\n".join(cues)


def render_vtt(result: Any, *, translation: bool = False) -> str:
    payload = _coerce_result(result)
    lines = ["WEBVTT", ""]
    for cue_number, segment in enumerate(
        _timed_segments(_segments(payload, translation=translation)), start=1
    ):
        lines.extend(
            [
                str(cue_number),
                f"{format_vtt_timestamp(segment['start'])} --> "
                f"{format_vtt_timestamp(segment['end'])}",
                _cue_text(segment, show_speaker=not translation),
                "",
            ]
        )
    return "\n".join(lines)


def render_csv(result: Any) -> str:
    """Render source-language segments; raw and translation remain separate JSON."""

    payload = _coerce_result(result)
    output = io.StringIO(newline="")
    fieldnames = (
        "segment_id",
        "start_seconds",
        "end_seconds",
        "speaker",
        "confidence",
        "overlap",
        "overlap_speakers",
        "word_count",
        "text",
    )
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for segment in _segments(payload):
        writer.writerow(
            {
                "segment_id": segment.get("id", ""),
                "start_seconds": _number_text(segment.get("start")),
                "end_seconds": _number_text(segment.get("end")),
                "speaker": _csv_text(segment.get("speaker") or ""),
                "confidence": _number_text(segment.get("confidence")),
                "overlap": "true" if segment.get("overlap") else "false",
                "overlap_speakers": _csv_text(
                    "|".join(
                        str(value) for value in segment.get("overlap_speakers", [])
                    )
                ),
                "word_count": len(segment.get("words", [])),
                "text": _csv_text(segment.get("text", "")),
            }
        )
    return output.getvalue()


def render_translation_json(result: Any) -> str:
    payload = _coerce_result(result)
    translation = payload.get("translation")
    if not isinstance(translation, Mapping):
        raise ValueError("result has no translation artifact")
    artifact = _without_raw_fields({
        "schema_version": payload.get("schema_version"),
        "job_id": payload.get("job_id"),
        "source_sha256": _source(payload).get("sha256"),
        "translation": translation,
        "source_transcript_mutated": False,
    })
    return _json_text(artifact)


def render_translation_txt(result: Any) -> str:
    payload = _coerce_result(result)
    translation = payload.get("translation")
    if not isinstance(translation, Mapping):
        raise ValueError("result has no translation artifact")
    lines = [
        "MACHINE-GENERATED TRANSLATION",
        "",
        f"Job ID: {payload.get('job_id', '')}",
        f"Source SHA-256: {_source(payload).get('sha256', '')}",
        f"Source language: {translation.get('source_language') or payload.get('source_language') or 'unknown'}",
        f"Target language: {translation.get('target_language') or 'unknown'}",
        f"Status: {translation.get('status', 'unknown')}",
        "Source transcript mutated: no",
        "",
    ]
    if _is_mock(payload):
        lines.extend(
            [
                "*** SYNTHETIC MOCK OUTPUT — NOT A TRANSLATION — NOT FOR EVIDENTIARY USE ***",
                "",
            ]
        )
    segments = _segments(payload, translation=True)
    if segments:
        lines.extend(_txt_segment(segment, show_speaker=False) for segment in segments)
    elif translation.get("text"):
        lines.append(str(translation["text"]).strip())
    else:
        lines.append("[Translation unavailable]")
    return "\n".join(lines).rstrip() + "\n"


def render_docx(result: Any) -> bytes:
    """Render a readable Word transcript with explicit machine-output cues.

    The DOCX is generated directly as deterministic Office Open XML.  It
    includes the canonical source-language transcript and, when requested, a
    separately labelled translation section.  No source media or provider raw
    objects are embedded in the document.
    """

    return _render_docx_package(_coerce_result(result), translation_only=False)


def render_translation_docx(result: Any) -> bytes:
    """Render a translation-only Word document without mutating the source."""

    payload = _coerce_result(result)
    if not isinstance(payload.get("translation"), Mapping):
        raise ValueError("result has no translation artifact")
    return _render_docx_package(payload, translation_only=True)


def build_manifest(
    result: Any,
    *,
    artifact_files: Mapping[str, Mapping[str, Any]] | None = None,
    archive_name: str | None = None,
) -> dict[str, Any]:
    """Build a delivery manifest without transcript text or source media."""

    payload = _coerce_result(result)
    translation = payload.get("translation")
    diarization = _diarization(payload)
    provenance = payload.get("provenance", {})
    return {
        "manifest_version": "transcription-delivery-manifest-v1",
        "schema_version": payload.get("schema_version"),
        "job_id": payload.get("job_id"),
        "processing_status": payload.get("status"),
        "evidentiary_output_allowed": bool(
            isinstance(provenance, Mapping)
            and provenance.get("evidentiary_output_allowed", False)
        ),
        "source": {
            "file_name": _source(payload).get("file_name"),
            "sha256": _source(payload).get("sha256"),
            "bytes": _source(payload).get("bytes"),
            "language": payload.get("source_language"),
            "language_confidence": payload.get("source_language_confidence"),
            "media_included_in_delivery": False,
        },
        "profile": _profile(payload),
        "diarization": {
            "requested": _diarization_requested(payload),
            "status": diarization.get("status"),
            "model": diarization.get("model"),
            "speaker_count": diarization.get("speaker_count"),
            "speakers": diarization.get("speakers", []),
            "overlap_region_count": len(
                diarization.get("overlap_regions", [])
            ),
        },
        "translation": (
            {
                "requested": True,
                "status": translation.get("status"),
                "source_language": translation.get("source_language"),
                "target_language": translation.get("target_language"),
                "model": translation.get("model"),
                "separate_artifact": True,
            }
            if isinstance(translation, Mapping)
            else {"requested": False, "separate_artifact": True}
        ),
        "stages": payload.get("stages", []),
        "warnings": payload.get("warnings", []),
        "errors": payload.get("errors", []),
        "provenance": provenance,
        "delivery": {
            "scope": "transient-per-job-output",
            "purpose": "ephemeral user delivery package",
            "archive_file": archive_name,
            "source_media_copied": False,
            "cleanup_required": True,
            "cleanup_owner": "caller-job-runner",
            "long_term_storage_implied": False,
        },
        "artifacts": dict(sorted((artifact_files or {}).items())),
    }


def write_export_bundle(
    result: Any,
    output_dir: str | Path,
    *,
    base_name: str = "transcript",
    create_zip: bool = True,
) -> ExportBundle:
    """Write deterministic artifacts inside one explicit transient directory.

    The function does not delete the directory because the job runner controls
    delivery completion and cleanup.  No source media is copied into the bundle.
    """

    payload = _coerce_result(result)
    target_dir = _prepare_output_dir(output_dir)
    stem = _safe_base_name(base_name)
    archive_name = f"{stem}.delivery.zip" if create_zip else None

    rendered: dict[str, str | bytes] = {
        # Provider-native raw objects remain in the short-lived machine result
        # for regeneration; delivery JSON contains the normalized auditable
        # schema only, which is substantially smaller for long recordings.
        f"{stem}.docx": render_docx(payload),
        f"{stem}.json": render_json(payload, include_raw=False),
        f"{stem}.txt": render_txt(payload),
        f"{stem}.srt": render_srt(payload),
        f"{stem}.vtt": render_vtt(payload),
        f"{stem}.csv": render_csv(payload),
    }
    translation = payload.get("translation")
    if isinstance(translation, Mapping):
        target_language = _safe_language(translation.get("target_language"))
        prefix = f"{stem}.translation.{target_language}"
        rendered[f"{prefix}.json"] = render_translation_json(payload)
        rendered[f"{prefix}.docx"] = render_translation_docx(payload)
        rendered[f"{prefix}.txt"] = render_translation_txt(payload)
        rendered[f"{prefix}.srt"] = render_srt(payload, translation=True)
        rendered[f"{prefix}.vtt"] = render_vtt(payload, translation=True)

    file_paths: dict[str, Path] = {}
    for file_name in sorted(rendered):
        path = target_dir / file_name
        content = rendered[file_name]
        _atomic_write(path, content if isinstance(content, bytes) else content.encode("utf-8"))
        file_paths[file_name] = path

    artifact_metadata = {
        file_name: {
            "sha256": _sha256_path(path),
            "bytes": path.stat().st_size,
            "media_type": _media_type(path.suffix),
        }
        for file_name, path in sorted(file_paths.items())
    }
    manifest_name = f"{stem}.manifest.json"
    manifest_path = target_dir / manifest_name
    manifest = build_manifest(
        payload,
        artifact_files=artifact_metadata,
        archive_name=archive_name,
    )
    _atomic_write(manifest_path, _json_text(manifest).encode("utf-8"))
    file_paths[manifest_name] = manifest_path

    zip_path: Path | None = None
    zip_sha256: str | None = None
    if create_zip:
        zip_path = target_dir / str(archive_name)
        _write_deterministic_zip(zip_path, file_paths)
        zip_sha256 = _sha256_path(zip_path)

    return ExportBundle(
        output_dir=target_dir,
        files=dict(sorted(file_paths.items())),
        manifest_path=manifest_path,
        zip_path=zip_path,
        zip_sha256=zip_sha256,
    )


def source_export_base_name(source_filename: str) -> str:
    """Return a path-free, extension-free delivery stem for one source file.

    Uploaded names are untrusted metadata.  Strip both POSIX and Windows path
    components before removing the media extension, then reuse the exporter's
    strict ASCII normalization.  The fallback keeps malformed legacy metadata
    deliverable without ever using it as a path.
    """

    source_component = str(source_filename or "").replace("\\", "/").split("/")[-1]
    suffix = Path(source_component).suffix
    source_stem = source_component[: -len(suffix)] if suffix else source_component
    try:
        stem = _safe_base_name(source_stem)
    except ValueError:
        stem = "transcript"
    if stem.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"
    return stem


# Clear, discoverable aliases for integrations that use "export" terminology.
export_json = render_json
export_docx = render_docx
export_txt = render_txt
export_srt = render_srt
export_vtt = render_vtt
export_csv = render_csv
write_exports = write_export_bundle


def format_srt_timestamp(seconds: Any) -> str:
    hours, minutes, whole_seconds, milliseconds = _timestamp_parts(seconds)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def format_vtt_timestamp(seconds: Any) -> str:
    hours, minutes, whole_seconds, milliseconds = _timestamp_parts(seconds)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def _timestamp_parts(seconds: Any) -> tuple[int, int, int, int]:
    try:
        value = Decimal(str(seconds))
    except (InvalidOperation, ValueError, TypeError):
        value = Decimal(0)
    if not value.is_finite() or value < 0:
        value = Decimal(0)
    total_ms = int((value * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return hours, minutes, whole_seconds, milliseconds


def _coerce_result(result: Any) -> dict[str, Any]:
    if hasattr(result, "to_dict") and callable(result.to_dict):
        result = result.to_dict()
    elif is_dataclass(result) and not isinstance(result, type):
        result = asdict(result)
    if not isinstance(result, Mapping):
        raise TypeError("result must be a PipelineResult or mapping")
    normalized = _json_safe(result)
    if not isinstance(normalized, dict):
        raise TypeError("result normalization did not produce an object")
    return normalized


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_json_safe(item) for item in value)
    return str(value)


def _json_text(payload: Any) -> str:
    return json.dumps(
        _json_safe(payload),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"


def _without_raw_fields(value: Any) -> Any:
    """Return a copy with every nested ``raw`` field removed."""

    if isinstance(value, Mapping):
        return {
            str(key): _without_raw_fields(item)
            for key, item in value.items()
            if str(key) != "raw"
        }
    if isinstance(value, list):
        return [_without_raw_fields(item) for item in value]
    return value


def _source(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    value = payload.get("source")
    return value if isinstance(value, Mapping) else {}


def _profile(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    value = payload.get("profile")
    return value if isinstance(value, Mapping) else {}


def _diarization(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    value = payload.get("diarization")
    return value if isinstance(value, Mapping) else {}


def _diarization_requested(payload: Mapping[str, Any]) -> bool:
    """Treat legacy results without the explicit field as requested."""

    value = _diarization(payload).get("requested")
    return True if value is None else bool(value)


def _segments(
    payload: Mapping[str, Any], *, translation: bool = False
) -> list[Mapping[str, Any]]:
    container: Any = payload
    if translation:
        container = payload.get("translation")
    if not isinstance(container, Mapping):
        return []
    values = container.get("segments", [])
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    return [value for value in values if isinstance(value, Mapping)]


def _timed_segments(
    segments: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    timed: list[dict[str, Any]] = []
    for segment in segments:
        start = _decimal_or_none(segment.get("start"))
        end = _decimal_or_none(segment.get("end"))
        if start is None or end is None:
            continue
        if start < 0:
            start = Decimal(0)
        if end < start:
            end = start
        item = dict(segment)
        item["start"] = start
        item["end"] = end
        timed.append(item)
    return timed


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _txt_segment(segment: Mapping[str, Any], *, show_speaker: bool = True) -> str:
    start = format_vtt_timestamp(segment.get("start"))
    end = format_vtt_timestamp(segment.get("end"))
    label = ""
    if show_speaker and segment.get("speaker"):
        label = f" {segment['speaker']}:"
    overlap = " [OVERLAP]" if segment.get("overlap") else ""
    return f"[{start} --> {end}]{overlap}{label} {str(segment.get('text', '')).strip()}".rstrip()


def _cue_text(segment: Mapping[str, Any], *, show_speaker: bool) -> str:
    text = str(segment.get("text", "")).replace("\r\n", "\n").replace("\r", "\n").strip()
    prefixes: list[str] = []
    if segment.get("overlap"):
        speakers = [str(value) for value in segment.get("overlap_speakers", [])]
        prefixes.append(
            f"[OVERLAP: {', '.join(speakers)}]" if speakers else "[OVERLAP]"
        )
    if show_speaker and segment.get("speaker"):
        prefixes.append(f"{segment['speaker']}:")
    return " ".join((*prefixes, text)).strip()


def _number_text(value: Any) -> str:
    decimal = _decimal_or_none(value)
    if decimal is None:
        return ""
    text = format(decimal, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _csv_text(value: Any) -> str:
    """Neutralize cells that spreadsheet software may evaluate as formulas."""

    text = str(value)
    candidate = text.lstrip(" \t\r\n")
    if candidate.startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r")):
        return "'" + text
    return text


def _safe_base_name(value: str) -> str:
    name = _SAFE_BASE_RE.sub("_", str(value).strip()).strip("._")
    if not name or name in {".", ".."}:
        raise ValueError("base_name must contain a safe filename character")
    return name[:120]


def _safe_language(value: Any) -> str:
    language = _SAFE_BASE_RE.sub("_", str(value or "unknown").lower()).strip("._")
    return language[:32] or "unknown"


def _prepare_output_dir(output_dir: str | Path) -> Path:
    if output_dir is None:
        raise TypeError("output_dir is required and must be a transient per-job directory")
    target = Path(output_dir).expanduser().resolve()
    if target == Path(target.anchor):
        raise ValueError("filesystem root cannot be used as a delivery output directory")
    if target.exists() and not target.is_dir():
        raise NotADirectoryError(target)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    return target


def _atomic_write(path: Path, data: bytes) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _render_docx_package(
    payload: Mapping[str, Any], *, translation_only: bool
) -> bytes:
    """Build a small, interoperable OOXML package without external services."""

    document = ET.Element(_word_tag("document"))
    body = ET.SubElement(document, _word_tag("body"))
    source = _source(payload)
    translation = payload.get("translation")
    provenance = payload.get("provenance")
    if not isinstance(provenance, Mapping):
        provenance = {}

    if translation_only:
        if not isinstance(translation, Mapping):  # defensive internal guard
            raise ValueError("result has no translation artifact")
        title = "Machine-Generated Translation"
        target_language = str(translation.get("target_language") or "unknown").upper()
        _docx_paragraph(body, title, style="Title")
        _docx_paragraph(
            body,
            f"{source.get('file_name') or 'Uploaded recording'} · {target_language}",
            style="Subtitle",
        )
        _docx_paragraph(
            body,
            "AI-generated translation. Verify important names, quotations, dates, "
            "and numbers against the source recording and source-language transcript.",
            style="MachineNotice",
        )
        if _is_mock(payload):
            _docx_paragraph(
                body,
                "SYNTHETIC MOCK OUTPUT — NOT A TRANSLATION — NOT FOR EVIDENTIARY USE",
                style="CriticalNotice",
            )
        _docx_metadata(
            body,
            [
                ("Source language", translation.get("source_language") or payload.get("source_language") or "unknown"),
                ("Target language", translation.get("target_language") or "unknown"),
                ("Translation status", translation.get("status") or "unknown"),
                ("Job ID", payload.get("job_id")),
                ("Source SHA-256", source.get("sha256")),
                ("Source transcript changed", "No"),
            ],
        )
        _docx_paragraph(body, f"Translation ({target_language})", style="Heading1")
        _docx_paragraph(
            body,
            "This is a separate machine-generated translation artifact. It does "
            "not replace or alter the source-language transcript.",
            style="TranslationNotice",
        )
        translated_segments = _segments(payload, translation=True)
        if translated_segments:
            _docx_segments(body, translated_segments, show_speaker=False)
        elif translation.get("text"):
            _docx_paragraph(body, str(translation["text"]).strip(), style="TranscriptText")
        else:
            error = translation.get("error")
            message = _diagnostic_message(error) if isinstance(error, Mapping) else ""
            _docx_paragraph(
                body,
                f"[Translation unavailable{': ' + message if message else ''}]",
                style="CriticalNotice",
            )
        translation_warnings = translation.get("warnings")
        if isinstance(translation_warnings, Sequence) and not isinstance(
            translation_warnings, (str, bytes, bytearray)
        ):
            _docx_quality_notes(body, translation_warnings, [])
        core_title = f"Translation — {source.get('file_name') or 'uploaded recording'}"
    else:
        _docx_paragraph(body, "Transcript", style="Title")
        _docx_paragraph(
            body,
            str(source.get("file_name") or "Uploaded recording"),
            style="Subtitle",
        )
        review = provenance.get("review")
        review_state = review.get("state") if isinstance(review, Mapping) else None
        diarization_requested = _diarization_requested(payload)
        speaker_verification = (
            " and speaker labels" if diarization_requested else ""
        )
        if review_state == "human_reviewed":
            notice = (
                "AI-generated transcript with saved staff edits. Verify important "
                f"names, quotations, dates, numbers{speaker_verification} against "
                "the recording."
            )
            review_label = "Machine transcript with saved staff edits"
        else:
            notice = (
                "AI-generated transcript; no human verification is recorded. Verify "
                f"important names, quotations, dates, numbers{speaker_verification} "
                "against the recording before relying on this document."
            )
            review_label = "Machine-generated; no human verification recorded"
        _docx_paragraph(body, notice, style="MachineNotice")
        if _is_mock(payload):
            _docx_paragraph(
                body,
                "SYNTHETIC MOCK OUTPUT — NOT A TRANSCRIPT — NOT FOR EVIDENTIARY USE",
                style="CriticalNotice",
            )

        diarization = _diarization(payload)
        diarization_status = str(diarization.get("status") or "unknown")
        speaker_count = diarization.get("speaker_count")
        speaker_status = (
            diarization_status if diarization_requested else "not requested"
        )
        if diarization_requested and speaker_count not in (None, ""):
            speaker_status = f"{diarization_status} ({speaker_count} voice clusters)"
        _docx_metadata(
            body,
            [
                ("Document status", review_label),
                ("Processing status", payload.get("status") or "unknown"),
                ("Source language", payload.get("source_language") or "unknown"),
                ("Speaker attribution", speaker_status),
                ("Job ID", payload.get("job_id")),
            ],
        )
        if diarization_requested and diarization_status.casefold() != "succeeded":
            _docx_paragraph(
                body,
                "Speaker attribution was unavailable. The transcript text may still "
                "be usable, but speakers were not reliably separated.",
                style="CriticalNotice",
            )
        elif speaker_count:
            _docx_paragraph(
                body,
                "Speaker labels identify machine-detected voice clusters; they do "
                "not establish a person's identity unless staff confirmed a name.",
                style="TranslationNotice",
            )

        _docx_paragraph(body, "Source-Language Transcript", style="Heading1")
        source_segments = _segments(payload)
        if source_segments:
            _docx_segments(body, source_segments, show_speaker=True)
        else:
            _docx_paragraph(
                body,
                "[No source speech segments available]",
                style="CriticalNotice",
            )

        if isinstance(translation, Mapping):
            target_language = str(
                translation.get("target_language") or "unknown"
            ).upper()
            _docx_paragraph(
                body,
                f"Translation ({target_language})",
                style="Heading1",
                page_break_before=True,
            )
            _docx_paragraph(
                body,
                "This is a separate AI-generated translation. The source-language "
                "transcript above is unchanged.",
                style="TranslationNotice",
            )
            translated_segments = _segments(payload, translation=True)
            if translated_segments:
                _docx_segments(body, translated_segments, show_speaker=False)
            elif translation.get("text"):
                _docx_paragraph(
                    body,
                    str(translation["text"]).strip(),
                    style="TranscriptText",
                )
            else:
                error = translation.get("error")
                message = _diagnostic_message(error) if isinstance(error, Mapping) else ""
                _docx_paragraph(
                    body,
                    f"[Translation unavailable{': ' + message if message else ''}]",
                    style="CriticalNotice",
                )

        _docx_quality_notes(
            body,
            payload.get("warnings", []),
            payload.get("errors", []),
        )
        _docx_processing_record(body, payload, provenance)
        core_title = f"Transcript — {source.get('file_name') or 'uploaded recording'}"

    section = ET.SubElement(body, _word_tag("sectPr"))
    ET.SubElement(
        section,
        _word_tag("pgSz"),
        {_word_tag("w"): "12240", _word_tag("h"): "15840"},
    )
    ET.SubElement(
        section,
        _word_tag("pgMar"),
        {
            _word_tag("top"): "1080",
            _word_tag("right"): "1080",
            _word_tag("bottom"): "1080",
            _word_tag("left"): "1080",
            _word_tag("header"): "720",
            _word_tag("footer"): "720",
            _word_tag("gutter"): "0",
        },
    )

    package_files = {
        "[Content_Types].xml": _docx_content_types_xml(),
        "_rels/.rels": _docx_package_relationships_xml(),
        "docProps/app.xml": _docx_app_properties_xml(),
        "docProps/core.xml": _docx_core_properties_xml(core_title),
        "word/_rels/document.xml.rels": _docx_document_relationships_xml(),
        "word/document.xml": ET.tostring(
            document, encoding="utf-8", xml_declaration=True
        ),
        "word/styles.xml": _docx_styles_xml(),
    }
    return _deterministic_zip_bytes(package_files)


def _docx_metadata(
    body: ET.Element, rows: Sequence[tuple[str, Any]]
) -> None:
    for label, value in rows:
        if value in (None, ""):
            continue
        paragraph = _docx_paragraph(body, style="Metadata")
        _docx_run(paragraph, f"{label}: ", bold=True)
        _docx_run(paragraph, str(value))


def _docx_segments(
    body: ET.Element,
    segments: Sequence[Mapping[str, Any]],
    *,
    show_speaker: bool,
) -> None:
    for segment in segments:
        paragraph = _docx_paragraph(body, style="TranscriptSegment")
        start = format_vtt_timestamp(segment.get("start"))
        end = format_vtt_timestamp(segment.get("end"))
        _docx_run(paragraph, f"[{start} – {end}] ", color="666666")
        if show_speaker and segment.get("speaker"):
            _docx_run(paragraph, str(segment["speaker"]), bold=True, color="1F4E79")
        if segment.get("overlap"):
            if show_speaker and segment.get("speaker"):
                _docx_run(paragraph, " ")
            _docx_run(paragraph, "[OVERLAPPING SPEECH]", bold=True, color="9C0006")
        if show_speaker and not segment.get("speaker") and not segment.get("overlap"):
            _docx_run(paragraph, "Speaker not identified", italic=True, color="666666")
        _docx_run(paragraph, "\n" + str(segment.get("text") or "").strip())


def _docx_quality_notes(
    body: ET.Element, warnings: Any, errors: Any
) -> None:
    warning_items = _mapping_sequence(warnings)
    error_items = _mapping_sequence(errors)
    if not warning_items and not error_items:
        return
    _docx_paragraph(body, "Quality Notes", style="Heading1", page_break_before=True)
    for warning in warning_items:
        _docx_paragraph(
            body,
            f"Warning — {_diagnostic_message(warning)}",
            style="QualityNote",
        )
    for error in error_items:
        _docx_paragraph(
            body,
            f"Processing issue — {_diagnostic_message(error)}",
            style="CriticalNotice",
        )


def _mapping_sequence(value: Any) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return list(value)


def _diagnostic_message(item: Any) -> str:
    if not isinstance(item, Mapping):
        return str(item)
    stage = str(item.get("stage") or "").strip()
    code = str(item.get("code") or item.get("type") or "").strip()
    message = str(item.get("message") or "").strip()
    prefix = "/".join(value for value in (stage, code) if value)
    if prefix and message:
        return f"[{prefix}] {message}"
    return message or prefix or "Unspecified processing issue"


def _docx_processing_record(
    body: ET.Element,
    payload: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> None:
    profile = _profile(payload)
    diarization = _diarization(payload)
    source = _source(payload)
    rows = [
        ("Source file", source.get("file_name")),
        ("Source SHA-256", source.get("sha256")),
        ("Profile", profile.get("name")),
        ("ASR model", provenance.get("asr_model") or profile.get("asr_model")),
        ("ASR revision", provenance.get("asr_revision")),
        ("Alignment model", provenance.get("alignment_model")),
        (
            "Diarization model",
            provenance.get("diarization_model") or diarization.get("model"),
        ),
        ("Pipeline version", provenance.get("pipeline_version")),
        ("Processing completed", provenance.get("completed_at")),
    ]
    if not any(value not in (None, "") for _, value in rows):
        return
    _docx_paragraph(body, "Processing Record", style="Heading1")
    _docx_metadata(body, rows)
    _docx_paragraph(
        body,
        "This record describes local machine processing and is not a copy of "
        "the source recording.",
        style="TranslationNotice",
    )


def _docx_paragraph(
    parent: ET.Element,
    text: str | None = None,
    *,
    style: str | None = None,
    page_break_before: bool = False,
) -> ET.Element:
    paragraph = ET.SubElement(parent, _word_tag("p"))
    if style or page_break_before:
        properties = ET.SubElement(paragraph, _word_tag("pPr"))
        if style:
            ET.SubElement(
                properties,
                _word_tag("pStyle"),
                {_word_tag("val"): style},
            )
        if page_break_before:
            ET.SubElement(properties, _word_tag("pageBreakBefore"))
    if text is not None:
        _docx_run(paragraph, text)
    return paragraph


def _docx_run(
    paragraph: ET.Element,
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    color: str | None = None,
) -> None:
    run = ET.SubElement(paragraph, _word_tag("r"))
    if bold or italic or color:
        properties = ET.SubElement(run, _word_tag("rPr"))
        if bold:
            ET.SubElement(properties, _word_tag("b"))
        if italic:
            ET.SubElement(properties, _word_tag("i"))
        if color:
            ET.SubElement(
                properties,
                _word_tag("color"),
                {_word_tag("val"): color},
            )
    lines = _xml_safe_text(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for index, line in enumerate(lines):
        if index:
            ET.SubElement(run, _word_tag("br"))
        text_node = ET.SubElement(run, _word_tag("t"))
        if line.startswith((" ", "\t")) or line.endswith((" ", "\t")):
            text_node.set(f"{{{_XML_NS}}}space", "preserve")
        text_node.text = line


def _xml_safe_text(value: Any) -> str:
    """Replace characters forbidden by XML 1.0 instead of corrupting a DOCX."""

    result: list[str] = []
    for character in str(value):
        codepoint = ord(character)
        if (
            codepoint in (0x09, 0x0A, 0x0D)
            or 0x20 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or 0x10000 <= codepoint <= 0x10FFFF
        ):
            result.append(character)
        else:
            result.append("\N{REPLACEMENT CHARACTER}")
    return "".join(result)


def _word_tag(name: str) -> str:
    return f"{{{_WORD_NS}}}{name}"


def _docx_content_types_xml() -> bytes:
    return b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>
"""


def _docx_package_relationships_xml() -> bytes:
    return b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
"""


def _docx_document_relationships_xml() -> bytes:
    return b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
"""


def _docx_core_properties_xml(title: str) -> bytes:
    root = ET.Element(f"{{{_CORE_PROPERTIES_NS}}}coreProperties")
    ET.SubElement(root, f"{{{_DUBLIN_CORE_NS}}}title").text = _xml_safe_text(title)
    ET.SubElement(root, f"{{{_DUBLIN_CORE_NS}}}subject").text = (
        "Machine-generated transcription delivery artifact"
    )
    ET.SubElement(root, f"{{{_DUBLIN_CORE_NS}}}creator").text = (
        "Exculpata Transcript Studio v2"
    )
    ET.SubElement(root, f"{{{_CORE_PROPERTIES_NS}}}lastModifiedBy").text = (
        "Exculpata Transcript Studio v2"
    )
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _docx_app_properties_xml() -> bytes:
    return b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Exculpata Transcript Studio v2</Application>
  <AppVersion>0.1</AppVersion>
</Properties>
"""


def _docx_styles_xml() -> bytes:
    return b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults>
    <w:rPrDefault><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/><w:sz w:val="22"/><w:szCs w:val="22"/><w:lang w:val="en-US"/></w:rPr></w:rPrDefault>
    <w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:next w:val="Subtitle"/><w:qFormat/><w:pPr><w:spacing w:before="0" w:after="80"/></w:pPr><w:rPr><w:b/><w:color w:val="1F4E79"/><w:sz w:val="36"/><w:szCs w:val="36"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="240"/></w:pPr><w:rPr><w:color w:val="666666"/><w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="300" w:after="140"/></w:pPr><w:rPr><w:b/><w:color w:val="1F4E79"/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="MachineNotice"><w:name w:val="Machine Notice"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="80" w:after="180"/><w:shd w:val="clear" w:fill="FFF2CC"/><w:ind w:left="120" w:right="120"/></w:pPr><w:rPr><w:b/><w:color w:val="7F6000"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="CriticalNotice"><w:name w:val="Critical Notice"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="80" w:after="140"/><w:shd w:val="clear" w:fill="FCE4D6"/><w:ind w:left="120" w:right="120"/></w:pPr><w:rPr><w:b/><w:color w:val="9C0006"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="TranslationNotice"><w:name w:val="Artifact Note"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="180"/></w:pPr><w:rPr><w:i/><w:color w:val="666666"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Metadata"><w:name w:val="Metadata"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="40"/><w:ind w:left="120"/></w:pPr><w:rPr><w:sz w:val="20"/><w:szCs w:val="20"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="TranscriptSegment"><w:name w:val="Transcript Segment"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="80" w:after="140"/><w:keepTogether/></w:pPr></w:style>
  <w:style w:type="paragraph" w:styleId="TranscriptText"><w:name w:val="Transcript Text"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="140"/></w:pPr></w:style>
  <w:style w:type="paragraph" w:styleId="QualityNote"><w:name w:val="Quality Note"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="100"/><w:ind w:left="240" w:hanging="120"/></w:pPr></w:style>
</w:styles>
"""


def _deterministic_zip_bytes(files: Mapping[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for file_name, content in sorted(files.items()):
            info = zipfile.ZipInfo(file_name, date_time=_FIXED_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100600 << 16
            archive.writestr(info, content)
    return output.getvalue()


def _sha256_path(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _media_type(suffix: str) -> str:
    return {
        ".docx": _DOCX_MEDIA_TYPE,
        ".json": "application/json",
        ".txt": "text/plain; charset=utf-8",
        ".srt": "application/x-subrip; charset=utf-8",
        ".vtt": "text/vtt; charset=utf-8",
        ".csv": "text/csv; charset=utf-8",
    }.get(suffix.lower(), "application/octet-stream")


def _write_deterministic_zip(
    zip_path: Path, files: Mapping[str, Path]
) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=zip_path.parent, prefix=f".{zip_path.name}.", delete=False
        ) as handle:
            temp_path = Path(handle.name)
        with zipfile.ZipFile(
            temp_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for file_name, path in sorted(files.items()):
                info = zipfile.ZipInfo(file_name, date_time=_FIXED_ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100600 << 16
                with path.open("rb") as source, archive.open(
                    info,
                    mode="w",
                    force_zip64=True,
                ) as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, zip_path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _is_mock(payload: Mapping[str, Any]) -> bool:
    provenance = payload.get("provenance")
    if not isinstance(provenance, Mapping):
        return False
    engine = provenance.get("engine")
    return isinstance(engine, Mapping) and engine.get("mock") is True


__all__ = [
    "ExportBundle",
    "build_manifest",
    "export_csv",
    "export_docx",
    "export_json",
    "export_srt",
    "export_txt",
    "export_vtt",
    "format_srt_timestamp",
    "format_vtt_timestamp",
    "render_csv",
    "render_docx",
    "render_json",
    "render_srt",
    "render_translation_json",
    "render_translation_docx",
    "render_translation_txt",
    "render_txt",
    "render_vtt",
    "source_export_base_name",
    "write_export_bundle",
    "write_exports",
]
