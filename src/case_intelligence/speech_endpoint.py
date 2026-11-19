"""Check an operator's speech API with the packaged synthetic recording."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

from .external_generator import DestinationRejected
from .generator_transport import open_generator_request
from .retrieval_endpoint_config import configured_endpoint


_PREFIX = "CASE_INTELLIGENCE_ASR"
_RECORDING = Path(__file__).with_name("config") / "generated-speech.wav"
_RECORDING_DIGEST = "325e73ecfff901a056d8e08ea55d7222e77ace5b2058459f0d25ae2b344ac90b"
# The packaged clip is 5.5 seconds; Whisper segment timestamps use 20 ms ticks.
_LAST_TIMESTAMP = 5.52
_MAX_RESPONSE = 64 * 1024

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


def _multipart(model, recording):
    boundary = "exculpata-synthetic-speech"
    body = bytearray()

    def add_field(name, value):
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(value.encode() + b"\r\n")

    add_field("model", model)
    add_field("response_format", "verbose_json")
    add_field("timestamp_granularities[]", "segment")
    body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="diagnostic.wav"\r\n'
                'Content-Type: audio/wav\r\n\r\n'.encode())
    body.extend(recording)
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", bytes(body)


def _headers(content_type):
    headers = {"Content-Type": content_type, "Accept": "application/json"}
    key_file = os.getenv(_PREFIX + "_API_KEY_FILE", "")
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


def _valid_transcript(response):
    if not isinstance(response, dict):
        return False
    for field in ("language", "text"):
        if not isinstance(response.get(field), str) or not response[field].strip():
            return False
    segments = response.get("segments")
    if not isinstance(segments, list) or not segments:
        return False
    previous_start = 0
    for segment in segments:
        if not isinstance(segment, dict):
            return False
        start, end, text = segment.get("start"), segment.get("end"), segment.get("text")
        if (type(start) not in (float, int) or type(end) not in (float, int)
                or not previous_start <= start < end <= _LAST_TIMESTAMP
                or not isinstance(text, str) or not text.strip()):
            return False
        previous_start = start
    return True


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

    content_type, body = _multipart(endpoint.model, recording)
    try:
        headers = _headers(content_type)
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
