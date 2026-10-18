"""Pinned, offline IDNA2008 source; reject modified archives before import."""
import hashlib
from pathlib import Path

ARCHIVE_SHA256 = "390aa8fb9c672c2f89f4c02d12d89671bde3dbcabba5aee9226356b4fce4ae13"
archive = Path(__file__).with_name("idna-3.20.pyz")
try:
    if hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA256:
        raise RuntimeError("Bundled IDNA source failed integrity validation")
except OSError as exc:
    raise RuntimeError("Bundled IDNA source is unavailable") from exc
# Use a private package namespace; do not modify global sys.path or replace
# HTTPX's independently locked dependency. The archive contains source only.
__path__.append(str(archive) + "/idna")
from .core import IDNAError, encode  # noqa: E402
from .package_data import __version__  # noqa: E402
