"""Bounded, persistent localhost upload storage for the supervised pilot."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from errno import ENOSYS, EOPNOTSUPP, EPERM, EXDEV
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO, Callable, Iterable, Sequence

from .managed_storage import format_bytes
from .malware_scan import MalwareScanner, scanner_status

MAX_FILES = 10
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_MEDIA_FILE_BYTES = 5 * 1024 * 1024 * 1024
MAX_REQUEST_BYTES = 100 * 1024 * 1024
MAX_UPLOAD_SESSION_ITEMS = 2_000
# One browser selection may be admitted as several independently durable
# sessions so large productions do not require one unbounded control request.
MAX_UPLOAD_INTAKE_ITEMS = 10_000
MAX_UPLOAD_SESSION_BYTES = 5 * 1024 * 1024 * 1024
MAX_UPLOAD_CHUNK_BYTES = 2 * 1024 * 1024
# Raw multipart cap includes the accepted 100 MiB aggregate plus 1 MiB for
# boundaries and part headers. It is enforced before multipart parsing.
MAX_RAW_REQUEST_BYTES = MAX_REQUEST_BYTES + 1024 * 1024
CHUNK_BYTES = 1024 * 1024
MAX_DISPLAY_NAME_BYTES = 240
MAX_TEXT_CHARS = 5_000_000
MAX_TEXT_UTF8_BYTES = MAX_TEXT_CHARS * 4 + 3
MAX_TEXT_LINES = 100_000
MAX_TEXT_LINE_CHARS = 100_000
MAX_TEXT_CHUNKS = 5_000
TEXT_LINES_PER_CHUNK = 20
PDF_TIMEOUT_SECONDS = 15
MAX_PDF_PAGES = 500
MAX_PDF_PAGE_CHARS = 250_000
MAX_PDF_TOTAL_CHARS = 5_000_000
MAX_PDF_CHUNKS = 2_000
MAX_OCR_PAGES = 25
MAX_EXPANDED_OCR_PAGES = 500
MAX_OCR_RASTER_BYTES = 16 * 1024 * 1024
OCR_TIMEOUT_SECONDS = 20
# Tesseract word confidences (0-100) that separate a misoriented reading from
# a confident one. Retried orientations share one additional OCR timeout.
OCR_LOW_WORD_CONFIDENCE = 50
OCR_CONFIDENT_WORD = 80
OCR_REORIENT_LOW_SHARE = 0.25
OCR_REORIENT_DEGREES = (180, 90, 270)
MAX_OCR_TSV_BYTES = 16 * 1024 * 1024
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOCUMENT_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".docx": DOCX_MEDIA_TYPE,
    ".txt": "text/plain",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".eml": "message/rfc822",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
IMAGE_MEDIA_TYPES = {"image/jpeg", "image/png", "image/tiff"}
EMAIL_MEDIA_TYPES = {"message/rfc822"}
SPREADSHEET_MEDIA_TYPES = {
    "text/csv",
    "text/tab-separated-values",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
EXTENDED_SOURCE_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".png",
    ".tif",
    ".tiff",
    ".eml",
    ".csv",
    ".tsv",
    ".xlsx",
}
AUDIO_MEDIA_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
}
VIDEO_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}
MEDIA_TYPES = {**AUDIO_MEDIA_TYPES, **VIDEO_MEDIA_TYPES}
_ALLOWED = {**DOCUMENT_MEDIA_TYPES, **MEDIA_TYPES}
_MEDIA_TYPE_ALIASES = {
    ".wav": {"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"},
    ".mp3": {"audio/mpeg", "audio/mp3"},
    ".m4a": {"audio/mp4", "audio/x-m4a", "video/mp4"},
    ".ogg": {"audio/ogg", "application/ogg"},
    ".opus": {"audio/ogg", "audio/opus", "application/ogg"},
    ".mp4": {"video/mp4", "audio/mp4"},
    ".mov": {"video/quicktime"},
    ".webm": {"video/webm", "audio/webm"},
    ".jpg": {"", "image/jpeg", "image/pjpeg"},
    ".jpeg": {"", "image/jpeg", "image/pjpeg"},
    ".png": {"", "image/png"},
    ".tif": {"", "image/tiff"},
    ".tiff": {"", "image/tiff"},
    ".eml": {"", "message/rfc822", "application/octet-stream"},
    ".csv": {"", "text/csv", "application/csv", "application/vnd.ms-excel", "text/plain"},
    ".tsv": {"", "text/tab-separated-values", "text/tsv", "text/plain"},
    ".xlsx": {
        "",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/octet-stream",
    },
}
_BIDI = {chr(value) for value in (*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x061C, 0x200E, 0x200F)}
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CLOCK$", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_STAGE_NAME = re.compile(r"^\.upload-[0-9a-f]{32}\.part$")
_UPLOAD_ITEM_ID = re.compile(r"^upload-item-([0-9a-f]{32})$")


class UploadProblem(ValueError):
    """An expected upload problem safe to show to staff."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def is_media_type(media_type: str) -> bool:
    return media_type in MEDIA_TYPES.values()


def media_kind(media_type: str) -> str:
    if media_type in AUDIO_MEDIA_TYPES.values():
        return "audio"
    if media_type in VIDEO_MEDIA_TYPES.values():
        return "video"
    return ""


def maximum_file_bytes(
    suffix: str,
    *,
    document_limit: int = MAX_FILE_BYTES,
    media_limit: int = MAX_MEDIA_FILE_BYTES,
) -> int:
    return media_limit if suffix.casefold() in MEDIA_TYPES else document_limit


def canonical_media_type(suffix: str) -> str:
    try:
        return _ALLOWED[suffix.casefold()]
    except KeyError as exc:
        raise UploadProblem("That source type is not supported.") from exc


class RawUploadLimitMiddleware:
    """Buffer and bound known upload routes before Starlette parses multipart data."""

    def __init__(self, app, limit: int = MAX_RAW_REQUEST_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        bounded_upload = path == "/matters/pilot/uploads" or bool(
            re.fullmatch(r"/matters/m-[0-9a-f]{12}/uploads", path)
        )
        if scope.get("type") != "http" or scope.get("method") != "POST" or not bounded_upload:
            await self.app(scope, receive, send)
            return
        lengths: list[int] = []
        malformed = False
        for key, value in scope.get("headers", ()):
            if key.lower() != b"content-length":
                continue
            try:
                decoded = value.decode("ascii")
                if not decoded or not decoded.isdecimal():
                    raise ValueError
                lengths.append(int(decoded))
            except (UnicodeDecodeError, ValueError):
                malformed = True
        if malformed or len(set(lengths)) > 1 or any(value < 0 or value > self.limit for value in lengths):
            await self._reject(send)
            return
        messages: list[dict] = []
        used = 0
        while True:
            message = await receive()
            kind = message.get("type")
            if kind == "http.disconnect":
                await self._respond(send, 400, b"Upload interrupted. Choose the files and try again.")
                return
            if kind != "http.request":
                messages.append(message)
                continue
            body = message.get("body", b"")
            used += len(body)
            if used > self.limit:
                await self._reject(send)
                return
            messages.append(message)
            if not message.get("more_body", False):
                break
        index = 0

        async def replay():
            nonlocal index
            if index < len(messages):
                value = messages[index]
                index += 1
                return value
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay, send)

    @staticmethod
    async def _respond(send, status: int, body: bytes) -> None:
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def _reject(self, send) -> None:
        await self._respond(send, 413, b"Upload request is too large. Choose fewer or smaller files.")


@dataclass(frozen=True)
class PdfOcrResult:
    text: str = ""
    status: str = "failed"


class _OcrStop(Exception):
    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


@dataclass(frozen=True)
class _OcrReading:
    text: str
    words: tuple[tuple[str, float], ...]


def _rotate_pgm(raster: bytes, degrees: int) -> bytes | None:
    """Rotate binary 8-bit grayscale pixels clockwise; None if not that format."""
    header = re.match(rb"P5\s+(\d+)\s+(\d+)\s+255\s", raster)
    if header is None:
        return None
    width, height = (int(value) for value in header.groups())
    pixels = raster[header.end():]
    if not width or not height or len(pixels) != width * height:
        return None
    if degrees == 180:
        return raster[:header.end()] + pixels[::-1]
    if degrees == 90:
        rotated = b"".join(pixels[(height - 1) * width + column::-width] for column in range(width))
    else:
        rotated = b"".join(pixels[width - 1 - column::width] for column in range(width))
    return b"P5\n%d %d\n255\n" % (height, width) + rotated


def _tesseract_reading(
    raster: bytes, language: str, mode: str, timeout: float, environment: dict[str, str],
) -> _OcrReading:
    """Return text and word confidences from one bounded Tesseract run."""
    import sys

    with tempfile.TemporaryDirectory(prefix="recordbench-ocr-") as directory:
        base = Path(directory) / "page"
        recognized = subprocess.run(
            [
                sys.executable, "-I", "-S", str(Path(__file__).with_name("ocr_process_helper.py")),
                str(MAX_OCR_TSV_BYTES),
                "/usr/bin/tesseract", "stdin", str(base), "-l", language,
                "--psm", mode, "txt", "tsv",
            ],
            input=raster,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=environment,
            cwd=directory,
        )
        # The helper writes this short receipt before exec. Use the actual
        # inherited cap, not just the nominal maximum, and never trust output
        # from a process that did not report successful limit installation.
        receipt_line, separator, _ = recognized.stdout[:32].partition(b"\n")
        receipt = re.fullmatch(rb"recordbench-ocr-limit:([0-9]{1,8})", receipt_line) if separator else None
        if receipt is None or (effective_limit := int(receipt[1])) > MAX_OCR_TSV_BYTES:
            raise _OcrStop("failed")
        text_path, table_path = base.with_suffix(".txt"), base.with_suffix(".tsv")
        # Without a confidence table the reading is kept but never re-oriented.
        table_size = table_path.stat().st_size if table_path.is_file() else 0
        text_size = text_path.stat().st_size if text_path.is_file() else 0
        # The child enforces RLIMIT_FSIZE before exec, so output cannot grow past
        # the cap while recognition runs. Treat a file reaching the cap as
        # incomplete even if the executable fails to report its write error.
        if (text_size >= effective_limit or table_size >= effective_limit
                or text_size > MAX_PDF_PAGE_CHARS):
            raise _OcrStop("output_limit")
        if recognized.returncode != 0:
            raise _OcrStop("failed")
        text = text_path.read_bytes().decode("utf-8", errors="strict").strip()
        table = table_path.read_bytes().decode("utf-8", errors="strict") if table_size else ""
    words = []
    for row in table.splitlines()[1:]:
        fields = row.split("\t")
        if len(fields) != 12 or fields[0] != "5" or not any(c.isalnum() for c in fields[11]):
            continue
        try:
            confidence = float(fields[10])
        except ValueError:
            continue
        if confidence >= 0:
            words.append((fields[11], confidence))
    return _OcrReading(text, tuple(words))


def _ocr_word_keys(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.casefold())


def _new_ocr_words(reading: _OcrReading, native_words: list[str]) -> list[tuple[str, float]]:
    """Discount complete native occurrences, including bounded joined runs."""
    from array import array

    words = [(key, confidence) for word, confidence in reading.words for key in _ocr_word_keys(word)]
    if not native_words or not words:
        return words
    ocr_keys = {key for key, _ in words}
    single_occurrences: dict[str, array] = {}
    joined_occurrences: dict[str, array] = {}
    # Index original positions once. Compact integer pairs bound index storage;
    # inverse joins are indexed only for keys actually present in OCR output.
    for start, word in enumerate(native_words):
        single_occurrences.setdefault(word, array("I")).extend((start, start + 1))
        joined = word
        for end in range(start + 1, min(len(native_words), start + 12)):
            if len(joined) + len(native_words[end]) > 256:
                break
            joined += native_words[end]
            if joined in ocr_keys:
                joined_occurrences.setdefault(joined, array("I")).extend((start, end + 1))
    consumed = bytearray(len(native_words))
    single_cursors: dict[str, int] = {}
    joined_cursors: dict[str, int] = {}

    def available(occurrences: dict[str, array], cursors: dict[str, int], key: str):
        spans = occurrences.get(key)
        if spans is None:
            return None
        cursor = cursors.get(key, 0)
        while cursor < len(spans):
            start, end = spans[cursor], spans[cursor + 1]
            if not any(consumed[start:end]):
                cursors[key] = cursor
                return start, end
            # A used occurrence cannot be reused through an overlapping join.
            cursor += 2
        cursors[key] = cursor
        return None

    added = []
    index = 0
    while index < len(words):
        joined = words[index][0]
        single = available(single_occurrences, single_cursors, joined)
        inverse = available(joined_occurrences, joined_cursors, joined)
        span = min((value for value in (single, inverse) if value is not None), default=None)
        match = (*span, index + 1) if span is not None else None
        # Missing native whitespace can join stamp words. Match an entire run,
        # never a substring, and bound work even for hostile confidence tables.
        for end in range(index + 1, min(len(words), index + 12)):
            if len(joined) + len(words[end][0]) > 256:
                break
            joined += words[end][0]
            span = available(single_occurrences, single_cursors, joined)
            if span is not None:
                match = (*span, end + 1)
        if match is not None:
            start, end, index = match
            consumed[start:end] = b"\x01" * (end - start)
        else:
            added.append(words[index])
            index += 1
    return added


def _confident_new_words(reading: _OcrReading, native_words: list[str]) -> int:
    return sum(confidence >= OCR_CONFIDENT_WORD for _, confidence in _new_ocr_words(reading, native_words))


def _ocr_pdf_page(source: Path, page_number: int, *, native_text: str = "") -> PdfOcrResult:
    """Recognize a bounded page, retaining the reason no text was obtained."""
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "OMP_THREAD_LIMIT": "1",
    }
    try:
        rendered = subprocess.run(
            [
                "/usr/bin/pdftoppm", "-f", str(page_number), "-l", str(page_number),
                "-scale-to", "2600", "-gray", "-singlefile", str(source),
            ],
            capture_output=True,
            check=False,
            timeout=OCR_TIMEOUT_SECONDS,
            env=environment,
        )
        if len(rendered.stdout) > MAX_OCR_RASTER_BYTES:
            return PdfOcrResult(status="output_limit")
        if rendered.returncode != 0 or not rendered.stdout:
            return PdfOcrResult(status="failed")
        language = os.getenv("CASE_INTELLIGENCE_OCR_LANGUAGE", "eng").strip()
        if not re.fullmatch(r"[A-Za-z0-9_+-]{1,40}", language):
            language = "eng"
        # Automatic orientation/layout first; block fallback is only for an empty
        # result. Length is not a quality score and never replaces native text.
        for page_segmentation_mode in ("1", "6"):
            reading = _tesseract_reading(
                rendered.stdout, language, page_segmentation_mode, OCR_TIMEOUT_SECONDS, environment,
            )
            if any(character.isalnum() for character in reading.text):
                break
        else:
            return PdfOcrResult(status="no_text")
    except _OcrStop as stop:
        return PdfOcrResult(status=stop.status)
    except subprocess.TimeoutExpired:
        return PdfOcrResult(status="timed_out")
    except (OSError, UnicodeDecodeError):
        return PdfOcrResult(status="failed")
    # Judge only what OCR adds: confident native stamp words must not mask a
    # misread body.
    native_words = _ocr_word_keys(native_text)
    added = [confidence for _, confidence in _new_ocr_words(reading, native_words)]
    low = sum(1 for confidence in added if confidence < OCR_LOW_WORD_CONFIDENCE)
    # A populated native-only table can mean an unread rotated image body.
    # Missing word confidences cannot support orientation comparison.
    if not reading.words or (added and low < OCR_REORIENT_LOW_SHARE * len(added)):
        return PdfOcrResult(text=reading.text, status="recognized")
    # Tesseract keeps the unrotated reading when its orientation estimate is
    # weak (sparse text, or an upright stamp over a turned scan). Retry the
    # other orientations within one extra timeout. A retry replaces the first
    # reading only with more confident words that native text lacks; competing
    # readings are never merged, and a failed retry keeps the best completed reading.
    selected, best = reading, _confident_new_words(reading, native_words)
    deadline = time.monotonic() + OCR_TIMEOUT_SECONDS
    for degrees in OCR_REORIENT_DEGREES:
        if time.monotonic() >= deadline:
            break
        rotated = _rotate_pgm(rendered.stdout, degrees)
        # Rotation consumes the shared budget; never launch after it expires.
        remaining = deadline - time.monotonic()
        if rotated is None or remaining <= 0:
            break
        try:
            candidate = _tesseract_reading(rotated, language, page_segmentation_mode, remaining, environment)
        except (_OcrStop, subprocess.TimeoutExpired, OSError, UnicodeDecodeError):
            break
        score = _confident_new_words(candidate, native_words)
        if score <= best or not any(character.isalnum() for character in candidate.text):
            continue
        selected, best = candidate, score
        # A confident fragment is not evidence of complete recognition. Evaluate
        # the remaining orientations while the shared deadline permits it.
    return PdfOcrResult(text=selected.text, status="recognized")


def _combine_pdf_text(native: str, recognized: str) -> str:
    """Keep native bytes of derived text; append distinct recognized lines."""
    if not native:
        return recognized.strip()

    def normalized(value: str) -> str:
        return " ".join(value.casefold().split())

    if normalized(native) == normalized(recognized):
        return native
    # Set membership keeps merging linear in bounded input size, including
    # OCR output containing many short lines. Do not scan the whole native
    # page once for every recognized line.
    existing = {normalized(line) for line in native.splitlines()}
    additions: list[str] = []
    for line in recognized.splitlines():
        line = line.strip()
        key = normalized(line)
        if not key or key in existing:
            continue
        additions.append(line)
        existing.add(key)
    return native + ("\n\n" + "\n".join(additions) if additions else "")


@dataclass(frozen=True)
class _MediaProbe:
    duration_ms: int
    has_video: bool
    browser_compatible: bool
    has_audio: bool = True
    audio_track_count: int = 1
    video_codec: str = ""
    audio_codec: str = ""
    pixel_format: str = ""

    @property
    def playback_method(self) -> str:
        if not self.has_video or self.browser_compatible:
            return "original"
        if self.video_codec == "h264" and self.pixel_format in {
            "yuv420p",
            "yuvj420p",
        }:
            if self.audio_codec in {"aac", "mp3", ""}:
                return "remux"
            return "audio_only"
        return "full_transcode"


def _queued_playback_message(probe: _MediaProbe) -> str:
    if not probe.has_video:
        return "The original recording is ready to play."
    if probe.playback_method == "original":
        return "The original video is ready without conversion."
    if probe.playback_method == "remux":
        return "Queued to prepare browser playback without re-encoding video."
    if probe.playback_method == "audio_only":
        return "Queued to convert only the audio track for browser playback."
    return "Queued to convert video for browser playback."


def _processing_playback_message(method: str) -> str:
    if method == "remux":
        return "Preparing browser playback without re-encoding video."
    if method == "audio_only":
        return "Converting only the audio track for browser playback."
    return "Converting video for browser playback."


def _ready_playback_message(method: str) -> str:
    if method == "remux":
        return "Browser playback is ready without re-encoding video."
    if method == "audio_only":
        return "Browser playback is ready; only the audio track was converted."
    return "Browser playback is ready after video conversion."


def _browser_playback_command(
    method: str,
    source: Path,
    destination: Path,
    maximum_output_bytes: int,
) -> list[str]:
    command = [
        "/usr/bin/ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
    ]
    if method == "remux":
        command.extend(["-c:v", "copy", "-c:a", "copy"])
    elif method == "audio_only":
        command.extend(
            [
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ac",
                "2",
            ]
        )
    elif method == "full_transcode":
        command.extend(
            [
                "-vf",
                (
                    "scale=w='min(1920,iw)':h='min(1080,ih)':"
                    "force_original_aspect_ratio=decrease:force_divisible_by=2"
                ),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "24",
                "-maxrate",
                "6000k",
                "-bufsize",
                "12000k",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ac",
                "2",
            ]
        )
    else:
        raise ValueError("unsupported browser playback method")
    command.extend(
        [
            "-movflags",
            "+faststart",
            "-avoid_negative_ts",
            "make_zero",
            "-threads",
            "2",
            "-fs",
            str(max(int(maximum_output_bytes), 1)),
            "-f",
            "mp4",
            str(destination),
        ]
    )
    return command


def _probe_media(source: Path, media_type: str) -> _MediaProbe:
    """Return bounded technical media facts and a conservative browser decision."""

    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    try:
        completed = subprocess.run(
            [
                "/usr/bin/ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,codec_name,pix_fmt",
                "-of",
                "json",
                "--",
                str(source),
            ],
            capture_output=True,
            check=False,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UploadProblem("That recording could not be inspected safely.") from exc
    if completed.returncode != 0 or len(completed.stdout) > 256 * 1024:
        raise UploadProblem("That file is not readable audio or video media.")
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
        streams = payload.get("streams") or []
        has_audio = any(item.get("codec_type") == "audio" for item in streams)
        has_video = any(item.get("codec_type") == "video" for item in streams)
        duration = float((payload.get("format") or {}).get("duration") or 0)
    except (AttributeError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UploadProblem("That file is not readable audio or video media.") from exc
    if not (has_audio or has_video) or not math.isfinite(duration) or duration <= 0:
        raise UploadProblem("Media must contain a readable audio track.")
    if duration > 12 * 60 * 60:
        raise UploadProblem("Recordings longer than 12 hours are not supported.", 413)
    browser_compatible = True
    if has_video:
        video_stream = next(
            (item for item in streams if item.get("codec_type") == "video"), {}
        )
        audio_stream = next(
            (item for item in streams if item.get("codec_type") == "audio"), {}
        )
        video_codec = str(video_stream.get("codec_name") or "").casefold()
        audio_codec = str(audio_stream.get("codec_name") or "").casefold()
        pixel_format = str(video_stream.get("pix_fmt") or "").casefold()
        if media_type == "video/mp4":
            browser_compatible = (
                video_codec == "h264"
                and (not has_audio or audio_codec in {"aac", "mp3"})
                and pixel_format in {"yuv420p", "yuvj420p"}
            )
        elif media_type == "video/webm":
            browser_compatible = video_codec in {"vp8", "vp9", "av1"} and (not has_audio or audio_codec in {
                "opus",
                "vorbis",
            })
        else:
            # MOV commonly contains HEVC, ProRes, PCM, or another combination
            # that Chromium cannot decode. Prepare a predictable MP4 rendition.
            browser_compatible = False
    return _MediaProbe(
        duration_ms=max(1, round(duration * 1000)),
        has_video=has_video,
        has_audio=has_audio,
        audio_track_count=sum(item.get("codec_type") == "audio" for item in streams),
        browser_compatible=browser_compatible,
        video_codec=video_codec if has_video else "",
        audio_codec=audio_codec if has_video else "",
        pixel_format=pixel_format if has_video else "",
    )


@dataclass(frozen=True)
class PilotUnit:
    number: int
    text: str
    line_start: int | None = None
    line_end: int | None = None
    excerpt_digest: str = ""
    start_ms: int | None = None
    end_ms: int | None = None
    speaker_cluster: str = ""
    location_label: str = ""

    @property
    def location(self) -> str:
        if self.location_label:
            return self.location_label
        if self.line_start is not None:
            end = self.line_end or self.line_start
            return f"Lines {self.line_start}–{end}" if end != self.line_start else f"Line {self.line_start}"
        return f"Page {self.number}"


@dataclass(frozen=True)
class _LinkedSourceRegistration:
    relative_path: str
    display_name: str
    media_type: str
    byte_size: int
    stable_device: int
    stable_inode: int
    stable_mtime_ns: int


@dataclass
class PilotDocument:
    document_id: str
    display_name: str
    stored_name: str
    media_type: str
    size: int
    state: str
    message: str
    units: list[dict]
    digest: str = ""
    version_id: str = ""
    name_key: str = ""
    page_count: int = 0
    units_file: str = ""
    origin: str = "upload"
    source_location_id: str = ""
    relative_path: str = ""
    stable_device: int = 0
    stable_inode: int = 0
    stable_mtime_ns: int = 0
    processing_stage: str = ""
    completed_units: int = 0
    total_units: int = 0
    duration_ms: int = 0
    has_video: bool = False
    playback_name: str = ""
    playback_media_type: str = ""
    playback_size: int = 0
    playback_state: str = ""
    playback_message: str = ""
    _units_loader: Callable[[str], tuple[PilotUnit, ...]] | None = field(
        default=None, repr=False, compare=False
    )

    _units_iterator: Callable | None = field(default=None, repr=False, compare=False)

    def iter_parsed_units(self, *, budget_check=None, read_check=None, max_record_chars=None):
        """Stream extracted units; budgeted callers never use a whole-file loader."""
        if self.units:
            for unit in self.units:
                if budget_check is not None:
                    budget_check()
                yield PilotUnit(**unit)
            if budget_check is not None:
                budget_check()
        elif self.units_file and self._units_iterator is not None:
            kwargs = {}
            if budget_check is not None:
                kwargs['budget_check'] = budget_check
            if read_check is not None:
                kwargs['read_check'] = read_check
            if max_record_chars is not None:
                kwargs['max_record_chars'] = max_record_chars
            yield from self._units_iterator(self.units_file, **kwargs)
        elif self.units_file and self._units_loader is not None:
            if budget_check is not None or read_check is not None or max_record_chars is not None:
                raise RuntimeError("bounded derived text reader is unavailable")
            yield from self._units_loader(self.units_file)
        elif self.units_file:
            raise RuntimeError("derived text reader is unavailable")

    def parsed_units(self) -> tuple[PilotUnit, ...]:
        if self.units:
            return tuple(PilotUnit(**unit) for unit in self.units)
        if self.units_file and self._units_loader is not None:
            return self._units_loader(self.units_file)
        return ()


@dataclass(frozen=True)
class PilotStoreChange:
    """One committed source-registry delta for downstream projections."""

    upserted: tuple[PilotDocument, ...] = ()
    removed_document_ids: tuple[str, ...] = ()


_PILOT_DOCUMENT_FIELDS = (
    "document_id",
    "display_name",
    "stored_name",
    "media_type",
    "size",
    "state",
    "message",
    "units",
    "digest",
    "version_id",
    "name_key",
    "page_count",
    "units_file",
    "origin",
    "source_location_id",
    "relative_path",
    "stable_device",
    "stable_inode",
    "stable_mtime_ns",
    "processing_stage",
    "completed_units",
    "total_units",
    "duration_ms",
    "has_video",
    "playback_name",
    "playback_media_type",
    "playback_size",
    "playback_state",
    "playback_message",
)
_SOURCE_REGISTRY_MARKER = {
    "version": 5,
    "storage": "source-registry.sqlite3",
    "schema_version": 1,
}


class PilotStore:
    """Transactional per-matter source registry with atomic legacy migration."""

    def __init__(
        self,
        root: Path,
        *,
        document_file_limit: int = MAX_FILE_BYTES,
        media_file_limit: int = MAX_MEDIA_FILE_BYTES,
        upload_session_limit: int = MAX_UPLOAD_SESSION_BYTES,
        on_change: Callable[[PilotStoreChange], None] | None = None,
        mutation_lock: threading.RLock | None = None,
        malware_scanner: MalwareScanner | None = None,
        malware_scan_mode: str = "extended",
    ) -> None:
        self._lock = mutation_lock or threading.RLock()
        self._retired = False
        self._registry: sqlite3.Connection | None = None
        self.document_file_limit = int(document_file_limit)
        self.media_file_limit = int(media_file_limit)
        self.upload_session_limit = int(upload_session_limit)
        self._on_change = on_change
        self.malware_scanner = malware_scanner
        self.malware_scan_mode = malware_scan_mode.strip().casefold()
        if self.malware_scan_mode not in {"disabled", "extended", "all"}:
            raise ValueError("invalid malware scan mode")
        if (
            min(
                self.document_file_limit,
                self.media_file_limit,
                self.upload_session_limit,
            )
            <= 0
            or max(self.document_file_limit, self.media_file_limit)
            > self.upload_session_limit
        ):
            raise ValueError("pilot upload limits are invalid")
        self.root = Path(root)
        if self.root.exists() and self.root.is_symlink():
            raise RuntimeError("pilot storage cannot be a symbolic link")
        self.files = self.root / "files"
        self.staging = self.root / "staging"
        self.incoming = self.root / "incoming"
        self.derived = self.root / "derived"
        self.quarantine_root = self.root / "quarantine"
        self.root.mkdir(parents=True, exist_ok=True)
        self.files.mkdir(exist_ok=True)
        self.staging.mkdir(exist_ok=True)
        self.incoming.mkdir(exist_ok=True)
        self.derived.mkdir(exist_ok=True)
        self.quarantine_root.mkdir(exist_ok=True)
        for directory in (
            self.files,
            self.staging,
            self.incoming,
            self.derived,
            self.quarantine_root,
        ):
            if directory.is_symlink() or not directory.is_dir():
                raise RuntimeError("pilot storage directory is unsafe")
        self._reconcile_staging()
        self.manifest_path = self.root / "manifest.json"
        self.registry_path = self.root / "source-registry.sqlite3"
        self.documents: dict[str, PilotDocument] = {}
        self._load()

    @contextmanager
    def mutation_guard(self):
        """Share the source mutation boundary with durable result writers."""

        with self._lock:
            yield

    def _ensure_active(self) -> None:
        if self._retired:
            raise UploadProblem(
                "This matter is being deleted and no longer accepts source changes.",
                409,
            )

    def maximum_file_bytes(self, suffix: str) -> int:
        return maximum_file_bytes(
            suffix,
            document_limit=self.document_file_limit,
            media_limit=self.media_file_limit,
        )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def malware_scan_status(self) -> dict[str, object]:
        try:
            quarantined = sum(
                1
                for item in self.quarantine_root.iterdir()
                if item.is_file() and not item.is_symlink()
            )
        except OSError:
            quarantined = 0
        status = scanner_status(self.malware_scanner)
        return {
            "mode": self.malware_scan_mode,
            "available": status.ready,
            "state": status.state,
            "engine": status.engine,
            "engine_version": status.engine_version,
            "signature_version": status.signature_version,
            "signature_updated_at": status.signature_updated_at,
            "message": status.message,
            "quarantined": quarantined,
        }

    def _scan_required(self, suffix: str) -> bool:
        return self.malware_scan_mode == "all" or (
            self.malware_scan_mode == "extended" and suffix in EXTENDED_SOURCE_SUFFIXES
        )

    def _scan_staged(self, path: Path, suffix: str) -> None:
        if not self._scan_required(suffix):
            return
        if self.malware_scanner is None:
            raise UploadProblem(
                "Security scanning is temporarily unavailable. This file was not added.",
                503,
            )
        result = self.malware_scanner.scan(path)
        if result.state == "clean":
            return
        if result.state == "infected":
            destination = self.quarantine_root / f"{uuid.uuid4().hex}.blocked"
            try:
                os.replace(path, destination)
                os.chmod(destination, 0o600, follow_symlinks=False)
                self._fsync_directory(self.quarantine_root)
                self._fsync_directory(path.parent)
            except OSError as exc:
                raise UploadProblem(
                    "Unsafe content was detected, but isolation did not finish. "
                    "Ask an administrator to inspect managed storage.",
                    503,
                ) from exc
            raise UploadProblem(
                "This file was isolated because security scanning detected malware.",
                422,
            )
        raise UploadProblem(
            "Security scanning could not verify this file. This file was not added.",
            503,
        )

    def _reconcile_staging(self) -> None:
        directory_fd = os.open(self.staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with os.scandir(self.staging) as entries:
                for entry in entries:
                    if not _STAGE_NAME.fullmatch(entry.name):
                        continue
                    # unlink removes a link/FIFO itself and never follows it.
                    try:
                        os.unlink(entry.name, dir_fd=directory_fd)
                    except IsADirectoryError:
                        raise RuntimeError("pilot staging contains an unexpected directory")
        finally:
            os.close(directory_fd)
        self._fsync_directory(self.staging)

    @staticmethod
    def _document_payload(document: PilotDocument) -> dict[str, object]:
        return {name: getattr(document, name) for name in _PILOT_DOCUMENT_FIELDS}

    @staticmethod
    def _decode_document(payload: object) -> PilotDocument:
        if not isinstance(payload, dict):
            raise ValueError
        prepared = dict(payload)
        prepared.setdefault("units", [])
        prepared.setdefault("duration_ms", 0)
        prepared.setdefault("has_video", False)
        prepared.setdefault("playback_name", "")
        prepared.setdefault("playback_media_type", "")
        prepared.setdefault("playback_size", 0)
        prepared.setdefault("playback_state", "")
        prepared.setdefault("playback_message", "")
        document = PilotDocument(**prepared)
        if not re.fullmatch(r"[0-9a-f]{32}", document.document_id):
            raise ValueError
        return document

    @staticmethod
    def _configure_registry(
        connection: sqlite3.Connection, *, initialize: bool
    ) -> None:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA trusted_schema=OFF")
        if initialize:
            with connection:
                connection.execute(
                    "CREATE TABLE source_registry_metadata("
                    "key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID"
                )
                connection.execute(
                    "CREATE TABLE source_document("
                    "document_id TEXT PRIMARY KEY,ordinal INTEGER NOT NULL UNIQUE,"
                    "payload TEXT NOT NULL) WITHOUT ROWID"
                )
                connection.execute(
                    "INSERT INTO source_registry_metadata(key,value) VALUES ('schema_version','1')"
                )
        row = connection.execute(
            "SELECT value FROM source_registry_metadata WHERE key='schema_version'"
        ).fetchone()
        if row is None or row[0] != "1":
            raise RuntimeError("pilot source registry schema is unsupported")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("pilot source registry integrity check failed")

    def _open_registry(
        self, path: Path, *, initialize: bool = False
    ) -> sqlite3.Connection:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("pilot source registry is not a regular file")
        try:
            connection = sqlite3.connect(
                path,
                timeout=30,
                check_same_thread=False,
            )
            self._configure_registry(connection, initialize=initialize)
            return connection
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            try:
                connection.close()
            except (UnboundLocalError, sqlite3.Error):
                pass
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError("pilot source registry could not be opened") from exc

    def _create_registry_atomic(self, documents: Sequence[PilotDocument]) -> None:
        if self.registry_path.exists() or self.registry_path.is_symlink():
            raise RuntimeError("pilot source registry already exists")
        temporary = self.root / f".source-registry-{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        os.close(descriptor)
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open_registry(temporary, initialize=True)
            prepared = [
                (
                    document.document_id,
                    ordinal,
                    json.dumps(
                        self._document_payload(document),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
                for ordinal, document in enumerate(documents, 1)
            ]
            if prepared:
                with connection:
                    connection.executemany(
                        "INSERT INTO source_document(document_id,ordinal,payload) "
                        "VALUES (?,?,?)",
                        prepared,
                    )
            if connection.execute("SELECT COUNT(*) FROM source_document").fetchone()[0] != len(
                prepared
            ):
                raise RuntimeError("pilot source registry migration did not verify")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("pilot source registry migration did not verify")
            connection.close()
            connection = None
            descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, self.registry_path)
            self._fsync_directory(self.root)
            self._registry = self._open_registry(self.registry_path)
        finally:
            if connection is not None:
                connection.close()
            temporary.unlink(missing_ok=True)

    def _write_manifest_marker(self) -> None:
        if self.manifest_path.is_symlink():
            raise RuntimeError("pilot manifest is not a regular file")
        encoded = json.dumps(
            _SOURCE_REGISTRY_MARKER, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if self.manifest_path.exists():
            if not self.manifest_path.is_file():
                raise RuntimeError("pilot manifest is not a regular file")
            try:
                if self.manifest_path.read_bytes() == encoded:
                    return
            except OSError as exc:
                raise RuntimeError("pilot manifest could not be loaded") from exc
        temporary = self.root / f".manifest-{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.manifest_path)
            self._fsync_directory(self.root)
        finally:
            temporary.unlink(missing_ok=True)

    def _registry_documents(self) -> list[PilotDocument]:
        if self._registry is None:
            raise RuntimeError("pilot source registry is unavailable")
        try:
            rows = self._registry.execute(
                "SELECT document_id,payload FROM source_document ORDER BY ordinal"
            ).fetchall()
            loaded: list[PilotDocument] = []
            for document_id, encoded in rows:
                document = self._decode_document(json.loads(encoded))
                if document.document_id != document_id:
                    raise ValueError
                loaded.append(document)
            return loaded
        except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("pilot source registry could not be loaded") from exc

    def _persist_registry_delta(
        self,
        documents: Sequence[PilotDocument],
        removed_document_ids: Sequence[str] = (),
    ) -> None:
        if self._registry is None:
            raise RuntimeError("pilot source registry is unavailable")
        for document_id in removed_document_ids:
            if not re.fullmatch(r"[0-9a-f]{32}", document_id):
                raise RuntimeError("pilot source registry removal is invalid")
        try:
            with self._registry:
                ordinal = int(
                    self._registry.execute(
                        "SELECT COALESCE(MAX(ordinal),0) FROM source_document"
                    ).fetchone()[0]
                )
                for document in documents:
                    ordinal += 1
                    self._registry.execute(
                        "INSERT INTO source_document(document_id,ordinal,payload) VALUES (?,?,?) "
                        "ON CONFLICT(document_id) DO UPDATE SET payload=excluded.payload",
                        (
                            document.document_id,
                            ordinal,
                            json.dumps(
                                self._document_payload(document),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        ),
                    )
                if removed_document_ids:
                    self._registry.executemany(
                        "DELETE FROM source_document WHERE document_id=?",
                        [(document_id,) for document_id in removed_document_ids],
                    )
        except sqlite3.Error as exc:
            raise RuntimeError("pilot source registry could not be saved") from exc

    def _load(self) -> None:
        legacy = False
        loaded: list[PilotDocument]
        manifest_payload: object | None = None
        if self.manifest_path.exists() or self.manifest_path.is_symlink():
            if self.manifest_path.is_symlink() or not self.manifest_path.is_file():
                raise RuntimeError("pilot manifest is not a regular file")
            try:
                manifest_payload = json.loads(
                    self.manifest_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError("pilot manifest could not be loaded") from exc

        if self.registry_path.exists() or self.registry_path.is_symlink():
            self._registry = self._open_registry(self.registry_path)
            loaded = self._registry_documents()
            if manifest_payload is not None:
                if not isinstance(manifest_payload, dict) or manifest_payload.get(
                    "version"
                ) not in {2, 3, 4, 5}:
                    raise RuntimeError("pilot manifest could not be loaded")
                if manifest_payload.get("version") == 5 and (
                    manifest_payload != _SOURCE_REGISTRY_MARKER
                ):
                    raise RuntimeError("pilot manifest could not be loaded")
        elif manifest_payload is not None:
            if not isinstance(manifest_payload, dict):
                raise RuntimeError("pilot manifest could not be loaded")
            if manifest_payload.get("version") == 5:
                raise RuntimeError("pilot source registry is unavailable")
            if manifest_payload.get("version") not in {2, 3, 4} or not isinstance(
                manifest_payload.get("documents"), list
            ):
                raise RuntimeError("pilot manifest could not be loaded")
            try:
                loaded = [
                    self._decode_document(item)
                    for item in manifest_payload["documents"]
                ]
            except (TypeError, ValueError) as exc:
                raise RuntimeError("pilot manifest could not be loaded") from exc
            legacy = True
        else:
            loaded = []

        changed: list[PilotDocument] = []
        for document in loaded:
            before = self._document_payload(document)
            document._units_loader = self._load_units
            document._units_iterator = self._iter_units
            if document.origin == "upload":
                path = self.files / document.stored_name
                if path.parent != self.files or path.is_symlink() or not path.is_file():
                    document.state = "missing"
                    document.message = "Stored file is unavailable. Ask the administrator to restore it, then restart the workbench."
                    document.units = []
                    document.units_file = ""
                elif not self._stored_file_matches(path, document):
                    document.state = "changed"
                    document.message = "Stored file changed after processing. Remove it and upload the intended file again."
                    document.units = []
                    document.units_file = ""
            elif document.origin != "registered":
                raise RuntimeError("pilot source registry contains an invalid source origin")
            if document.units_file and not self._units_file_is_safe(document.units_file):
                document.state = "failed"
                document.message = "Derived searchable text is unavailable. Choose Try again."
                document.units_file = ""
            if not document.has_video:
                document.playback_name = ""
                document.playback_media_type = document.media_type
                document.playback_size = document.size
                document.playback_state = "original"
                document.playback_message = "The original recording is ready to play."
            elif document.playback_state == "processing":
                document.playback_state = "queued"
                document.playback_message = "Browser playback preparation will resume."
            elif document.playback_state == "ready" and not self._playback_file_is_safe(
                document
            ):
                document.playback_name = ""
                document.playback_media_type = ""
                document.playback_size = 0
                document.playback_state = "queued"
                document.playback_message = "Browser playback preparation will resume."
            if document.document_id in self.documents:
                raise RuntimeError("pilot source registry contains duplicate source identities")
            self.documents[document.document_id] = document
            if before != self._document_payload(document):
                changed.append(document)
        self._reconcile_files()
        self._reconcile_derived()

        if legacy:
            for document in loaded:
                self._write_units(document)
            self._create_registry_atomic(tuple(loaded))
        elif self._registry is None:
            self._create_registry_atomic(())
        elif changed:
            self._persist_registry_delta(tuple(changed))
        self._write_manifest_marker()

    @staticmethod
    def _stored_file_matches(path: Path, document: PilotDocument) -> bool:
        if is_media_type(document.media_type) and document.stable_mtime_ns:
            try:
                metadata = path.stat(follow_symlinks=False)
            except OSError:
                return False
            return (
                stat.S_ISREG(metadata.st_mode)
                and metadata.st_size == document.size
                and metadata.st_mtime_ns == document.stable_mtime_ns
            )
        digest = hashlib.sha256()
        size = 0
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return False
                while chunk := stream.read(CHUNK_BYTES):
                    size += len(chunk)
                    digest.update(chunk)
        except OSError:
            return False
        return size == document.size and digest.hexdigest() == document.digest

    def _reconcile_files(self) -> None:
        referenced = {item.stored_name for item in self.documents.values() if item.stored_name}
        directory_fd = os.open(self.files, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with os.scandir(self.files) as entries:
                for entry in entries:
                    if re.fullmatch(r"[0-9a-f]{32}", entry.name) and entry.name not in referenced:
                        os.unlink(entry.name, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        self._fsync_directory(self.files)

    def _units_file_is_safe(self, name: str) -> bool:
        if not re.fullmatch(r"[0-9a-f]{32}\.json", name):
            return False
        path = self.derived / name
        return path.parent == self.derived and path.is_file() and not path.is_symlink()

    def _playback_file_is_safe(self, document: PilotDocument) -> bool:
        expected = f"{document.document_id}.playback.mp4"
        if document.playback_name != expected or document.playback_media_type != "video/mp4":
            return False
        path = self.derived / document.playback_name
        if path.parent != self.derived or path.is_symlink():
            return False
        try:
            metadata = path.stat(follow_symlinks=False)
        except OSError:
            return False
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_size > 0
            and metadata.st_size == document.playback_size
        )

    def _iter_units(self, name: str, **kwargs):
        from .unit_stream import iter_unit_records, UnitRecordLimit
        if not self._units_file_is_safe(name):
            raise RuntimeError("derived searchable text is unavailable")
        try:
            fd = os.open(self.derived / name, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, encoding="utf-8") as stream:
                for item in iter_unit_records(stream, **kwargs):
                    yield PilotUnit(**item)
        except UnitRecordLimit:
            raise
        except (OSError, TypeError, ValueError) as exc:
            if kwargs:
                # Budgeted callers own their failure taxonomy; preserve the
                # exact callback exception instead of calling it a read error.
                raise
            raise RuntimeError("derived searchable text could not be loaded") from exc

    def _load_units(self, name: str) -> tuple[PilotUnit, ...]:
        if not self._units_file_is_safe(name):
            raise RuntimeError("derived searchable text is unavailable")
        try:
            payload = json.loads((self.derived / name).read_text(encoding="utf-8"))
            if payload.get("version") != 1 or not isinstance(payload.get("units"), list):
                raise ValueError
            return tuple(PilotUnit(**item) for item in payload["units"])
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("derived searchable text could not be loaded") from exc

    def _write_units(self, document: PilotDocument) -> None:
        if not document.units:
            return
        name = f"{document.document_id}.json"
        temporary = self.derived / f".{document.document_id}-{uuid.uuid4().hex}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(
                    {"version": 1, "units": document.units},
                    stream,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.derived / name)
            self._fsync_directory(self.derived)
            document.units_file = name
            document.units = []
        finally:
            temporary.unlink(missing_ok=True)

    def _drop_units(self, document: PilotDocument) -> None:
        document.units = []
        if document.units_file and re.fullmatch(r"[0-9a-f]{32}\.json", document.units_file):
            (self.derived / document.units_file).unlink(missing_ok=True)
            self._fsync_directory(self.derived)
        document.units_file = ""

    def _reconcile_derived(self) -> None:
        referenced = {
            name
            for item in self.documents.values()
            for name in (item.units_file, item.playback_name)
            if name
        }
        directory_fd = os.open(self.derived, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with os.scandir(self.derived) as entries:
                for entry in entries:
                    if re.fullmatch(r"[0-9a-f]{32}\.json", entry.name) and entry.name not in referenced:
                        os.unlink(entry.name, dir_fd=directory_fd)
                    elif (
                        re.fullmatch(r"[0-9a-f]{32}\.playback\.mp4", entry.name)
                        and entry.name not in referenced
                    ):
                        os.unlink(entry.name, dir_fd=directory_fd)
                    elif re.fullmatch(r"\.[0-9a-f]{32}-[0-9a-f]{32}\.tmp", entry.name):
                        os.unlink(entry.name, dir_fd=directory_fd)
                    elif re.fullmatch(
                        r"\.[0-9a-f]{32}-playback-[0-9a-f]{32}\.tmp\.mp4",
                        entry.name,
                    ):
                        os.unlink(entry.name, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        self._fsync_directory(self.derived)

    def _save(
        self,
        document_ids: Sequence[str] | None = None,
        *,
        removed_document_ids: Sequence[str] = (),
    ) -> None:
        self._ensure_active()
        selected_ids = (
            tuple(self.documents)
            if document_ids is None
            else tuple(dict.fromkeys(document_ids))
        )
        selected: list[PilotDocument] = []
        for document_id in selected_ids:
            document = self.documents.get(document_id)
            if document is None:
                raise RuntimeError("pilot source registry update is invalid")
            self._write_units(document)
            selected.append(document)
        removed = tuple(dict.fromkeys(removed_document_ids))
        if set(selected_ids) & set(removed):
            raise RuntimeError("pilot source registry delta is invalid")
        self._persist_registry_delta(tuple(selected), removed)
        if self._on_change is not None:
            self._on_change(PilotStoreChange(tuple(selected), removed))

    @staticmethod
    def validate_name(name: str) -> tuple[str, str, str]:
        normalized = unicodedata.normalize("NFC", name or "")
        bad = "Each file must have a normal filename without folders or special path characters."
        if not normalized or not normalized.strip() or len(normalized.encode("utf-8")) > MAX_DISPLAY_NAME_BYTES:
            raise UploadProblem(bad)
        if normalized != normalized.rstrip(" .") or normalized in {".", ".."}:
            raise UploadProblem(bad)
        windows = PureWindowsPath(normalized)
        if windows.is_absolute() or windows.drive or normalized.startswith(("/", "\\")):
            raise UploadProblem(bad)
        if any(character in normalized for character in "/\\:"):
            raise UploadProblem(bad)
        if any(unicodedata.category(character) in {"Cc", "Cf"} or character in _BIDI for character in normalized):
            raise UploadProblem(bad)
        stem = normalized.split(".", 1)[0].rstrip(" .").upper()
        if stem in _RESERVED:
            raise UploadProblem(bad)
        suffix = Path(normalized).suffix.casefold()
        if suffix not in _ALLOWED:
            raise UploadProblem(
                "Supported sources are PDF, DOCX, UTF-8 TXT, JPEG, PNG, TIFF, "
                "EML, CSV, TSV, XLSX, WAV, MP3, M4A, OGG, Opus, MP4, MOV, and WebM."
            )
        key = normalized.rstrip(" .").casefold()
        return normalized, suffix, key

    @classmethod
    def validate_relative_upload_path(cls, value: str) -> tuple[str, str, str, str]:
        normalized = unicodedata.normalize("NFC", value or "")
        bad = "Each selected file must have a safe relative folder and filename."
        if (
            not normalized
            or len(normalized.encode("utf-8")) > 2_048
            or normalized.startswith(("/", "\\"))
            or "\\" in normalized
            or ":" in normalized
        ):
            raise UploadProblem(bad)
        path = PurePosixPath(normalized)
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise UploadProblem(bad)
        for part in path.parts[:-1]:
            if (
                not part.strip()
                or len(part.encode("utf-8")) > MAX_DISPLAY_NAME_BYTES
                or part != part.rstrip(" .")
                or any(
                    unicodedata.category(character) in {"Cc", "Cf"} or character in _BIDI
                    for character in part
                )
                or part.rstrip(" .").upper() in _RESERVED
            ):
                raise UploadProblem(bad)
        filename, suffix, _ = cls.validate_name(path.name)
        canonical = "/".join((*path.parts[:-1], filename))
        return canonical, filename, suffix, f"upload:{canonical.casefold()}"

    @staticmethod
    def _validate_file_signature(suffix: str, first: bytes) -> None:
        if suffix == ".pdf" and not first.startswith(b"%PDF-"):
            raise UploadProblem("That file is not a valid PDF.")
        if suffix in {".docx", ".xlsx"} and not first.startswith(b"PK\x03\x04"):
            raise UploadProblem(f"That file is not a valid {suffix[1:].upper()}.")
        if suffix in {".jpg", ".jpeg"} and not first.startswith(b"\xff\xd8\xff"):
            raise UploadProblem("That file is not a valid JPEG image.")
        if suffix == ".png" and not first.startswith(b"\x89PNG\r\n\x1a\n"):
            raise UploadProblem("That file is not a valid PNG image.")
        if suffix in {".tif", ".tiff"} and not first.startswith(
            (b"II*\x00", b"MM\x00*")
        ):
            raise UploadProblem("That file is not a valid TIFF image.")
        if suffix in {".txt", ".csv", ".tsv"} and first.startswith(
            (b"\xff\xfe", b"\xfe\xff", b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")
        ):
            raise UploadProblem("Text and delimited files must be valid UTF-8.")

    def store_stream(
        self,
        display_name: str,
        content_type: str | None,
        stream: BinaryIO,
        *,
        relative_path: str | None = None,
        request_used: int = 0,
        retain_extraction_failure: bool = False,
        defer_processing: bool = False,
        request_limit: int = MAX_REQUEST_BYTES,
    ) -> tuple[PilotDocument, int]:
        with self._lock:
            self._ensure_active()
            return self._store_stream_locked(
                display_name,
                content_type,
                stream,
                relative_path=relative_path,
                request_used=request_used,
                retain_extraction_failure=retain_extraction_failure,
                defer_processing=defer_processing,
                request_limit=request_limit,
            )

    def _store_stream_locked(
        self,
        display_name: str,
        content_type: str | None,
        stream: BinaryIO,
        *,
        relative_path: str | None = None,
        request_used: int = 0,
        retain_extraction_failure: bool = False,
        defer_processing: bool = False,
        request_limit: int = MAX_REQUEST_BYTES,
    ) -> tuple[PilotDocument, int]:
        display_name, suffix, _ = self.validate_name(display_name)
        upload_path, filename, path_suffix, name_key = self.validate_relative_upload_path(
            relative_path or display_name
        )
        if filename != display_name or path_suffix != suffix:
            raise UploadProblem("The selected filename does not match its relative path.")
        expected_type = _ALLOWED[suffix]
        normalized_type = (content_type or "").split(";", 1)[0].strip().casefold()
        accepted_types = _MEDIA_TYPE_ALIASES.get(suffix, {expected_type})
        if normalized_type not in accepted_types and not (
            suffix in MEDIA_TYPES and normalized_type in {"", "application/octet-stream"}
        ):
            raise UploadProblem("The selected file type does not match its filename.")
        stage_name = f".upload-{uuid.uuid4().hex}.part"
        stage = self.staging / stage_name
        fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        size = 0
        first = b""
        digest = hashlib.sha256()
        final: Path | None = None
        document_id = ""
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise UploadProblem("The upload could not be staged safely.")
            with os.fdopen(fd, "wb") as output:
                while True:
                    chunk = stream.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise UploadProblem("The upload could not be read safely.")
                    if not first:
                        first = chunk[:8]
                    size += len(chunk)
                    maximum = self.maximum_file_bytes(suffix)
                    if size > maximum:
                        label = format_bytes(maximum)
                        raise UploadProblem(
                            f"Each {('recording' if suffix in MEDIA_TYPES else 'file')} "
                            f"must be {label} or smaller.",
                            413,
                        )
                    if request_used + size > request_limit:
                        raise UploadProblem(
                            f"The selected files must total {format_bytes(request_limit)} or less.",
                            413,
                        )
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size == 0:
                raise UploadProblem("Empty files cannot be uploaded.")
            hexdigest = digest.hexdigest()
            collision = next(
                (
                    item
                    for item in self.documents.values()
                    if item.origin == "upload"
                    and (item.relative_path or item.display_name).casefold()
                    == upload_path.casefold()
                ),
                None,
            )
            if collision is not None:
                if collision.digest == hexdigest:
                    return collision, request_used + size
                raise UploadProblem("A different file already uses that name. Rename this file and upload it again.", 409)
            self._scan_staged(stage, suffix)
            self._validate_file_signature(suffix, first)
            duration_ms = 0
            has_video = False
            browser_compatible = True
            media_probe: _MediaProbe | None = None
            if suffix in MEDIA_TYPES:
                media_probe = _probe_media(stage, expected_type)
                duration_ms = media_probe.duration_ms
                has_video = media_probe.has_video
                browser_compatible = media_probe.browser_compatible
            document_id = uuid.uuid4().hex
            version_id = uuid.uuid4().hex
            stored_name = uuid.uuid4().hex
            document = PilotDocument(
                document_id=document_id,
                display_name=upload_path,
                stored_name=stored_name,
                media_type=expected_type,
                size=size,
                state="queued" if defer_processing else "processing",
                message="Queued for processing" if defer_processing else "Processing…",
                units=[],
                digest=hexdigest,
                version_id=version_id,
                name_key=name_key,
                relative_path=upload_path,
                processing_stage=(
                    "Queued to check recording"
                    if suffix in MEDIA_TYPES
                    else ("Queued" if defer_processing else "Extracting text")
                ),
                duration_ms=duration_ms,
                has_video=has_video,
                playback_media_type=expected_type if not has_video or browser_compatible else "",
                playback_size=size if not has_video or browser_compatible else 0,
                playback_state=("original" if not has_video or browser_compatible else "queued"),
                playback_message=(
                    _queued_playback_message(media_probe)
                    if media_probe is not None
                    else "Queued to prepare browser-compatible playback."
                ),
                _units_loader=self._load_units,
                _units_iterator=self._iter_units,
            )
            if suffix in MEDIA_TYPES:
                document.state = "queued"
                document.message = "Queued to check recording"
            elif not defer_processing:
                try:
                    self._extract(document, stage)
                except UploadProblem as exc:
                    if not retain_extraction_failure:
                        raise
                    document.state = "failed"
                    document.message = str(exc)
                    document.units = []
                    document.page_count = 0
            final = self.files / stored_name
            final_fd = os.open(final, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(final_fd).st_mode):
                    raise UploadProblem("The upload could not be finalized safely.")
            finally:
                os.close(final_fd)
            os.replace(stage, final)
            self._fsync_directory(self.files)
            metadata = final.stat(follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != size:
                raise UploadProblem("The upload could not be finalized safely.")
            document.stable_device = int(metadata.st_dev)
            document.stable_inode = int(metadata.st_ino)
            document.stable_mtime_ns = int(metadata.st_mtime_ns)
            self.documents[document_id] = document
            self._save((document_id,))
            return document, request_used + size
        except Exception:
            if document_id:
                self.documents.pop(document_id, None)
            if final is not None:
                final.unlink(missing_ok=True)
            raise
        finally:
            stage.unlink(missing_ok=True)
            self._fsync_directory(self.staging)

    @staticmethod
    def _resumable_name(item_id: str) -> str:
        match = _UPLOAD_ITEM_ID.fullmatch(item_id or "")
        if match is None:
            raise UploadProblem("The upload item is invalid.")
        return f".session-{match.group(1)}.part"

    def resumable_size(self, item_id: str, *, expected_size: int) -> int:
        if expected_size <= 0 or expected_size > self.upload_session_limit:
            raise UploadProblem(
                f"Each upload item must be {format_bytes(self.upload_session_limit)} or smaller.",
                413,
            )
        name = self._resumable_name(item_id)
        path = self.incoming / name
        with self._lock:
            self._ensure_active()
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            except FileNotFoundError:
                return 0
            except OSError as exc:
                raise UploadProblem("The saved upload could not be resumed safely.", 409) from exc
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > expected_size:
                    raise UploadProblem("The saved upload could not be resumed safely.", 409)
                return int(metadata.st_size)
            finally:
                os.close(descriptor)

    def append_resumable_chunk(
        self,
        item_id: str,
        *,
        offset: int,
        expected_size: int,
        chunk: bytes,
    ) -> int:
        if not isinstance(chunk, bytes) or not chunk:
            raise UploadProblem("The upload chunk is empty.")
        if len(chunk) > MAX_UPLOAD_CHUNK_BYTES:
            raise UploadProblem("Each upload chunk must be 2 MiB or smaller.", 413)
        if expected_size <= 0 or expected_size > self.upload_session_limit:
            raise UploadProblem(
                f"Each upload item must be {format_bytes(self.upload_session_limit)} or smaller.",
                413,
            )
        if offset < 0 or offset + len(chunk) > expected_size:
            raise UploadProblem("The upload chunk does not match the expected file size.", 409)
        name = self._resumable_name(item_id)
        path = self.incoming / name
        with self._lock:
            self._ensure_active()
            flags = os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
            try:
                descriptor = os.open(path, flags, 0o600)
            except OSError as exc:
                raise UploadProblem("The upload could not be staged safely.") from exc
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != offset:
                    raise UploadProblem(
                        "The upload offset changed. Refresh its saved status and resume.",
                        409,
                    )
                os.lseek(descriptor, 0, os.SEEK_END)
                written = 0
                while written < len(chunk):
                    count = os.write(descriptor, chunk[written:])
                    if count <= 0:
                        raise UploadProblem("The upload chunk could not be saved safely.")
                    written += count
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._fsync_directory(self.incoming)
            return offset + len(chunk)

    def finalize_resumable_upload(
        self,
        item_id: str,
        *,
        display_name: str,
        relative_path: str,
        content_type: str,
        expected_size: int,
    ) -> PilotDocument:
        name = self._resumable_name(item_id)
        path = self.incoming / name
        with self._lock:
            self._ensure_active()
            if self.resumable_size(item_id, expected_size=expected_size) != expected_size:
                raise UploadProblem("Finish uploading this source before it is queued.", 409)
            display_name, suffix, _ = self.validate_name(display_name)
            upload_path, filename, path_suffix, name_key = self.validate_relative_upload_path(
                relative_path
            )
            if filename != display_name or path_suffix != suffix:
                raise UploadProblem("The selected filename does not match its relative path.")
            maximum = self.maximum_file_bytes(suffix)
            if expected_size > maximum:
                label = format_bytes(maximum)
                raise UploadProblem(
                    f"Each {('recording' if suffix in MEDIA_TYPES else 'file')} must be "
                    f"{label} or smaller.",
                    413,
                )
            expected_type = _ALLOWED[suffix]
            normalized_type = (content_type or "").split(";", 1)[0].strip().casefold()
            accepted_types = _MEDIA_TYPE_ALIASES.get(suffix, {expected_type})
            if normalized_type not in accepted_types and not (
                suffix in MEDIA_TYPES and normalized_type in {"", "application/octet-stream"}
            ):
                raise UploadProblem("The selected file type does not match its filename.")
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            except OSError as exc:
                raise UploadProblem("The completed upload could not be opened safely.") from exc
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
                    raise UploadProblem("The completed upload changed before finalization.", 409)
                digest = hashlib.sha256()
                first = b""
                used = 0
                while chunk := os.read(descriptor, CHUNK_BYTES):
                    if not first:
                        first = chunk[:8]
                    digest.update(chunk)
                    used += len(chunk)
                after = os.fstat(descriptor)
                if (
                    (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    or used != expected_size
                ):
                    raise UploadProblem("The completed upload changed during finalization.", 409)
            finally:
                os.close(descriptor)
            hexdigest = digest.hexdigest()
            occurrence_key = f"resumable:{item_id}"
            collision = next(
                (
                    item
                    for item in self.documents.values()
                    if item.origin == "upload"
                    and item.name_key == occurrence_key
                ),
                None,
            )
            if collision is not None:
                if (collision.digest == hexdigest and collision.relative_path == upload_path
                    and collision.size == expected_size and collision.media_type == expected_type):
                    return collision
                raise UploadProblem(
                    "The saved upload does not match this item. Review the selected files and try again.",
                    409,
                )
            # An older reader may have persisted the source hard link just
            # before interruption, without committing its control-store item.
            # The same inode and bytes prove that exact pending occurrence;
            # path/content equality alone cannot identify an earlier import.
            legacy = next((item for item in self.documents.values()
                if item.origin == "upload" and item.name_key == name_key
                and item.relative_path == upload_path and item.digest == hexdigest
                and (item.stable_device, item.stable_inode) == (before.st_dev, before.st_ino)), None)
            if legacy is not None:
                try:
                    linked = (self.files / legacy.stored_name).stat(follow_symlinks=False)
                except OSError:
                    legacy = None
                else:
                    if not stat.S_ISREG(linked.st_mode) or (linked.st_dev, linked.st_ino) != (before.st_dev, before.st_ino):
                        legacy = None
            if legacy is not None:
                legacy.name_key = occurrence_key
                try:
                    self._save((legacy.document_id,))
                except Exception:
                    legacy.name_key = name_key
                    raise
                return legacy
            self._scan_staged(path, suffix)
            self._validate_file_signature(suffix, first)
            duration_ms = 0
            has_video = False
            browser_compatible = True
            media_probe: _MediaProbe | None = None
            if suffix in MEDIA_TYPES:
                media_probe = _probe_media(path, expected_type)
                duration_ms = media_probe.duration_ms
                has_video = media_probe.has_video
                browser_compatible = media_probe.browser_compatible
            document_id = uuid.uuid4().hex
            stored_name = uuid.uuid4().hex
            document = PilotDocument(
                document_id=document_id,
                display_name=upload_path,
                stored_name=stored_name,
                media_type=expected_type,
                size=expected_size,
                state="queued",
                message=(
                    "Queued to check recording" if suffix in MEDIA_TYPES else "Queued for processing"
                ),
                units=[],
                digest=hexdigest,
                version_id=uuid.uuid4().hex,
                name_key=occurrence_key,
                relative_path=upload_path,
                processing_stage=("Queued to check recording" if suffix in MEDIA_TYPES else "Queued"),
                duration_ms=duration_ms,
                has_video=has_video,
                playback_media_type=expected_type if not has_video or browser_compatible else "",
                playback_size=expected_size if not has_video or browser_compatible else 0,
                playback_state=("original" if not has_video or browser_compatible else "queued"),
                playback_message=(
                    _queued_playback_message(media_probe)
                    if media_probe is not None
                    else "Queued to prepare browser-compatible playback."
                ),
                _units_loader=self._load_units,
                _units_iterator=self._iter_units,
            )
            final = self.files / stored_name
            try:
                try:
                    os.link(path, final, follow_symlinks=False)
                except OSError as exc:
                    if exc.errno not in {EXDEV, EPERM, EOPNOTSUPP, ENOSYS}:
                        raise
                    self._copy_resumable_source(path, final, expected_size, hexdigest)
                self._fsync_directory(self.files)
                metadata = final.stat(follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_size:
                    raise UploadProblem("The upload could not be finalized safely.")
                document.stable_device = int(metadata.st_dev)
                document.stable_inode = int(metadata.st_ino)
                document.stable_mtime_ns = int(metadata.st_mtime_ns)
                self.documents[document_id] = document
                self._save((document_id,))
                # The caller unlinks the incoming name only after the control
                # store commits. A hard link retains retry safety without a
                # second copy of large source bytes.
                return document
            except Exception:
                self.documents.pop(document_id, None)
                final.unlink(missing_ok=True)
                self._fsync_directory(self.files)
                raise

    @staticmethod
    def _copy_resumable_source(source: Path, destination: Path, expected_size: int, expected_digest: str) -> None:
        """Copy an already checked item without applying legacy path deduplication."""
        digest = hashlib.sha256()
        copied = 0
        with os.fdopen(os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as incoming:
            before = os.fstat(incoming.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
                raise UploadProblem('The completed upload changed before it could be saved.', 409)
            with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600), 'wb') as output:
                while chunk := incoming.read(CHUNK_BYTES):
                    copied += len(chunk)
                    if copied > expected_size:
                        raise UploadProblem('The completed upload changed while it was saved.', 409)
                    digest.update(chunk)
                    output.write(chunk)
                after = os.fstat(incoming.fileno())
                if (copied != expected_size or digest.hexdigest() != expected_digest
                    or (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
                    != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)):
                    raise UploadProblem('The completed upload changed while it was saved.', 409)
                output.flush()
                os.fsync(output.fileno())

    def discard_resumable_upload(self, item_id: str) -> None:
        name = self._resumable_name(item_id)
        path = self.incoming / name
        with self._lock:
            if path.is_symlink():
                raise RuntimeError("resumable upload staging is unsafe")
            path.unlink(missing_ok=True)
            self._fsync_directory(self.incoming)

    @staticmethod
    def _digest_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _read_regular_file(path: Path, maximum: int) -> bytes:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except OSError as exc:
            raise UploadProblem("The source could not be read safely.") from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
                raise UploadProblem("That text file contains too many characters.")
            chunks: list[bytes] = []
            used = 0
            while chunk := os.read(descriptor, min(CHUNK_BYTES, maximum + 1 - used)):
                chunks.append(chunk)
                used += len(chunk)
                if used > maximum:
                    raise UploadProblem("That text file contains too many characters.")
            after = os.fstat(descriptor)
            if (
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or used != before.st_size
            ):
                raise UploadProblem("The source changed while it was being read. Try again.")
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    @staticmethod
    def _expanded_ocr_limit() -> int:
        try:
            configured = int(os.getenv("CASE_INTELLIGENCE_MAX_OCR_PAGES", str(MAX_EXPANDED_OCR_PAGES)))
        except ValueError:
            configured = MAX_EXPANDED_OCR_PAGES
        return min(max(configured, 1), MAX_EXPANDED_OCR_PAGES)

    @staticmethod
    def _text_strength(value: str) -> int:
        return sum(character.isalnum() for character in value)

    def _extract(
        self,
        document: PilotDocument,
        path: Path | None = None,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> None:
        source = path or (self.files / document.stored_name)
        if is_media_type(document.media_type):
            raise UploadProblem("Media sources must use the transcription queue.")
        if document.media_type in IMAGE_MEDIA_TYPES:
            from .extended_extract import extract_image

            try:
                sections, frame_count = extract_image(source, document.media_type)
            except ValueError as exc:
                raise UploadProblem(str(exc)) from exc
            document.units = [
                asdict(
                    PilotUnit(
                        section.number,
                        section.text,
                        excerpt_digest=self._digest_text(section.text),
                        location_label=section.label,
                    )
                )
                for section in sections
            ]
            document.page_count = frame_count
            document.state = "ready"
            document.message = (
                f"{frame_count} image frame{'s' if frame_count != 1 else ''} ready; "
                f"{len(sections)} searchable text section"
                f"{'s' if len(sections) != 1 else ''} recognized"
                if sections
                else f"{frame_count} image frame{'s' if frame_count != 1 else ''} ready; no searchable text was recognized"
            )
            if progress is not None:
                progress("Recognizing image text", frame_count, frame_count)
            return
        if document.media_type in EMAIL_MEDIA_TYPES:
            from .extended_extract import extract_email

            try:
                sections = extract_email(source)
            except ValueError as exc:
                raise UploadProblem(str(exc)) from exc
            document.units = [
                asdict(
                    PilotUnit(
                        section.number,
                        section.text,
                        excerpt_digest=self._digest_text(section.text),
                        location_label=section.label,
                    )
                )
                for section in sections
            ]
            document.page_count = len(sections)
            document.state = "ready"
            document.message = (
                f"Email ready with {len(sections)} review section"
                f"{'s' if len(sections) != 1 else ''}"
            )
            if progress is not None:
                progress("Reading email", len(sections), len(sections))
            return
        if document.media_type in SPREADSHEET_MEDIA_TYPES:
            from .extended_extract import extract_delimited, extract_xlsx

            try:
                if document.media_type == "text/csv":
                    sections = extract_delimited(source, ",")
                elif document.media_type == "text/tab-separated-values":
                    sections = extract_delimited(source, "\t")
                else:
                    sections = extract_xlsx(source)
            except ValueError as exc:
                raise UploadProblem(str(exc)) from exc
            document.units = [
                asdict(
                    PilotUnit(
                        section.number,
                        section.text,
                        excerpt_digest=self._digest_text(section.text),
                        location_label=section.label,
                    )
                )
                for section in sections
            ]
            document.page_count = len(sections)
            document.state = "ready"
            document.message = (
                f"Spreadsheet ready with {len(sections)} review section"
                f"{'s' if len(sections) != 1 else ''}; formulas were not executed"
            )
            if progress is not None:
                progress("Reading spreadsheet", len(sections), len(sections))
            return
        if document.media_type == "application/pdf":
            from .review_bench_v2 import extract_pdf_pages

            try:
                pages = extract_pdf_pages(source)
            except ValueError as exc:
                message = str(exc)
                if "Encrypted PDFs" in message:
                    raise UploadProblem(message) from exc
                if "exceeds" in message or "timed out" in message or "could not start" in message:
                    raise UploadProblem(message) from exc
                raise UploadProblem("That PDF is damaged or malformed. Check the file and try again.") from exc
            ocr_mode = os.getenv(
                "CASE_INTELLIGENCE_OCR_MODE", os.getenv("CASE_REVIEW_OCR_MODE", "")
            ).strip().casefold()
            ocr_enabled = ocr_mode in {"selective", "expanded"}
            ocr_limit = MAX_OCR_PAGES if ocr_mode == "selective" else self._expanded_ocr_limit()
            attempted = 0
            native_pages = 0
            outcomes: dict[str, int] = {}
            # Native extraction has already passed these bounds. OCR additions
            # must share them rather than multiplying the document text budget.
            total_chars = sum(len(page.text.strip()) for page in pages)
            units: list[PilotUnit] = []
            for page in pages:
                text = page.text.strip()
                native_pages += bool(text)
                candidate = (
                    self._text_strength(text) < 20
                    or getattr(page, "image_coverage", 0.0) >= 0.10
                    or not getattr(page, "image_evidence_known", False)
                )
                if candidate:
                    if not ocr_enabled:
                        outcome = "disabled"
                    elif ocr_mode == "selective" and text:
                        outcome = "selection"
                    elif attempted >= ocr_limit:
                        outcome = "page_limit"
                    else:
                        attempted += 1
                        # Native words let orientation retries ignore an
                        # upright stamp; pages without them keep the plain call.
                        result = (
                            _ocr_pdf_page(source, page.page_number, native_text=text)
                            if text else _ocr_pdf_page(source, page.page_number)
                        )
                        outcome = result.status
                        if result.status == "recognized":
                            combined = _combine_pdf_text(text, result.text)
                            addition = len(combined) - len(text)
                            if (
                                len(combined) > MAX_PDF_PAGE_CHARS
                                or total_chars + addition > MAX_PDF_TOTAL_CHARS
                            ):
                                outcome = "output_limit"
                            else:
                                total_chars += addition
                                text = combined
                    outcomes[outcome] = outcomes.get(outcome, 0) + 1
                if text:
                    units.append(
                        PilotUnit(
                            page.page_number,
                            text,
                            excerpt_digest=self._digest_text(text),
                        )
                    )
                if progress is not None:
                    progress("Recognizing pages" if attempted else "Extracting pages", page.page_number, len(pages))
            if len(units) > MAX_PDF_CHUNKS:
                raise UploadProblem("That PDF contains too many searchable sections.")
            document.units = [asdict(unit) for unit in units]
            document.page_count = len(pages)
            document.state = "ready" if units else "needs_ocr"
            notes = [
                f"{len(units)} of {len(pages)} pages with searchable text",
                f"native text on {native_pages}",
                f"OCR attempted on {attempted}",
            ]
            labels = {
                "recognized": "OCR returned text",
                "disabled": "OCR skipped (disabled)",
                "selection": "OCR skipped (selective mode)",
                "page_limit": "OCR skipped (page limit)",
                "timed_out": "OCR timed out",
                "failed": "OCR failed",
                "no_text": "OCR returned no text",
                "output_limit": "OCR text/raster limit reached",
            }
            notes.extend(f"{label}: {outcomes[key]}" for key, label in labels.items() if outcomes.get(key))
            notes.append("Complete page reading is not established.")
            document.message = ("Needs OCR. " if not units else "") + "; ".join(notes)
            return
        if document.media_type == DOCX_MEDIA_TYPE:
            from .docx_extract import extract_docx_sections

            try:
                sections = extract_docx_sections(source)
            except ValueError as exc:
                message = str(exc)
                if "exceeds" in message or "timed out" in message:
                    raise UploadProblem(message) from exc
                raise UploadProblem("That DOCX is damaged or malformed. Check the file and try again.") from exc
            if not sections:
                raise UploadProblem("The DOCX contains no searchable text.")
            units = [
                PilotUnit(
                    section.section_number,
                    section.text,
                    excerpt_digest=self._digest_text(section.text),
                )
                for section in sections
            ]
            document.units = [asdict(unit) for unit in units]
            document.page_count = len(units)
            document.state = "ready"
            document.message = (
                f"{len(units)} section{'s' if len(units) != 1 else ''} ready and searchable"
            )
            if progress is not None:
                progress("Extracting sections", len(units), len(units))
            return
        raw = self._read_regular_file(source, MAX_TEXT_UTF8_BYTES)
        if raw.startswith((b"\xff\xfe", b"\xfe\xff", b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            raise UploadProblem("TXT files must be valid UTF-8, not UTF-16 or UTF-32.")
        if b"\x00" in raw:
            raise UploadProblem("TXT files cannot contain NUL or binary content.")
        try:
            text = raw.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as exc:
            raise UploadProblem("TXT files must be valid UTF-8 text.") from exc
        if len(text) > MAX_TEXT_CHARS:
            raise UploadProblem("That text file contains too many characters.")
        lines = text.splitlines()
        if len(lines) > MAX_TEXT_LINES:
            raise UploadProblem("That text file contains too many lines.")
        if any(len(line) > MAX_TEXT_LINE_CHARS for line in lines):
            raise UploadProblem("That text file contains a line that is too long.")
        units: list[PilotUnit] = []
        for start in range(0, len(lines), TEXT_LINES_PER_CHUNK):
            block = lines[start:start + TEXT_LINES_PER_CHUNK]
            if any(line.strip() for line in block):
                excerpt = "\n".join(block)
                units.append(PilotUnit(len(units) + 1, excerpt, start + 1, start + len(block), self._digest_text(excerpt)))
                if len(units) > MAX_TEXT_CHUNKS:
                    raise UploadProblem("That text file contains too many searchable sections.")
        if not units:
            raise UploadProblem("The text file contains no searchable text.")
        document.units = [asdict(unit) for unit in units]
        document.page_count = len(lines)
        document.state = "ready"
        document.message = f"{len(lines)} lines ready and searchable"
        # Ready TXT progress counts extracted chunks, while page_count retains
        # original line count. This persists tail completeness without changing
        # the registry format or inventing searchable whitespace-only units.
        document.completed_units = document.total_units = len(units)
        if progress is not None:
            progress("Extracting text", len(units), len(units))

    def register_linked_source(
        self,
        *,
        source_location_id: str,
        relative_path: str,
        display_name: str,
        media_type: str,
        byte_size: int,
        stable_device: int,
        stable_inode: int,
        stable_mtime_ns: int,
    ) -> PilotDocument:
        source = _LinkedSourceRegistration(
            relative_path,
            display_name,
            media_type,
            byte_size,
            stable_device,
            stable_inode,
            stable_mtime_ns,
        )
        return self.register_linked_sources(
            source_location_id=source_location_id, sources=(source,)
        )[0]

    def register_linked_sources(
        self,
        *,
        source_location_id: str,
        sources: Iterable[object],
    ) -> tuple[PilotDocument, ...]:
        """Register a reviewed collection with one atomic manifest replacement."""

        with self._lock:
            self._ensure_active()
            reviewed = tuple(sources)
            existing_keys = {item.name_key for item in self.documents.values()}
            created: list[PilotDocument] = []
            new_keys: set[str] = set()
            for source in reviewed:
                relative_path = str(getattr(source, "relative_path"))
                media_type = str(getattr(source, "media_type"))
                byte_size = int(getattr(source, "byte_size"))
                name_key = f"registered:{source_location_id}:{relative_path.casefold()}"
                if name_key in existing_keys or name_key in new_keys:
                    raise UploadProblem("That source is already part of this matter.", 409)
                if media_type not in DOCUMENT_MEDIA_TYPES.values() or byte_size <= 0:
                    raise UploadProblem("That source type cannot be added.")
                new_keys.add(name_key)
                created.append(
                    PilotDocument(
                        document_id=uuid.uuid4().hex,
                        display_name=relative_path,
                        stored_name="",
                        media_type=media_type,
                        size=byte_size,
                        state="queued",
                        message="Queued for processing",
                        units=[],
                        version_id=uuid.uuid4().hex,
                        name_key=name_key,
                        origin="registered",
                        source_location_id=source_location_id,
                        relative_path=relative_path,
                        stable_device=int(getattr(source, "stable_device")),
                        stable_inode=int(getattr(source, "stable_inode")),
                        stable_mtime_ns=int(getattr(source, "stable_mtime_ns")),
                        processing_stage="Queued",
                        _units_loader=self._load_units,
                        _units_iterator=self._iter_units,
                    )
                )
            for document in created:
                self.documents[document.document_id] = document
            try:
                self._save(tuple(document.document_id for document in created))
            except Exception:
                for document in created:
                    self.documents.pop(document.document_id, None)
                raise
            return tuple(created)

    def source_path(self, document_id: str, *, verify_digest: bool = False) -> Path:
        """Resolve one owned upload to a regular, size-matched source path."""

        with self._lock:
            document = self.get(document_id)
            if document.origin != "upload" or not document.stored_name:
                raise UploadProblem("The original is not stored in this workbench.", 404)
            path = self.files / document.stored_name
            if path.parent != self.files or path.is_symlink():
                raise UploadProblem("The stored source is unavailable.", 409)
            try:
                metadata = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise UploadProblem("The stored source is unavailable.", 409) from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size != document.size
                or (
                    document.stable_mtime_ns
                    and metadata.st_mtime_ns != document.stable_mtime_ns
                )
            ):
                raise UploadProblem("The stored source is unavailable.", 409)
            if verify_digest:
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(fd, "rb") as stream:
                        opened = os.fstat(stream.fileno())
                        if not stat.S_ISREG(opened.st_mode) or opened.st_size != document.size:
                            raise UploadProblem("The stored source changed. Upload a fresh copy.", 409)
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                        after = os.fstat(stream.fileno())
                    if (digest != document.digest or opened.st_size != after.st_size
                            or opened.st_mtime_ns != after.st_mtime_ns
                            or opened.st_ctime_ns != after.st_ctime_ns):
                        raise UploadProblem("The stored source changed. Upload a fresh copy.", 409)
                except OSError as exc:
                    raise UploadProblem("The stored source is unavailable.", 409) from exc
            return path

    def queue_browser_playback(
        self, document_id: str, *, retry: bool = False
    ) -> PilotDocument:
        """Persist a video compatibility check without changing transcript state."""

        with self._lock:
            document = self.get(document_id)
            if not is_media_type(document.media_type) or not document.has_video:
                return document
            if (
                document.playback_state == "ready"
                and self._playback_file_is_safe(document)
                and not retry
            ):
                return document
            if document.playback_state == "original" and not retry:
                return document
            if document.playback_state == "failed" and not retry:
                return document
            document.playback_state = "queued"
            document.playback_message = "Queued to check browser playback."
            self._save((document_id,))
            return document

    def playback_reservation_bytes(self, document_id: str) -> int:
        """Bound temporary rendition growth before the worker starts writing."""

        with self._lock:
            document = self.get(document_id)
            # Source size alone is a weak ceiling for a long, highly compressed
            # upload. Reserve for the configured 6 Mbps video + 128 kbps audio
            # lane, with container/headroom, so ``-fs`` does not truncate an
            # otherwise valid browser rendition near the end of the recording.
            duration_seconds = max(document.duration_ms / 1_000, 1)
            bitrate_ceiling = round(
                duration_seconds * (6_128_000 / 8) * 1.10
            ) + 16 * 1024 * 1024
            requested = max(
                (document.size * 5) // 4,
                bitrate_ceiling,
                16 * 1024 * 1024,
            )
            return max(1, min(requested, self.media_file_limit))

    def browser_playback_source(
        self, document_id: str
    ) -> tuple[Path, str, int, bool]:
        """Resolve the compatible rendition when ready, otherwise the original."""

        with self._lock:
            document = self.get(document_id)
            if document.playback_state == "ready" and self._playback_file_is_safe(document):
                return (
                    self.derived / document.playback_name,
                    document.playback_media_type,
                    document.playback_size,
                    True,
                )
            return self.source_path(document_id), document.media_type, document.size, False

    def fail_browser_playback(self, document_id: str, message: str) -> PilotDocument:
        with self._lock:
            document = self.get(document_id)
            if not document.has_video:
                return document
            document.playback_state = "failed"
            document.playback_message = message[:240]
            self._save((document_id,))
            return document

    def prepare_browser_playback(
        self,
        document_id: str,
        source_version_id: str,
        *,
        maximum_output_bytes: int,
        cancelled: Callable[[], bool],
        force_full: bool = False,
    ) -> PilotDocument:
        """Prepare atomic browser playback while retaining the uploaded original."""

        with self._lock:
            document = self.get(document_id)
            if document.version_id != source_version_id or not document.has_video:
                return document
            if (
                document.playback_state == "ready"
                and self._playback_file_is_safe(document)
                and not force_full
            ):
                return document
            if document.playback_state == "original" and not force_full:
                return document
            document.playback_state = "processing"
            document.playback_message = "Checking whether this video plays in the browser."
            self._save((document_id,))
            source = self.source_path(document_id)
            source_media_type = document.media_type
            source_duration_ms = document.duration_ms

        temporary = self.derived / (
            f".{document_id}-playback-{uuid.uuid4().hex}.tmp.mp4"
        )
        final_name = f"{document_id}.playback.mp4"
        final = self.derived / final_name
        process: subprocess.Popen[bytes] | None = None
        try:
            probe = _probe_media(source, source_media_type)
            if not probe.has_video:
                raise UploadProblem("The stored video track is unavailable.")
            if probe.browser_compatible and not force_full:
                with self._lock:
                    document = self.get(document_id)
                    if document.version_id != source_version_id:
                        return document
                    document.playback_name = ""
                    document.playback_media_type = source_media_type
                    document.playback_size = document.size
                    document.playback_state = "original"
                    document.playback_message = (
                        "The original video is ready without conversion."
                    )
                    self._save((document_id,))
                    return document
            if cancelled():
                raise InterruptedError
            selected_method = (
                "full_transcode" if force_full else probe.playback_method
            )
            methods = [selected_method]
            if selected_method in {"remux", "audio_only"}:
                # Containers occasionally carry details ffprobe cannot expose in
                # a compact probe. If the cheap path fails validation, retain
                # service usefulness by falling back to the bounded full lane.
                methods.append("full_transcode")
            metadata: os.stat_result | None = None
            completed_method = ""
            last_problem: Exception | None = None
            for method in methods:
                if cancelled():
                    raise InterruptedError
                temporary.unlink(missing_ok=True)
                with self._lock:
                    document = self.get(document_id)
                    if document.version_id != source_version_id:
                        return document
                    document.playback_message = _processing_playback_message(method)
                    self._save((document_id,))
                try:
                    process = subprocess.Popen(
                        _browser_playback_command(
                            method,
                            source,
                            temporary,
                            maximum_output_bytes,
                        ),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        env={
                            "PATH": "/usr/bin:/bin",
                            "LANG": "C.UTF-8",
                            "LC_ALL": "C.UTF-8",
                        },
                    )
                    while process.poll() is None:
                        if cancelled():
                            process.terminate()
                            try:
                                process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=2)
                            raise InterruptedError
                        time.sleep(0.2)
                    if process.returncode != 0:
                        raise UploadProblem(
                            "A browser-compatible video copy could not be prepared."
                        )
                    metadata = temporary.stat(follow_symlinks=False)
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_size <= 0
                        or metadata.st_size > maximum_output_bytes
                    ):
                        raise UploadProblem(
                            "A browser-compatible video copy could not be prepared."
                        )
                    rendition = _probe_media(temporary, "video/mp4")
                    duration_tolerance = max(2_000, source_duration_ms // 100)
                    if (
                        not rendition.has_video
                        or not rendition.browser_compatible
                        or rendition.duration_ms
                        < max(source_duration_ms - duration_tolerance, 1)
                    ):
                        raise UploadProblem(
                            "A complete browser-compatible video copy could not be prepared."
                        )
                    completed_method = method
                    break
                except (OSError, UploadProblem, subprocess.SubprocessError) as exc:
                    last_problem = exc
                    if method == methods[-1]:
                        raise
            if metadata is None or not completed_method:
                if last_problem is not None:
                    raise last_problem
                raise UploadProblem(
                    "A browser-compatible video copy could not be prepared."
                )
            os.chmod(temporary, 0o600, follow_symlinks=False)
            descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, final)
            self._fsync_directory(self.derived)
            with self._lock:
                document = self.get(document_id)
                if document.version_id != source_version_id:
                    final.unlink(missing_ok=True)
                    self._fsync_directory(self.derived)
                    return document
                document.playback_name = final_name
                document.playback_media_type = "video/mp4"
                document.playback_size = int(metadata.st_size)
                document.playback_state = "ready"
                document.playback_message = _ready_playback_message(completed_method)
                self._save((document_id,))
                return document
        except InterruptedError:
            with self._lock:
                try:
                    document = self.get(document_id)
                except (KeyError, UploadProblem):
                    raise
                document.playback_state = "queued"
                document.playback_message = "Browser playback preparation will resume."
                self._save((document_id,))
                return document
        except (OSError, UploadProblem, subprocess.SubprocessError):
            return self.fail_browser_playback(
                document_id,
                "The original video is retained, but a browser-compatible copy could not be prepared. Try again.",
            )
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            temporary.unlink(missing_ok=True)

    def mark_media_processing(
        self, document_id: str, *, stage: str, progress: float = 0
    ) -> PilotDocument:
        with self._lock:
            document = self.get(document_id)
            if not is_media_type(document.media_type):
                raise UploadProblem("That source is not media.")
            document.state = "processing"
            document.processing_stage = (stage or "Transcribing")[:100]
            document.message = document.processing_stage
            document.completed_units = max(0, min(round(float(progress) * 100), 100))
            document.total_units = 100
            self._save((document_id,))
            return document

    def install_media_transcript(
        self, document_id: str, units: Sequence[PilotUnit], *, degraded: bool = False
    ) -> PilotDocument:
        """Atomically project current transcript text into the source manifest."""

        prepared = tuple(units)
        if not prepared or len(prepared) > 100_000:
            raise UploadProblem("The transcript contains no searchable passages.")
        with self._lock:
            document = self.get(document_id)
            if not is_media_type(document.media_type):
                raise UploadProblem("That source is not media.")
            previous_start = -1
            for ordinal, unit in enumerate(prepared, 1):
                if (
                    unit.number != ordinal
                    or unit.start_ms is None
                    or unit.end_ms is None
                    or unit.start_ms < previous_start
                    or unit.end_ms <= unit.start_ms
                    or unit.end_ms > document.duration_ms + 2_000
                    or not unit.text.strip()
                    or unit.excerpt_digest != self._digest_text(unit.text)
                ):
                    raise UploadProblem("The transcript projection is invalid.")
                previous_start = unit.start_ms
            self._drop_units(document)
            document.units = [asdict(unit) for unit in prepared]
            document.page_count = len(prepared)
            document.state = "ready"
            document.processing_stage = "Transcript ready"
            document.completed_units = 100
            document.total_units = 100
            qualifier = " with quality warnings" if degraded else ""
            document.message = (
                f"{len(prepared)} transcript passage"
                f"{'s' if len(prepared) != 1 else ''} ready and searchable{qualifier}"
            )
            self._save((document_id,))
            return document

    def process(
        self,
        document_id: str,
        *,
        staged_source: Path | None = None,
        staged_digest: str = "",
        progress: Callable[[str, int, int], None] | None = None,
    ) -> PilotDocument:
        with self._lock:
            self._ensure_active()
            document = self.get(document_id)
            source = staged_source or (self.files / document.stored_name)
            if document.origin == "upload" and not self._stored_file_matches(source, document):
                document.state = "changed"
                document.message = "Stored file changed after processing. Remove it and upload the intended file again."
                self._drop_units(document)
                self._save((document_id,))
                raise UploadProblem(document.message, 409)
            if document.origin == "registered":
                if staged_source is None or not staged_digest:
                    raise UploadProblem("The registered source could not be staged safely.")
                document.digest = staged_digest
            self._drop_units(document)
            document.state = "processing"
            document.message = "Processing…"
            document.processing_stage = "Extracting text"
            document.completed_units = 0
            document.total_units = 0
            self._save((document_id,))

        def report(stage: str, completed: int, total: int) -> None:
            with self._lock:
                document.processing_stage = stage
                document.completed_units = completed
                document.total_units = total
                # SQLite is the granular progress ledger. Persisting the complete
                # per-matter manifest for every page creates quadratic write
                # amplification on large matters; the final state boundary below
                # remains fsync-backed and startup safely requeues interrupted work.
            if progress is not None:
                progress(stage, completed, total)

        try:
            self._extract(document, source, report)
        except UploadProblem as exc:
            with self._lock:
                document.state = "failed"
                document.message = str(exc)
                document.processing_stage = "Needs attention"
                document.page_count = 0
                self._drop_units(document)
                self._save((document_id,))
            raise
        with self._lock:
            document.processing_stage = "Ready"
            self._save((document_id,))
        return document

    def retry(self, document_id: str) -> PilotDocument:
        with self._lock:
            document = self.get(document_id)
            if document.state not in {"failed", "needs_ocr"}:
                raise UploadProblem(
                    "This source cannot be retried. Remove it and upload the intended file again.",
                    409,
                )
            source = self.files / document.stored_name
            if not self._stored_file_matches(source, document):
                document.state = "changed"
                document.message = "Stored file changed after processing. Remove it and upload the intended file again."
                document.units = []
                self._save((document_id,))
                raise UploadProblem(document.message, 409)
        try:
            return self.process(document_id)
        except UploadProblem:
            return self.get(document_id)

    def get(self, document_id: str) -> PilotDocument:
        self._ensure_active()
        document = self.documents.get(document_id)
        if document is None:
            raise KeyError(document_id)
        return document

    def mark_failed(self, document_id: str, message: str) -> PilotDocument:
        with self._lock:
            document = self.get(document_id)
            document.state = "failed"
            document.message = message[:240]
            document.processing_stage = "Needs attention"
            self._drop_units(document)
            self._save((document_id,))
            return document

    @staticmethod
    def action_token(document: PilotDocument) -> str:
        return hashlib.sha256(
            ("pilot-action\x00" + document.document_id + "\x00" + document.version_id).encode("utf-8")
        ).hexdigest()[:32]

    def get_by_action_token(self, token: str) -> PilotDocument:
        self._ensure_active()
        matches = [item for item in self.documents.values() if self.action_token(item) == token]
        if len(matches) != 1:
            raise KeyError(token)
        return matches[0]

    def ready_documents(self) -> Iterable[PilotDocument]:
        with self._lock:
            self._ensure_active()
            return tuple(item for item in self.documents.values() if item.state == "ready")

    def document_count(self) -> int:
        with self._lock:
            self._ensure_active()
            return len(self.documents)

    def remove(self, document_id: str) -> None:
        with self._lock:
            document = self.get(document_id)
            if document.stored_name:
                target = self.files / document.stored_name
                if target.parent != self.files or target.is_symlink():
                    raise RuntimeError("pilot removal refused")
                if target.exists():
                    mode = target.stat(follow_symlinks=False).st_mode
                    if not stat.S_ISREG(mode):
                        raise RuntimeError("pilot removal refused")
                    target.unlink()
                    self._fsync_directory(self.files)
            self._drop_units(document)
            if document.playback_name and re.fullmatch(
                r"[0-9a-f]{32}\.playback\.mp4", document.playback_name
            ):
                (self.derived / document.playback_name).unlink(missing_ok=True)
                self._fsync_directory(self.derived)
            del self.documents[document_id]
            self._save((), removed_document_ids=(document_id,))

    def _close_registry_locked(self) -> None:
        if self._registry is not None:
            self._registry.close()
            self._registry = None

    def close(self) -> None:
        """Close the per-matter registry after background users have stopped."""

        with self._lock:
            self._close_registry_locked()
            self._retired = True

    def quarantine(self, destination: Path) -> Path:
        """Atomically retire this owned source root onto the same storage boundary."""

        target = Path(destination)
        with self._lock:
            self._ensure_active()
            if self.root.is_symlink() or not self.root.is_dir():
                raise RuntimeError("matter source storage is unsafe")
            if target.exists() or target.is_symlink():
                raise RuntimeError("matter purge quarantine already exists")
            if target.parent.is_symlink() or not target.parent.is_dir():
                raise RuntimeError("matter purge quarantine is unsafe")
            self._close_registry_locked()
            os.replace(self.root, target)
            self._fsync_directory(target.parent)
            self._fsync_directory(self.root.parent)
            self._retired = True
        return target


def securely_delete_owned_tree(path: Path, *, allowed_parent: Path) -> None:
    """Delete one exact owned quarantine tree without following symbolic links."""

    target = Path(path)
    boundary = Path(allowed_parent)
    if target.parent != boundary or target == boundary:
        raise RuntimeError("matter purge path escaped its storage boundary")
    if boundary.is_symlink() or not boundary.is_dir():
        raise RuntimeError("matter purge boundary is unsafe")
    if target.is_symlink():
        raise RuntimeError("matter purge tree is unsafe")
    if not target.exists():
        return
    if not target.is_dir():
        raise RuntimeError("matter purge tree is unsafe")

    def delete_directory(directory: Path) -> None:
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("matter purge tree contains an unsafe directory")
        with os.scandir(directory) as entries:
            children = tuple(entries)
        for entry in children:
            child = directory / entry.name
            if entry.is_symlink():
                raise RuntimeError("matter purge tree contains a symbolic link")
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                delete_directory(child)
            elif stat.S_ISREG(mode):
                child.unlink()
            else:
                raise RuntimeError("matter purge tree contains an unexpected file type")
        directory.rmdir()

    delete_directory(target)
    PilotStore._fsync_directory(boundary)
