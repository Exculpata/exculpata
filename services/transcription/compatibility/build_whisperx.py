#!/usr/bin/env python3
"""Build the pinned metadata-only WhisperX compatibility wheel (Python 3.11)."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import stat
import sys
import tarfile
import tempfile
import urllib.request

REVISION = "3ccc17b8de34f305300f8a3fd3c9f76ba820c0d0"
URL = f"https://codeload.github.com/m-bain/whisperX/tar.gz/{REVISION}"
SOURCE_SHA256 = "595d0ca69ec9d847d2b174909d4aa4a2853285c54cbe3b7bac500269fd3d7101"
METADATA_SHA256 = "aabe8d8b1c014dc8ab27ef1b0491b070c798a9a2e9c8facd3cec2c4a0977348d"
VERSION = "3.8.6+recordbench.1"
WHEEL = f"whisperx-{VERSION}-py3-none-any.whl"
MAX_ARCHIVE = 32 * 1024 * 1024
BUILD_TOOLS = {"setuptools": "84.0.0", "wheel": "0.48.0", "packaging": "26.3"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def patch_metadata(data: bytes) -> bytes:
    if sha256(data) != METADATA_SHA256:
        raise RuntimeError("upstream metadata identity changed")
    text = data.decode("utf-8")
    replacements = {
        'version = "3.8.6"': f'version = "{VERSION}"',
        '"huggingface-hub<1.0.0"': '"huggingface-hub>=1.5.0,<2.0"',
        '"torch~=2.8.0"': '"torch==2.14.0"',
        '"torchaudio~=2.8.0"': '"torchaudio==2.11.0"',
        '"torchvision~=0.23.0"': '"torchvision==0.29.0"',
        '"torchcodec>=0.6.0,<0.8.0;': '"torchcodec==0.16.0;',
        '"transformers>=4.48.0"': '"transformers==5.17.0"',
        'include-package-data = true': 'include-package-data = true\nlicense-files = ["LICENSE*", "licenses/*.txt"]',
    }
    # Exculpata's explicit lock selects wheel origins; upstream uv settings
    # are not part of the patched distribution's dependency resolution.
    text = text.split("\n# torchcodec (transitive dep", 1)[0]
    for old, new in replacements.items():
        if text.count(old) != 1:
            raise RuntimeError("upstream metadata patch does not apply exactly")
        text = str(text).replace(old, new)
    return text.encode("utf-8")


def extract_source(archive: Path, destination: Path) -> Path:
    descriptor = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise RuntimeError("source archive must be a regular file")
        data = stream.read(MAX_ARCHIVE + 1)
    if len(data) > MAX_ARCHIVE or sha256(data) != SOURCE_SHA256:
        raise RuntimeError("WhisperX source archive failed identity verification")
    import io
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as source:
        members = source.getmembers()
        if len(members) > 2048 or sum(m.size for m in members) > 64 * 1024 * 1024:
            raise RuntimeError("source archive exceeds extraction limits")
        names = set()
        for member in members:
            path = PurePosixPath(member.name)
            if (not path.parts or path.parts[0] != f"whisperX-{REVISION}"
                    or path.is_absolute() or ".." in path.parts or "\\" in member.name
                    or member.name in names or not (member.isdir() or member.isfile())):
                raise RuntimeError("source archive contains an unsafe entry")
            names.add(member.name)
        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as stream, target.open("xb") as output:
                    output.write(stream.read())
                target.chmod(0o644)
    return destination / f"whisperX-{REVISION}"


def build(archive: Path, output: Path) -> dict:
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError("the compatibility build requires Python 3.11")
    if any(importlib.metadata.version(name) != version for name, version in BUILD_TOOLS.items()):
        raise RuntimeError("install requirements-api-build.lock before building")
    # A fresh output is deliberate: never mix a previous wheel with this build.
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="whisperx-build-") as temporary:
        source = extract_source(archive, Path(temporary))
        original = {p.relative_to(source).as_posix(): sha256(p.read_bytes())
                    for p in sorted((source / "whisperx").rglob("*.py"))}
        metadata = source / "pyproject.toml"
        metadata.write_bytes(patch_metadata(metadata.read_bytes()))
        notices = source / "licenses"
        notices.mkdir()
        notice = Path(__file__).resolve().parents[1] / "licenses/pyannote-segmentation-MIT.txt"
        (notices / notice.name).write_bytes(notice.read_bytes())
        (notices / notice.name).chmod(0o644)
        env = dict(os.environ, SOURCE_DATE_EPOCH="1704067200", PYTHONHASHSEED="0")
        subprocess.run([sys.executable, "-c", "import os; os.umask(0o022); import setuptools.build_meta as b; b.build_wheel('dist')"],
                       cwd=source, env=env, check=True, timeout=180)
        current = {p.relative_to(source).as_posix(): sha256(p.read_bytes())
                   for p in sorted((source / "whisperx").rglob("*.py"))}
        if current != original:
            raise RuntimeError("upstream Python sources changed during build")
        wheel = (source / "dist" / WHEEL).read_bytes()
        receipt = {"source_revision": REVISION, "source_sha256": SOURCE_SHA256,
                   "version": VERSION, "wheel": WHEEL, "wheel_sha256": sha256(wheel),
                   "metadata_sha256": sha256(metadata.read_bytes()),
                   "python_sources": original, "build_tools": BUILD_TOOLS,
                   "source_date_epoch": 1704067200}
        (output / WHEEL).write_bytes(wheel)
        (output / "build-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-archive", type=Path,
                        help="use an already acquired, hash-verified upstream archive; no download")
    parser.add_argument("--output", type=Path, required=True, help="new wheel output directory")
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        parser.error("--output must be a new directory")
    with tempfile.TemporaryDirectory(prefix="whisperx-source-") as temporary:
        archive = args.source_archive
        if archive is None:
            archive = Path(temporary) / "source.tar.gz"
            with urllib.request.urlopen(URL, timeout=60) as response:
                data = response.read(MAX_ARCHIVE + 1)
            if len(data) > MAX_ARCHIVE:
                raise RuntimeError("source download exceeds limit")
            archive.write_bytes(data)
        receipt = build(archive, args.output.resolve())
    print(json.dumps({key: receipt[key] for key in ("version", "wheel_sha256")}))


if __name__ == "__main__":
    main()
