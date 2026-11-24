"""Bounded, read-only TIFF inspection and PNG preview rendering.

Originals are verified by SHA-256 and stat identity before decoding, opened
without following symlinks, and never modified.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path
import stat

from PIL import Image, UnidentifiedImageError

MAX_TIFF_BYTES = 32 * 1024 * 1024
MAX_TIFF_PIXELS = 25_000_000
MAX_TIFF_FRAMES = 500
MAX_PREVIEW_EDGE = 2048
MAX_PREVIEW_PNG_BYTES = 12 * 1024 * 1024


class TiffPreviewError(ValueError):
    """The TIFF could not be safely inspected or rendered within bounds."""


class TiffPreviewStale(RuntimeError):
    """The stored TIFF original changed or is no longer available."""


@dataclass(frozen=True, slots=True)
class TiffMetadata:
    frame_count: int
    width: int
    height: int


def _read_verified_bytes(path: Path, expected_digest: str) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise TiffPreviewStale("The stored TIFF source is unavailable.") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise TiffPreviewStale("The stored TIFF source is not a regular file.")
        if before.st_size <= 0:
            raise TiffPreviewError("The stored TIFF file is empty.")
        if before.st_size > MAX_TIFF_BYTES:
            raise TiffPreviewError("The stored TIFF exceeds the preview byte limit.")
        chunks: list[bytes] = []
        hasher = hashlib.sha256()
        remaining = before.st_size
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            hasher.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if remaining or not expected_digest or hasher.hexdigest() != expected_digest or identity(before) != identity(after):
            raise TiffPreviewStale("The stored TIFF original changed on disk.")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _check_frame_bounds(image: Image.Image) -> tuple[int, int]:
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > MAX_TIFF_PIXELS:
        raise TiffPreviewError("The TIFF frame dimensions exceed the preview pixel limit.")
    return width, height


def _open_tiff(raw: bytes) -> tuple[Image.Image, int]:
    try:
        image = Image.open(io.BytesIO(raw))
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise TiffPreviewError("The stored file is not a supported TIFF image.") from exc
    if image.format != "TIFF":
        image.close()
        raise TiffPreviewError("The stored file is not a TIFF image.")
    frame_count = int(getattr(image, "n_frames", 1))
    if frame_count < 1 or frame_count > MAX_TIFF_FRAMES:
        image.close()
        raise TiffPreviewError("The TIFF frame count is outside the supported preview range.")
    return image, frame_count


def _to_preview_mode(image: Image.Image) -> Image.Image:
    if image.mode in {"RGB", "RGBA", "L", "LA"}:
        return image.copy()
    if image.mode.startswith("I;16") or image.mode == "I":
        return image.point(lambda value: max(0, min(255, int(value) >> 8))).convert("L")
    if image.mode == "1":
        return image.convert("L")
    return image.convert("RGB")


def inspect_tiff(path: Path, expected_digest: str) -> TiffMetadata:
    """Verify the original TIFF and inspect its frame count and first-frame dimensions."""
    raw = _read_verified_bytes(path, expected_digest)
    image, frame_count = _open_tiff(raw)
    try:
        width, height = _check_frame_bounds(image)
        return TiffMetadata(frame_count=frame_count, width=width, height=height)
    finally:
        image.close()


def render_tiff_frame(path: Path, expected_digest: str, *, frame: int = 1) -> tuple[bytes, int]:
    """Render one verified 1-based TIFF frame to bounded PNG bytes in memory."""
    if type(frame) is not int or frame < 1 or frame > MAX_TIFF_FRAMES:
        raise TiffPreviewError("The requested TIFF frame number is invalid.")
    raw = _read_verified_bytes(path, expected_digest)
    image, frame_count = _open_tiff(raw)
    try:
        if frame > frame_count:
            raise TiffPreviewError(f"TIFF frame {frame} is outside the {frame_count}-frame image.")
        try:
            if frame > 1:
                image.seek(frame - 1)
            _check_frame_bounds(image)
            converted = _to_preview_mode(image)
        except TiffPreviewError:
            raise
        except (Image.DecompressionBombError, OSError, ValueError, EOFError) as exc:
            raise TiffPreviewError("The requested TIFF frame could not be decoded safely.") from exc
        if max(converted.size) > MAX_PREVIEW_EDGE:
            converted.thumbnail((MAX_PREVIEW_EDGE, MAX_PREVIEW_EDGE), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        converted.save(output, format="PNG", optimize=True)
        png = output.getvalue()
        if len(png) > MAX_PREVIEW_PNG_BYTES:
            raise TiffPreviewError("The rendered TIFF preview exceeds the size limit.")
        return png, frame_count
    finally:
        image.close()
