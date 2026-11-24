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


def _pillow():
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:
        raise TiffPreviewError("TIFF preview rendering is unavailable in this environment.") from exc
    return Image, UnidentifiedImageError


def read_verified_tiff_bytes(path: Path, expected_digest: str) -> bytes:
    """Read an admitted TIFF safely without following symlinks and verify SHA-256 + stat."""
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
        same_stat = (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns
        ) == (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        )
        if remaining or not expected_digest or hasher.hexdigest() != expected_digest or not same_stat:
            raise TiffPreviewStale("The stored TIFF original changed on disk.")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _check_frame_bounds(image) -> tuple[int, int]:
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > MAX_TIFF_PIXELS:
        raise TiffPreviewError("The TIFF frame dimensions exceed the preview pixel limit.")
    return width, height


def _bounded_frame_count(image) -> int:
    """Count TIFF frames without traversing past MAX_TIFF_FRAMES + 1."""
    count = 1
    while True:
        try:
            image.seek(count)
        except EOFError:
            break
        except (OSError, ValueError) as exc:
            raise TiffPreviewError("The TIFF frame chain could not be read safely.") from exc
        count += 1
        if count > MAX_TIFF_FRAMES:
            raise TiffPreviewError("The TIFF frame count exceeds the preview limit.")
    image.seek(0)
    return count


def _open_tiff(raw: bytes):
    Image, UnidentifiedImageError = _pillow()
    try:
        image = Image.open(io.BytesIO(raw))
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise TiffPreviewError("The stored file is not a supported TIFF image.") from exc
    if image.format != "TIFF":
        image.close()
        raise TiffPreviewError("The stored file is not a TIFF image.")
    try:
        return image, _bounded_frame_count(image), Image
    except TiffPreviewError:
        image.close()
        raise


def _to_preview_mode(image):
    if image.mode in {"RGB", "RGBA", "L", "LA"}:
        return image.copy()
    if image.mode.startswith("I;16") or image.mode == "I":
        return image.point(lambda value: value * (1.0 / 256.0)).convert("L")
    return image.convert("L" if image.mode == "1" else "RGB")


def inspect_tiff_bytes(raw: bytes) -> TiffMetadata:
    image, frame_count, _ = _open_tiff(raw)
    try:
        width, height = _check_frame_bounds(image)
        return TiffMetadata(frame_count=frame_count, width=width, height=height)
    finally:
        image.close()


def inspect_tiff(path: Path, expected_digest: str) -> TiffMetadata:
    return inspect_tiff_bytes(read_verified_tiff_bytes(path, expected_digest))


def render_tiff_frame_bytes(raw: bytes, *, frame: int = 1) -> tuple[bytes, int]:
    """Render one 1-based TIFF frame from verified bytes to bounded PNG bytes."""
    if type(frame) is not int or frame < 1 or frame > MAX_TIFF_FRAMES:
        raise TiffPreviewError("The requested TIFF frame number is invalid.")
    image, frame_count, Image = _open_tiff(raw)
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
        except (Image.DecompressionBombError, OSError, ValueError, TypeError, EOFError) as exc:
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


def render_tiff_frame(path: Path, expected_digest: str, *, frame: int = 1) -> tuple[bytes, int]:
    if type(frame) is not int or frame < 1 or frame > MAX_TIFF_FRAMES:
        raise TiffPreviewError("The requested TIFF frame number is invalid.")
    return render_tiff_frame_bytes(read_verified_tiff_bytes(path, expected_digest), frame=frame)
