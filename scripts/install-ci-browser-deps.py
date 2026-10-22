#!/usr/bin/env python3
"""Bound package acquisition on the disposable Ubuntu CI runner."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import platform
import re
import subprocess
import sys


PACKAGES = (
    "libnss3", "libatk-bridge2.0-0", "libxkbcommon0", "libxcomposite1",
    "libxdamage1", "libxrandr2", "libgbm1", "libasound2t64", "libgtk-3-0",
    "fonts-liberation", "ffmpeg",
)
MIRRORS = Path("/etc/apt/apt-mirrors.txt")
SOURCES = Path("/etc/apt/sources.list.d/ubuntu.sources")
OFFICIAL_MIRRORS = (
    "https://archive.ubuntu.com/ubuntu/",
    "https://security.ubuntu.com/ubuntu/",
)
ACQUIRE_OPTIONS = (
    "-o", "Acquire::Retries=1",
    "-o", "Acquire::http::Timeout=15",
    "-o", "Acquire::https::Timeout=15",
)


def validate_sources(text: str) -> None:
    """Require every active Ubuntu Deb822 stanza to use only our mirror list.

    Parse stanza boundaries, folded values and Enabled instead of finding a URI
    anywhere in the file. Reject ambiguous/malformed input without rewriting it.
    """
    stanzas = []
    fields: dict[str, str] = {}
    previous = ""
    for line in [*text.splitlines(), ""]:
        if not line.strip():
            if fields:
                stanzas.append(fields)
            fields, previous = {}, ""
        elif line.startswith("#"):
            continue
        elif line[0].isspace():
            if not previous:
                raise ValueError("Ubuntu sources contain an orphan continuation.")
            fields[previous] += " " + line.strip()
        else:
            name, separator, value = line.partition(":")
            if not separator or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", name):
                raise ValueError("Ubuntu sources contain a malformed field.")
            name = name.lower()
            if name in fields:
                raise ValueError("Ubuntu sources contain a duplicate field.")
            fields[name], previous = value.strip(), name

    active = 0
    for stanza in stanzas:
        enabled = stanza.get("enabled", "yes").lower()
        if enabled not in {"yes", "no"}:
            raise ValueError("Ubuntu sources contain an unsupported Enabled value.")
        if enabled == "no":
            continue
        active += 1
        if any(not stanza.get(field) for field in ("types", "uris", "suites", "components", "signed-by")):
            raise ValueError("An active Ubuntu source is missing a required field.")
        if not set(stanza["types"].split()).issubset({"deb", "deb-src"}):
            raise ValueError("Ubuntu sources contain an unsupported source type.")
        if stanza["uris"].split() != ["mirror+file:/etc/apt/apt-mirrors.txt"]:
            raise ValueError("Every active Ubuntu source must use only the expected mirror list.")
    if not active:
        raise ValueError("Ubuntu sources contain no active mirror-list stanza.")


def archive_options(archives: Path | None) -> tuple[str, ...]:
    # APT verifies cached packages against freshly downloaded signed indexes.
    return () if archives is None else (
        "-o", f"Dir::Cache::archives={archives}", "-o", "APT::Keep-Downloaded-Packages=true",
    )


def acquire(source_options: tuple[str, ...] = (), packages: tuple[str, ...] = PACKAGES,
            archives: Path | None = None) -> bool:
    # Only acquisition is terminated by these deadlines, never dpkg unpacking.
    # Connection/data timeouts alone do not bound a continuously slow download.
    for seconds, arguments in (
        (60, ("update", "--error-on=any")),
        (180, ("install", "--yes", "--no-install-recommends", "--download-only", *packages)),
    ):
        result = subprocess.run([
            "sudo", "timeout", "--signal=TERM", "--kill-after=10s", f"{seconds}s",
            "apt-get", *ACQUIRE_OPTIONS, *source_options, *archive_options(archives), *arguments,
        ], check=False)
        if result.returncode:
            print(f"APT acquisition stopped with status {result.returncode}.", flush=True)
            return False
    return True


@contextmanager
def official_mirrors(mirrors: Path, sources: Path):
    """Temporarily select only official HTTPS entries already on this runner."""
    original = mirrors.read_bytes()
    existing = {
        line.split()[0] for line in original.decode().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if not set(OFFICIAL_MIRRORS).issubset(existing):
        raise ValueError("The runner's existing official HTTPS mirrors are unavailable.")
    validate_sources(sources.read_text())
    replacement = "".join(
        f"{uri}\tpriority:{priority}\n"
        for priority, uri in enumerate(OFFICIAL_MIRRORS, start=1)
    ).encode()
    # Keep ubuntu.sources (suites, components, Signed-By) and APT trust untouched.
    # Restore even if a write, acquisition, or installation fails.
    try:
        subprocess.run(["sudo", "tee", str(mirrors)], input=replacement,
                       stdout=subprocess.DEVNULL, check=True)
        yield
    finally:
        subprocess.run(["sudo", "tee", str(mirrors)], input=original,
                       stdout=subprocess.DEVNULL, check=True)


def install(source_options: tuple[str, ...] = (), packages: tuple[str, ...] = PACKAGES,
            archives: Path | None = None) -> None:
    # No timeout/fallback around unpacking. Missing archives remain a hard error.
    subprocess.run([
        "sudo", "apt-get", "install", *source_options, *archive_options(archives), "--yes", "--no-install-recommends",
        "--no-download", *packages,
    ], check=True)


def install_dependencies(mirrors: Path = MIRRORS, sources: Path = SOURCES,
                         packages: tuple[str, ...] = PACKAGES, archives: Path | None = None) -> None:
    if archives is not None:
        (archives / "partial").mkdir(parents=True, exist_ok=True)
    print("Acquiring CI dependencies with bounded downloads.", flush=True)
    if acquire(packages=packages, archives=archives):
        install(packages=packages, archives=archives)
    else:
        print("Retrying acquisition once with the existing official HTTPS mirrors.", flush=True)
        with official_mirrors(mirrors, sources):
            # APT parses the .sources main file as Deb822. /dev/null disables the
            # source-parts directory, excluding unrelated sources during refresh,
            # package selection/download and offline installation alike.
            source_options = (
                "-o", f"Dir::Etc::sourcelist={sources.resolve()}",
                "-o", "Dir::Etc::sourceparts=/dev/null",
            )
            if not acquire(source_options, packages, archives):
                raise RuntimeError("CI dependency acquisition failed on both attempts; nothing installed.")
            install(source_options, packages, archives)
    if archives is not None:
        subprocess.run(["sudo", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(archives)], check=True)


def main() -> int:
    # This helper may change only the disposable hosted runner's mirror list.
    if sys.platform != "linux" or platform.machine() != "x86_64" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("This helper requires the Ubuntu 24.04 x86_64 GitHub Actions runner.")
    release = platform.freedesktop_os_release()
    if release.get("ID") != "ubuntu" or release.get("VERSION_ID") != "24.04":
        raise RuntimeError("This helper requires Ubuntu 24.04.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archives", type=Path)
    parser.add_argument("packages", nargs="*")
    args = parser.parse_args()
    install_dependencies(packages=tuple(args.packages) or PACKAGES, archives=args.archives)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"CI dependency setup failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
