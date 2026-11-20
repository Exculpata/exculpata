"""Speech API diagnostic and matter-authorized recording dispatch."""
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
import stat
from uuid import uuid4
from pathlib import Path
import urllib.error
import urllib.request

from .external_generator import DestinationRejected
from .external_model_grants import model_destination
from .generator_transport import open_generator_request
from .media_processor_errors import MediaProcessorError
from .pilot_uploads import MEDIA_TYPES
from .retrieval_endpoint_config import configured_endpoint


_PREFIX = "CASE_INTELLIGENCE_ASR"
_RECORDING = Path(__file__).with_name("config") / "generated-speech.wav"
_RECORDING_DIGEST = "325e73ecfff901a056d8e08ea55d7222e77ace5b2058459f0d25ae2b344ac90b"
# The packaged clip is 5.5 seconds; Whisper segment timestamps use 20 ms ticks.
_LAST_TIMESTAMP = 5.52
_MAX_RESPONSE = 64 * 1024
_MAX_CASE_RESPONSE = 64 * 1024 * 1024

UNCONFIGURED = "No external transcription endpoint configured."
INVALID_CONFIGURATION = "Invalid transcription configuration. Check the HTTPS origin, model, credential and relay settings."
UNAVAILABLE = "Transcription service unreachable. Check the endpoint and relay."
AUTHENTICATION = "Transcription authentication failed. Check the credential."
INCOMPATIBLE = "Invalid transcription response. Check support for timed segments in verbose JSON."
REFUSED = "Transcription service refused the request. Check the model and API compatibility."
FIXTURE_UNAVAILABLE = "Transcription diagnostic recording is unavailable. Check the application installation."
DIAGNOSTIC_MESSAGES = frozenset({
    "ok", UNCONFIGURED, INVALID_CONFIGURATION, UNAVAILABLE, AUTHENTICATION, INCOMPATIBLE, REFUSED,
    FIXTURE_UNAVAILABLE,
})


def configured_speech_endpoint(environment=None):
    return configured_endpoint("ASR", environment)


def _multipart_parts(fields, filename, media_type):
    boundary = "exculpata-" + uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        for name, value in fields.items()
    ]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                 f'Content-Type: {media_type}\r\n\r\n')
    return f"multipart/form-data; boundary={boundary}", "".join(parts).encode(), f"\r\n--{boundary}--\r\n".encode()


def _transcription_fields(model):
    return {"model": model, "response_format": "verbose_json", "timestamp_granularities[]": "segment"}


def request_headers(content_type, *, prefix=_PREFIX):
    headers = {"Content-Type": content_type, "Accept": "application/json"}
    key_file = os.getenv(prefix + "_API_KEY_FILE", "")
    if key_file:
        with Path(key_file).open("rb") as stream:
            key = stream.read(4097)
        if len(key) > 4096:
            raise ValueError("Invalid credential")
        token = key.decode("ascii").strip()
        if not token or not all("!" <= character <= "~" for character in token):
            raise ValueError("Invalid credential")
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _valid_transcript(response, last_timestamp=_LAST_TIMESTAMP):
    if not isinstance(response, dict):
        return False
    for field in ("language", "text"):
        if not isinstance(response.get(field), str) or not response[field].strip():
            return False
    segments = response.get("segments")
    if not isinstance(segments, list) or not 0 < len(segments) <= 100_000:
        return False
    previous_start = 0
    for segment in segments:
        if not isinstance(segment, dict):
            return False
        start, end, text = segment.get("start"), segment.get("end"), segment.get("text")
        if (type(start) not in (float, int) or type(end) not in (float, int)
                or not previous_start <= start < end <= last_timestamp
                or round(start * 1000) >= round(end * 1000)
                or not isinstance(text, str) or len(text) > 20_000 or not text.strip()):
            return False
        previous_start = start
    return True


@contextmanager
def recording_request(url, fields, source, job, *, prefix):
    """Hold the verified original descriptor throughout the bounded multipart transfer."""
    suffix = next((suffix for suffix, media_type in MEDIA_TYPES.items() if media_type == job.media_type), None)
    if suffix is None:
        raise MediaProcessorError("The recording type is unsupported.")
    content_type, before, after = _multipart_parts(fields, f"recording{suffix}", job.media_type)
    headers = request_headers(content_type, prefix=prefix)
    headers["Content-Length"] = str(len(before) + job.byte_size + len(after))
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size != job.byte_size
                or hashlib.file_digest(stream, "sha256").hexdigest() != job.source_sha256):
            raise MediaProcessorError("The stored recording changed before processing.")
        stream.seek(0)

        def chunks():
            yield before
            remaining = job.byte_size
            while remaining:
                chunk = stream.read(min(remaining, 256 * 1024))
                if not chunk:
                    raise MediaProcessorError("The stored recording changed during processing.")
                remaining -= len(chunk)
                yield chunk
            yield after

        yield urllib.request.Request(url, data=chunks(), headers=headers, method="POST")


def transcribe_recording(workspace, job, source, endpoint, *, check_access):
    target = model_destination("transcription", endpoint.url, endpoint.model)
    url = endpoint.url + "/v1/audio/transcriptions"
    try:
        with recording_request(url, _transcription_fields(endpoint.model), source, job, prefix=_PREFIX) as request:
            check_access()
            if not workspace.model_grant_allows(job.matter_id, target):
                raise MediaProcessorError("Transcription requires matter approval for the configured endpoint and model.")
            with open_generator_request(request, timeout=endpoint.timeout, environment_prefix=_PREFIX) as response:
                raw = response.read(_MAX_CASE_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise MediaProcessorError(AUTHENTICATION if exc.code in {401, 403} else REFUSED) from None
    except DestinationRejected:
        raise MediaProcessorError(INVALID_CONFIGURATION) from None
    except (http.client.HTTPException, OSError, ValueError):
        raise MediaProcessorError("Could not send the verified recording. Check the source, credential and relay.") from None
    try:
        if len(raw) > _MAX_CASE_RESPONSE:
            raise ValueError
        result = json.loads(raw)
        if not _valid_transcript(result, last_timestamp=job.duration_ms / 1000 + 0.02):
            raise ValueError
    except (ValueError, RecursionError):
        raise MediaProcessorError(INCOMPATIBLE) from None
    return {
        "language": result["language"],
        "segments": [{"id": f"segment-{index}", "start": row["start"], "end": row["end"], "text": row["text"]}
                     for index, row in enumerate(result["segments"], 1)],
        "quality": {"diarization_status": "skipped"},
        "provenance": {"transcription": {"endpoint": target.endpoint, "model": target.model}},
    }


def doctor():
    try:
        endpoint = configured_speech_endpoint()
    except ValueError:
        return INVALID_CONFIGURATION
    if endpoint is None:
        return UNCONFIGURED
    try:
        with _RECORDING.open("rb") as stream:
            recording = stream.read(176_078 + 1)
    except OSError:
        return FIXTURE_UNAVAILABLE
    if hashlib.sha256(recording).hexdigest() != _RECORDING_DIGEST:
        return FIXTURE_UNAVAILABLE

    content_type, before, after = _multipart_parts(_transcription_fields(endpoint.model), "diagnostic.wav", "audio/wav")
    body = before + recording + after
    try:
        headers = request_headers(content_type)
    except (ValueError, OSError):
        return INVALID_CONFIGURATION
    try:
        request = urllib.request.Request(endpoint.url + "/v1/audio/transcriptions", data=body, headers=headers)
        with open_generator_request(request, timeout=min(30, endpoint.timeout), environment_prefix=_PREFIX) as response:
            raw = response.read(_MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        return AUTHENTICATION if exc.code in {401, 403} else REFUSED
    except DestinationRejected:
        return INVALID_CONFIGURATION
    except (http.client.HTTPException, OSError, ValueError):
        return UNAVAILABLE
    if len(raw) <= _MAX_RESPONSE:
        try:
            if _valid_transcript(json.loads(raw)):
                return "ok"
        except (ValueError, RecursionError):
            pass
    return INCOMPATIBLE


if __name__ == "__main__":
    print(json.dumps({"transcription": doctor()}))
