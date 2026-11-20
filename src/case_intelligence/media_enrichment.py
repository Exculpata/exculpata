"""Capability-specific enrichment of timestamped speech, before durable import."""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from functools import partial
import http.client
import json
import re
import unicodedata
import urllib.error
import urllib.request

from . import speech_endpoint
from .external_generator import DestinationRejected
from .external_model_grants import model_destination
from .generator_transport import open_generator_request
from .media_processor_errors import MediaProcessorError
from .retrieval_endpoint_config import configured_endpoint


_ROUTES = {
    "alignment": ("ALIGNMENT", "/v1/audio/alignments"),
    "translation": ("TRANSLATION", "/v1/translations"),
    "speaker_labeling": ("DIARIZATION", "/v1/audio/speaker-labels"),
}
_LABELS = {"alignment": "Word alignment", "translation": "Translation", "speaker_labeling": "Speaker labeling"}
_LIMIT = 8 * 1024 * 1024
_ERRORS = {
    "configuration": "Check the endpoint configuration and relay.",
    "unconfigured": "No endpoint configured.",
    "authentication": "The endpoint rejected its credential.",
    "unreachable": "The service is unreachable.",
    "refused": "The service refused the request.",
    "incompatible": "The service returned an invalid response.",
    "limit": "The transcript exceeds the service request limit.",
    "fixture": "The diagnostic recording is unavailable.",
    "denied": "The matter has no current approval for this service.",
}


class MediaContractError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(_ERRORS[code])


def _aligned_times(original, words):
    if not isinstance(words, list) or not words:
        raise MediaContractError("incompatible")
    previous = original["start"]
    tokens = []
    for word in words:
        if not isinstance(word, dict):
            raise MediaContractError("incompatible")
        token, start, end = word.get("word"), word.get("start"), word.get("end")
        if (not isinstance(token, str) or not token.strip() or type(start) not in (int, float)
                or type(end) not in (int, float) or not previous <= start < end <= original["end"]
                or round(start * 1000) >= round(end * 1000)):
            raise MediaContractError("incompatible")
        tokens.append(token)
        previous = end
    if "".join("".join(tokens).split()) != "".join(original["text"].split()):
        raise MediaContractError("incompatible")
    return {"start": words[0]["start"], "end": previous}


def apply_contract(capability, segments, reply):
    """Return complete annotations on the original segment identities and order."""
    rows = reply.get("segments") if isinstance(reply, dict) else None
    if not isinstance(rows, list) or len(rows) != len(segments) or not rows:
        raise MediaContractError("incompatible")
    result = []
    for segment, row in zip(segments, rows, strict=True):
        if not isinstance(row, dict) or row.get("id") != segment["id"]:
            raise MediaContractError("incompatible")
        if capability == "alignment":
            annotation = _aligned_times(segment, row.get("words"))
            if result and annotation["start"] < result[-1]["start"]:
                raise MediaContractError("incompatible")
        elif capability == "speaker_labeling":
            speaker = row.get("speaker")
            if not isinstance(speaker, str) or not re.fullmatch(r"SPEAKER_[0-9]{1,5}", speaker):
                raise MediaContractError("incompatible")
            annotation = {"speaker": {"cluster_id": speaker}}
        elif capability == "translation":
            text = row.get("text")
            if (not isinstance(text, str) or not text.strip() or len(text) > 20_000
                    or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} and c not in "\n\t" for c in text)):
                raise MediaContractError("incompatible")
            annotation = {"translated_text": text.strip()}
        else:
            raise ValueError("Unknown speech capability.")
        result.append(segment | annotation)
    return result


def _serialized(value):
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    if len(encoded) > _LIMIT:
        raise MediaContractError("limit")
    return encoded


def _request(capability, endpoint, segments, language, source, job):
    role, api = _ROUTES[capability]
    prefix = "CASE_INTELLIGENCE_" + role
    if capability == "translation":
        rows = [{"id": item["id"], "text": item["text"]} for item in segments]
        body = _serialized({"model": endpoint.model, "source_language": language, "target_language": "en",
                            "segments": rows}).encode("ascii")
        headers = speech_endpoint.request_headers("application/json", prefix=prefix)
        return nullcontext(urllib.request.Request(endpoint.url + api, data=body, headers=headers))
    keys = ("id", "start", "end", "text") if capability == "alignment" else ("id", "start", "end")
    rows = [{key: item[key] for key in keys} for item in segments]
    fields = {"model": endpoint.model, "segments": _serialized(rows)}
    if capability == "alignment":
        fields["language"] = language
    return speech_endpoint.recording_request(endpoint.url + api, fields, source, job, prefix=prefix)


def _call(capability, endpoint, segments, language, source, job, *, authorize, timeout):
    try:
        with _request(capability, endpoint, segments, language, source, job) as request:
            authorize()
            prefix = "CASE_INTELLIGENCE_" + endpoint.role
            try:
                with open_generator_request(request, timeout=timeout, environment_prefix=prefix) as response:
                    raw = response.read(_LIMIT + 1)
            except urllib.error.HTTPError as exc:
                exc.close()
                raise MediaContractError("authentication" if exc.code in {401, 403} else "refused") from None
            except DestinationRejected:
                raise MediaContractError("configuration") from None
            except (http.client.HTTPException, OSError, ValueError):
                raise MediaContractError("unreachable") from None
    except (OSError, ValueError):
        raise MediaContractError("configuration") from None
    if len(raw) > _LIMIT:
        raise MediaContractError("incompatible")
    try:
        reply = json.loads(raw)
    except (ValueError, RecursionError):
        raise MediaContractError("incompatible") from None
    return apply_contract(capability, segments, reply)


def _configured(capability):
    try:
        return configured_endpoint(_ROUTES[capability][0])
    except ValueError:
        raise MediaContractError("configuration") from None


def _check_approval(workspace, matter_id, destination, check_access):
    check_access()
    if not workspace.model_grant_allows(matter_id, destination):
        raise MediaContractError("denied")


def _source_rows(segments, duration_ms):
    rows = []
    previous = 0
    if not isinstance(segments, list) or not 0 < len(segments) <= 100_000:
        raise MediaContractError("incompatible")
    for index, segment in enumerate(segments, 1):
        if not isinstance(segment, dict):
            raise MediaContractError("incompatible")
        start, end = segment.get("start"), segment.get("end")
        text = segment.get("model_text") or segment.get("text")
        if (type(start) not in (float, int) or type(end) not in (float, int)
                or not previous <= start < end <= (duration_ms + 2000) / 1000
                or round(start * 1000) >= round(end * 1000)
                or not isinstance(text, str) or not text.strip() or len(text) > 20_000):
            raise MediaContractError("incompatible")
        rows.append({"id": f"segment-{index}", "start": start, "end": end, "text": text})
        previous = start
    return rows


def enrich_transcript(workspace, job, source, payload, *, check_access):
    if payload.get("review_state") == "Human reviewed":
        return payload
    originals = payload["segments"]
    segments = None
    quality = dict(payload["quality"]) if isinstance(payload.get("quality"), dict) else {}
    warnings = list(payload["warnings"]) if isinstance(payload.get("warnings"), list) else []
    provenance = dict(payload["provenance"]) if isinstance(payload.get("provenance"), dict) else {}
    language = payload.get("language") or quality.get("detected_language") or "und"
    if not isinstance(language, str) or not re.fullmatch(r"[A-Za-z][A-Za-z _-]{0,63}", language):
        language = "und"
    degraded = bool(payload.get("degraded"))
    for capability in _ROUTES:
        status_key = capability + "_status"
        try:
            endpoint = _configured(capability)
            if not endpoint:
                continue
            if capability == "alignment" and quality.get("source_alignment_status") == "succeeded":
                quality[status_key] = "completed_locally"
                continue
            if capability == "translation" and language.casefold().replace("_", "-").split("-")[0] in {"en", "english"}:
                quality[status_key] = "skipped"
                continue
            destination = model_destination(capability, endpoint.url, endpoint.model)

            authorize = partial(_check_approval, workspace, job.matter_id, destination, check_access)
            authorize()
            if segments is None:
                segments = _source_rows(originals, job.duration_ms)
            segments = _call(capability, endpoint, segments, language, source, job, authorize=authorize,
                             timeout=endpoint.timeout)
        except MediaContractError as exc:
            quality[status_key] = exc.code
            if capability == "speaker_labeling":
                quality["diarization_status"] = "skipped" if exc.code == "denied" else "failed"
            warnings.append({"code": capability + "_" + exc.code, "user_message": f"{_LABELS[capability]}: {exc}"})
            degraded = True
            continue
        quality[status_key] = "completed"
        if capability == "speaker_labeling":
            quality["diarization_status"] = "completed"
        provenance[capability] = {"endpoint": destination.endpoint, "model": destination.model}
    if segments is not None:
        originals = [original | {key: row[key] for key in ("start", "end", "speaker", "translated_text") if key in row}
                     for original, row in zip(originals, segments, strict=True)]
    return payload | {"segments": originals, "quality": quality, "warnings": warnings,
                      "provenance": provenance, "degraded": degraded}


@dataclass(frozen=True)
class _DiagnosticRecording:
    byte_size: int = 176_078
    source_sha256: str = speech_endpoint._RECORDING_DIGEST
    media_type: str = "audio/wav"


def probe_contract(capability):
    """Run the dispatch contract using only packaged audio and invented text."""
    endpoint = _configured(capability)
    if endpoint is None:
        raise MediaContractError("unconfigured")
    translating = capability == "translation"
    text = ("Grabación de prueba." if translating else
            "The blue marker is beside the empty box. This is a generated recording for a software test.")
    segments = [{"id": "segment-1", "start": 0.0, "end": 5.5, "text": text}]
    try:
        _call(capability, endpoint, segments, "es" if translating else "en",
              speech_endpoint._RECORDING, _DiagnosticRecording(), authorize=lambda: None,
              timeout=min(30, endpoint.timeout))
    except MediaProcessorError:
        raise MediaContractError("fixture") from None
