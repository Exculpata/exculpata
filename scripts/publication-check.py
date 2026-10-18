#!/usr/bin/env python3
"""Fail closed when an OSS candidate contains deployment or secret residue."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import ipaddress
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import date
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
# Owner-reviewed public attribution; exact commit and name/email digest only.
# This suppresses only the email rule, never deny terms or content checks.
REVIEWED_COMMIT_EMAIL_IDENTITIES = {
    "6735691a938e42173c6c7a309b700be2e19c0668": {
        "33a1b1226f681d54071f794ee5d2b2cb226c4d91efbe954997eab68adcd3f35b",
    },
    "624fbe4dcfbae093c442955bfb6cc4b6be6fff3e": {
        "33a1b1226f681d54071f794ee5d2b2cb226c4d91efbe954997eab68adcd3f35b",
    },
    "d2f1e779e70063b94fece1291169f547c7c44d83": {
        "33a1b1226f681d54071f794ee5d2b2cb226c4d91efbe954997eab68adcd3f35b",
    },
    "4b8d74be09f41a443abb705bed1c6ab8bb5371c7": {
        "33a1b1226f681d54071f794ee5d2b2cb226c4d91efbe954997eab68adcd3f35b",
    },
    "f8ccae72b39704942c23caecb8195874a74e7699": {
        "33a1b1226f681d54071f794ee5d2b2cb226c4d91efbe954997eab68adcd3f35b",
    },
    "a8f4fbe36439a374746f9c2b2ca9660d89793044": {
        "33a1b1226f681d54071f794ee5d2b2cb226c4d91efbe954997eab68adcd3f35b",
    },
}
# Owner-reviewed outside contributors who chose to publish their own commit
# address. Exact SHA-256 of name + NUL + email bytes, on any commit, because
# squash merges mint a new SHA. Suppresses only the email rule for that exact
# author/committer identity or an exact Co-authored-by trailer line; deny terms,
# secrets and every other rule still apply. No case or whitespace folding.
# Maintainer identities must stay no-reply and never belong here.
REVIEWED_CONTRIBUTOR_EMAIL_IDENTITIES = frozenset(
    {
        # Fire162: owner review Oct 13 2026, outside contributor PR #234.
        "4fda816440a2b260c0f98662327e8ad67c3e2351a39e0300deb8d8e778ecccb5",
    }
)
# Exact media blobs whose raw bytes were reviewed. Local
# --allow-reviewed-media-digest values are added to this set at scan time.
# docs/assets/exculpata-launch-16x9.mp4 and site/assets/exculpata-launch-16x9.mp4.
# docs/assets/exculpata-launch-9x16.mp4 and site/assets/exculpata-launch-9x16.mp4.
REVIEWED_MEDIA_DIGESTS = (
    "764e069b47c7fb122f7907376868e79498594710745247f0e46416e9f59bd7ca",
    "b82dceab0752282f7442eba2c8630f7f01e4bcd0ec46948c091aba4f8098a62d",
)
COAUTHOR_TRAILER = re.compile(
    rb"(?m)^Co-authored-by: ([^<>\r\n\0]+) <([^<>\s\0]+)>\r?$"
)


def _identity_digest(name: bytes, email: bytes) -> str:
    return hashlib.sha256(name + b"\0" + email).hexdigest()


def _mask_reviewed_contributor_trailers(message: bytes) -> bytes:
    """Replace only exact listed Co-authored-by addresses, for the email rule."""
    def replace(match: re.Match[bytes]) -> bytes:
        if _identity_digest(match.group(1), match.group(2)) not in REVIEWED_CONTRIBUTOR_EMAIL_IDENTITIES:
            return match.group(0)
        start, end = match.span(2)
        base = match.start()
        whole = match.group(0)
        return whole[: start - base] + b"reviewed@example.invalid" + whole[end - base :]
    return COAUTHOR_TRAILER.sub(replace, message)
IGNORED_DIRECTORIES = {".git", ".venv", ".pytest_cache", "__pycache__", "node_modules"}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".cer",
    ".crt",
    ".der",
    ".jks",
    ".key",
    ".keytab",
    ".keystore",
    ".p12",
    ".pfx",
    ".pem",
    ".sqlite",
    ".sqlite3",
}
FORBIDDEN_NAMES = {
    ".env",
    "id_rsa",
    "id_ed25519",
    "credentials",
    "credentials.json",
    "secrets.json",
}
ALLOWED_DOCUMENTATION_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "192.0.2.0/24",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "2001:db8::/32",
    )
)
IPV4 = re.compile(rb"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
IPV6 = re.compile(
    rb"(?<![A-Za-z0-9_:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}"
    rb"(?![A-Za-z0-9_:])"
)
EMAIL = re.compile(
    rb"(?i)(?<![a-z0-9._%+\[\]-])[a-z0-9._%+\[\]-]{1,64}"
    rb"@([a-z0-9.-]{1,253}\.[a-z]{2,63})(?![a-z0-9.-])"
)
# Exact public automation addresses. Brackets are part of the local part so
# Dependabot's no-reply identity is inspected rather than skipped by the grammar.
# Deny terms still apply. The support mailbox is not an identity allowance.
PUBLIC_BOT_NOREPLY_ADDRESSES = frozenset(
    {
        b"49699333+dependabot[bot]@users.noreply.github.com",
        b"noreply@" + b"github.com",
        # Cursor cloud agent commit identity. Exact address only.
        b"cursoragent@" + b"cursor.com",
    }
)
DEPENDABOT_SIGNOFF_LINE = b"Signed-off-by: dependabot[bot] <support@" + b"github.com>"
PUBLIC_GITHUB_CLONE_URL = re.compile(
    r"(?:git@github\.com:|https://github\.com/)"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/"
    r"[A-Za-z0-9._-]{1,100}\.git"
)
GITHUB_NOREPLY_IDENTITY = re.compile(
    r"(?P<account>[0-9]+)\+(?P<username>[A-Za-z0-9-]+)"
    r"@users\.noreply\.github\.com"
)
HOME_PATH = re.compile(rb"(?i)/(?:home|users)/([A-Za-z0-9._-]{1,64})/")
WINDOWS_HOME_PATH = re.compile(
    rb"(?i)(?:[A-Z]:)?\\(?:Users|Documents[ ]and[ ]Settings)\\([^\\/\r\n]{1,128})\\"
)
INTERNAL_FQDN = re.compile(
    rb"(?i)(?<![A-Za-z0-9_-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    rb"(?:corp|internal|intranet|lan|local|localdomain|pvt)(?![A-Za-z0-9_.(-])"
)
QUOTED_SECRET_ASSIGNMENT = re.compile(
    rb"(?i)(?:access[_-]?token|api[_-]?key|client[_-]?secret|password|passwd|secret|token)"
    rb"\s*[:=]\s*(['\"])([A-Za-z0-9_./+%@=:-]{20,4096})\1"
)
UNQUOTED_SECRET_ASSIGNMENT = re.compile(
    rb"(?im)(?:access[_-]?token|api[_-]?key|client[_-]?secret|password|passwd|secret|token)"
    rb"\s*[:=]\s*([A-Za-z0-9_./+%@=:-]{20,4096})[ \t]*(?:[#;][^\r\n]*)?$"
)
BEARER_TOKEN = re.compile(
    rb"(?i)\bbearer[ \t]+([A-Za-z0-9._~+/=-]{20,4096})(?![A-Za-z0-9._~+/=-])"
)
PEM_MARKERS = tuple(
    (rule, b"-----BEGIN " + label + b"-----")
    for rule, label in (
        ("private-key-material", b"PRIVATE KEY"),
        ("private-key-material", b"RSA PRIVATE KEY"),
        ("private-key-material", b"EC PRIVATE KEY"),
        ("private-key-material", b"OPENSSH PRIVATE KEY"),
        ("private-key-material", b"ENCRYPTED PRIVATE KEY"),
        ("certificate-material", b"CERTIFICATE"),
        ("certificate-material", b"X509 CERTIFICATE"),
        ("certificate-material", b"CERTIFICATE REQUEST"),
        ("certificate-material", b"NEW CERTIFICATE REQUEST"),
        ("certificate-material", b"PKCS7"),
    )
)
SAFE_LITERAL_PREFIXES = (
    b"synthetic",
    b"example",
    b"placeholder",
    b"change-me",
    b"changeme",
    b"not-a-real",
    b"dry-run",
    b"dummy",
    b"fake-",
    b"fixture-",
    b"test-",
)
SAFE_UNQUOTED_VALUE_PREFIXES = (
    b"/",
    b"./",
    b"../",
    b"self.",
    b"settings.",
    b"config.",
    b"options.",
    b"args.",
)
MEDIA_SUFFIXES = {
    ".avi",
    ".flac",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
    ".wma",
    ".wmv",
}
IMAGE_TYPES = {".gif": "GIF", ".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
MEDIA_REVIEW_FILE = Path(__file__).with_name("publication-media-reviews.json")
MEDIA_APPROVERS_FILE = Path(__file__).with_name("publication-approvers.json")
MAX_DERIVED_SCAN_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 5_000


@dataclass(frozen=True)
class Finding:
    location: str
    rule: str


def _candidate_files(root: Path) -> Iterable[Path]:
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names if name not in IGNORED_DIRECTORIES)
        base = Path(directory)
        for name in sorted(files):
            if name in IGNORED_DIRECTORIES:
                continue
            yield base / name


def _safe_example_ip(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return any(address in network for network in ALLOWED_DOCUMENTATION_NETWORKS)


def _previous_fixture_home_owners() -> frozenset[str]:
    """Load retained fixture owners bundled with the reviewed scanner."""
    path = Path(__file__).with_name("publication_home_owners.py")
    if not path.is_file():
        return frozenset()
    import importlib.util

    spec = importlib.util.spec_from_file_location("_publication_home_owners", path)
    if spec is None or spec.loader is None:
        return frozenset()
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    values = getattr(module, "PREVIOUS_FIXTURE_HOME_OWNERS", ())
    return frozenset(item.casefold() for item in values if isinstance(item, str))


_UNIX_HOME_OWNERS = frozenset({"exculpata", "user", "example", *_previous_fixture_home_owners()})
_WINDOWS_HOME_OWNERS = frozenset({"default", "example", "public", "exculpata", "user", *_previous_fixture_home_owners()})


def _recognized_public_automation_email(data: bytes, match: re.Match[bytes]) -> bool:
    """True only for exact public automation addresses or Dependabot's sign-off line."""
    address = match.group(0).lower()
    if address in PUBLIC_BOT_NOREPLY_ADDRESSES:
        return True
    if address != b"support@" + b"github.com":
        return False
    line_start = data.rfind(b"\n", 0, match.start()) + 1
    line_end = data.find(b"\n", match.end())
    if line_end < 0:
        line_end = len(data)
    line = data[line_start:line_end]
    if line.endswith(b"\r"):
        line = line[:-1]
    return line == DEPENDABOT_SIGNOFF_LINE


def _mask_exact_public_literals(
    data: bytes, literals: tuple[bytes, ...]
) -> bytes:
    masked = data
    token_character = rb"A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-"
    for literal in literals:
        if not literal:
            continue
        pattern = re.compile(
            rb"(?<![" + token_character + rb"])"
            + re.escape(literal)
            + rb"(?!["
            + token_character
            + rb"])"
        )
        masked = pattern.sub(lambda match: b"\0" * len(match.group(0)), masked)
    return masked


def _scan_bytes(
    data: bytes,
    *,
    location: str,
    deny: tuple[bytes, ...],
    allowed_deny_literals: tuple[bytes, ...] = (),
) -> list[Finding]:
    findings: list[Finding] = []
    lowered = data.lower()
    pem_rules: set[str] = set()
    for rule, marker in PEM_MARKERS:
        if marker.lower() in lowered:
            pem_rules.add(rule)
    findings.extend(Finding(location, rule) for rule in sorted(pem_rules))
    network_data = data
    logical_path = re.sub(r"^git:[0-9a-f]{12}:", "", location)
    reviewed_cuda_versions = {
        "services/transcription/requirements-nemotron.lock": rb"10[.]3[.](?:9[.]90|7[.]77)",
        "services/transcription/requirements-worker.lock": rb"10[.]3[.](?:9[.]90|7[.]77)",
        "requirements/retrieval.lock": rb"10[.]3[.]7[.]77",
    }
    if logical_path in reviewed_cuda_versions:
        # Reviewed public dependency coordinates, not network endpoints. Exact
        # file, package, version and line grammar only; other rules retain bytes.
        network_data = re.sub(
            rb"(?m)^nvidia-curand-cu12==" + reviewed_cuda_versions[logical_path] + rb"(?: \\)?$",
            b"reviewed-public-dependency-version", data,
        )
    for raw in (*IPV4.findall(network_data), *IPV6.findall(network_data)):
        try:
            address = ipaddress.ip_address(raw.decode("ascii"))
        except ValueError:
            continue
        if address.is_loopback or address.is_unspecified or _safe_example_ip(address):
            continue
        if address.is_private or address.is_link_local:
            findings.append(Finding(location, "private-network-address"))
            break
    for match in EMAIL.finditer(data):
        if _recognized_public_automation_email(data, match):
            continue  # Exact public bot identity or Dependabot sign-off; deny rules still apply.
        domain = match.group(1).decode("ascii", "ignore").casefold()
        if domain not in {
            "example.com",
            "example.net",
            "example.org",
            "example.test",
            "example.invalid",
            "users.noreply.github.com",
        } and not domain.endswith(
            ".example.test"
        ):
            findings.append(Finding(location, "non-example-email-address"))
            break
    for match in HOME_PATH.finditer(data):
        owner = match.group(1).decode("ascii", "ignore").casefold()
        if owner not in _UNIX_HOME_OWNERS:
            findings.append(Finding(location, "personal-home-path"))
            break
    for match in WINDOWS_HOME_PATH.finditer(data):
        owner = match.group(1).decode("ascii", "ignore").casefold()
        if owner not in _WINDOWS_HOME_OWNERS:
            findings.append(Finding(location, "personal-home-path"))
            break
    if INTERNAL_FQDN.search(data):
        findings.append(Finding(location, "internal-fqdn"))
    quoted_values = (
        match.group(2) for match in QUOTED_SECRET_ASSIGNMENT.finditer(data)
    )
    unquoted_values = (
        match.group(1) for match in UNQUOTED_SECRET_ASSIGNMENT.finditer(data)
    )
    for literal in quoted_values:
        if not literal.lower().startswith(SAFE_LITERAL_PREFIXES):
            findings.append(Finding(location, "literal-secret-shaped-value"))
            break
    else:
        for literal in unquoted_values:
            lowered_literal = literal.lower()
            if lowered_literal.startswith(
                (*SAFE_LITERAL_PREFIXES, *SAFE_UNQUOTED_VALUE_PREFIXES)
            ):
                continue
            findings.append(Finding(location, "literal-secret-shaped-value"))
            break
    for match in BEARER_TOKEN.finditer(data):
        literal = match.group(1).lower()
        if not literal.startswith(SAFE_LITERAL_PREFIXES):
            findings.append(Finding(location, "bearer-token"))
            break
    deny_source = _mask_exact_public_literals(data, allowed_deny_literals).lower()
    for term in deny:
        if term and term.lower() in deny_source:
            findings.append(Finding(location, "operator-deny-term"))
            break
    return findings


def _scan_pdf(data: bytes, *, location: str, deny: tuple[bytes, ...]) -> list[Finding]:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            return [Finding(location, "encrypted-document")]
        parts: list[str] = []
        if reader.metadata is not None:
            parts.extend(str(value) for value in reader.metadata.values() if value)
        size = sum(len(value.encode("utf-8", errors="replace")) for value in parts)
        for page in reader.pages:
            value = page.extract_text() or ""
            size += len(value.encode("utf-8", errors="replace"))
            if size > MAX_DERIVED_SCAN_BYTES:
                return [Finding(location, "expanded-document-too-large")]
            parts.append(value)
    except Exception:
        return [Finding(location, "document-inspection-failed")]
    return _scan_bytes(
        "\n".join(parts).encode("utf-8", errors="replace"),
        location=f"{location}#pdf-content",
        deny=deny,
    )


def _metadata_content(value) -> bytes:
    """Inspect decoded values, including nested and binary ExifTool metadata."""
    if isinstance(value, dict):
        return b"\n".join(_metadata_content(part) for pair in value.items() for part in pair)
    if isinstance(value, list):
        return b"\n".join(_metadata_content(part) for part in value)
    if isinstance(value, str):
        literal = value.encode("utf-8", errors="replace")
        if value.startswith("base64:"):
            return literal + b"\n" + base64.b64decode(value[7:], validate=True)
        return literal
    return str(value).encode("utf-8")


def _run_metadata_tool(arguments: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess:
    """Bound combined stdout/stderr while draining both pipes under one deadline."""
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output, errors = bytearray(), bytearray()
    size = 0
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, output)
            selector.register(process.stderr, selectors.EVENT_READ, errors)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(arguments, timeout)
                events = selector.select(remaining)
                if not events:
                    raise subprocess.TimeoutExpired(arguments, timeout)
                for key, _ in events:
                    chunk = os.read(key.fd, min(65536, MAX_DERIVED_SCAN_BYTES - size + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    size += len(chunk)
                    if size > MAX_DERIVED_SCAN_BYTES:
                        raise ValueError("Metadata output exceeds inspection limit")
                    key.data.extend(chunk)
            returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
        return subprocess.CompletedProcess(arguments, returncode, bytes(output), bytes(errors))
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        process.stderr.close()


def _scan_media(
    data: bytes,
    *,
    name: str,
    location: str,
    deny: tuple[bytes, ...],
) -> list[Finding]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return [Finding(location, "media-inspection-unavailable")]
    try:
        with tempfile.NamedTemporaryFile(
            prefix="exculpata-publication-",
            suffix=Path(name).suffix.casefold(),
        ) as handle:
            handle.write(data)
            handle.flush()
            result = _run_metadata_tool(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format_tags:stream_tags:chapter_tags",
                    "-of",
                    "json",
                    handle.name,
                ],
            )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return [Finding(location, "media-inspection-failed")]
    if result.returncode != 0 or len(result.stdout) > MAX_DERIVED_SCAN_BYTES:
        return [Finding(location, "media-inspection-failed")]
    try:
        tags = json.loads(result.stdout)
        if not isinstance(tags, dict):
            raise ValueError
        content = _metadata_content(tags)
    except (UnicodeDecodeError, ValueError):
        return [Finding(location, "media-inspection-failed")]
    return _scan_bytes(
        content,
        location=f"{location}#media-metadata",
        deny=deny,
    )


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _media_maintainers() -> set[str]:
    metadata = MEDIA_APPROVERS_FILE.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 64 * 1024:
        raise ValueError("Invalid media approvers")
    policy = json.loads(MEDIA_APPROVERS_FILE.read_text(), object_pairs_hook=_unique_json_object)
    if (not isinstance(policy, dict) or set(policy) != {"version", "maintainers"}
            or type(policy["version"]) is not int or policy["version"] != 1
            or not isinstance(policy["maintainers"], list)):
        raise ValueError("Invalid media approvers")
    names = set()
    ids = set()
    for entry in policy["maintainers"]:
        if (not isinstance(entry, dict) or set(entry) != {"login", "id"} or not isinstance(entry["login"], str)
                or re.fullmatch(r"[A-Za-z0-9-]{1,39}", entry["login"]) is None
                or type(entry["id"]) is not int or entry["id"] <= 0
                or entry["login"] in names or entry["id"] in ids):
            raise ValueError("Invalid media approvers")
        names.add(entry["login"])
        ids.add(entry["id"])
    if not names:
        raise ValueError("No media approvers")
    return names


def _reviewed_binary_media(data: bytes, suffix: str) -> bool:
    """Policy lives beside the trusted scanner, never in the candidate tree."""
    try:
        metadata = MEDIA_REVIEW_FILE.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 512 * 1024:
            raise ValueError
        policy = json.loads(MEDIA_REVIEW_FILE.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object)
        if (not isinstance(policy, dict) or set(policy) != {"version", "reviews"}
                or type(policy["version"]) is not int or policy["version"] != 2):
            raise ValueError
        if not isinstance(policy["reviews"], list):
            raise ValueError
        maintainers = _media_maintainers()
        seen = set()
        digest = hashlib.sha256(data).hexdigest()
        approved = False
        for entry in policy["reviews"]:
            if not isinstance(entry, dict) or set(entry) != {"sha256", "suffix", "reason", "approver", "expires", "status", "approved_on"}:
                raise ValueError
            if any(not isinstance(entry[key], str) or not entry[key].strip()
                   for key in ("sha256", "suffix", "reason", "expires", "status")):
                raise ValueError
            if entry["status"] == "pending":
                if entry["approver"] is not None or entry["approved_on"] is not None:
                    raise ValueError
            elif entry["status"] == "approved":
                if entry["approver"] not in maintainers or not isinstance(entry["approved_on"], str):
                    raise ValueError
            else:
                raise ValueError
            if re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None:
                raise ValueError
            if entry["suffix"] not in MEDIA_SUFFIXES | IMAGE_TYPES.keys():
                raise ValueError
            key = (entry["sha256"], entry["suffix"])
            if key in seen or re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["expires"]) is None:
                raise ValueError
            seen.add(key)
            expiry = date.fromisoformat(entry["expires"])
            if entry["status"] == "pending":
                continue
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["approved_on"]) is None:
                raise ValueError
            approved_on = date.fromisoformat(entry["approved_on"])
            if not 0 < (expiry - approved_on).days <= 180 or approved_on > date.today():
                raise ValueError
            if key == (digest, suffix) and expiry >= date.today():
                approved = True
        return approved
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError("Media review policy is unavailable or invalid") from None


def _scan_image(data: bytes, *, name: str, location: str, deny: tuple[bytes, ...]) -> list[Finding]:
    exiftool = shutil.which("exiftool")
    if exiftool is None:
        return [Finding(location, "media-inspection-unavailable")]
    try:
        with tempfile.NamedTemporaryFile(prefix="exculpata-publication-", suffix=Path(name).suffix) as handle:
            handle.write(data)
            handle.flush()
            result = _run_metadata_tool(
                [exiftool, "-config", "", "-j", "-a", "-G1:4", "-u", "-U", "-ee", "-b", handle.name],
            )
        if result.returncode or result.stderr or len(result.stdout) > MAX_DERIVED_SCAN_BYTES:
            raise ValueError
        entries = json.loads(result.stdout)
        if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
            raise ValueError
        tags = entries[0]
        if tags.get("File:FileType") != IMAGE_TYPES[Path(name).suffix.casefold()]:
            raise ValueError
        if any(key.rsplit(":", 1)[-1] in {"Warning", "Error"} for key in tags):
            raise ValueError
        # These fields describe the temporary inspection file, not asset metadata.
        for key in tuple(tags):
            if key == "SourceFile" or key.startswith("System:"):
                del tags[key]
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return [Finding(location, "media-inspection-failed")]
    try:
        content = _metadata_content(tags)
    except ValueError:
        return [Finding(location, "media-inspection-failed")]
    return _scan_bytes(content, location=f"{location}#media-metadata", deny=deny)



def _scan_archive(data: bytes, *, location: str, deny: tuple[bytes, ...]) -> list[Finding]:
    findings: list[Finding] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                return [Finding(location, "archive-member-limit")]
            expanded = 0
            for index, member in enumerate(members, start=1):
                member_location = f"{location}!member[{index}]"
                findings.extend(
                    _scan_bytes(
                        member.filename.encode("utf-8", errors="surrogateescape"),
                        location=f"{member_location}-name",
                        deny=deny,
                    )
                )
                member_path = PurePosixPath(member.filename)
                if (
                    member_path.is_absolute()
                    or ".." in member_path.parts
                    or "\\" in member.filename
                    or "\x00" in member.filename
                ):
                    findings.append(Finding(member_location, "unsafe-archive-member-name"))
                member_mode = member.external_attr >> 16
                if stat.S_ISLNK(member_mode):
                    findings.append(Finding(member_location, "symbolic-link"))
                if member.is_dir():
                    continue
                if (
                    member_path.name in FORBIDDEN_NAMES
                    or member_path.suffix.casefold() in FORBIDDEN_SUFFIXES
                ):
                    findings.append(Finding(member_location, "runtime-or-credential-file"))
                    continue
                if member.flag_bits & 0x1:
                    return [Finding(location, "encrypted-document")]
                expanded += member.file_size
                if (
                    member.file_size > MAX_DERIVED_SCAN_BYTES
                    or expanded > MAX_DERIVED_SCAN_BYTES
                ):
                    return [Finding(location, "expanded-document-too-large")]
                findings.extend(
                    _scan_bytes(
                        archive.read(member),
                        location=member_location,
                        deny=deny,
                    )
                )
    except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return [Finding(location, "document-inspection-failed")]
    return findings


# One- or two-character local parts plus a four-character domain are six or
# seven bytes. Only an exact reviewed media blob may ignore that collision.
_SHORT_REVIEWED_EMAIL_COLLISION_BYTES = 8


def _without_short_email_collisions(data: bytes) -> bytes:
    masked = bytearray(data)
    changed = False
    for match in EMAIL.finditer(data):
        if match.end() - match.start() >= _SHORT_REVIEWED_EMAIL_COLLISION_BYTES:
            continue
        at = data.find(b"@", match.start(), match.end())
        if at < 0:
            continue
        masked[at] = 0
        changed = True
    return bytes(masked) if changed else data


def _scan_content(
    data: bytes,
    *,
    name: str,
    location: str,
    deny: tuple[bytes, ...],
    allowed_deny_literals: tuple[bytes, ...] = (),
    reviewed_media_digests: tuple[str, ...] = (),
) -> list[Finding]:
    # Only a manually reviewed, exact media blob may adjudicate accidental
    # three-byte deny terms and sub-8-byte email-shaped runs in its raw bytes.
    # Longer matches, other files, and decoded metadata retain every check.
    raw_deny = deny
    scanned = data
    if (Path(name).suffix.casefold() in MEDIA_SUFFIXES
            and hashlib.sha256(data).hexdigest() in reviewed_media_digests):
        raw_deny = tuple(term for term in deny if len(term) != 3)
        scanned = _without_short_email_collisions(data)
    suffix = Path(name).suffix.casefold()
    approved = False
    policy_findings = []
    if suffix in MEDIA_SUFFIXES | IMAGE_TYPES.keys():
        try:
            approved = _reviewed_binary_media(data, suffix)
        except ValueError:
            policy_findings.append(Finding(location, "media-review-policy-invalid"))
    findings = policy_findings + ([] if approved else _scan_bytes(
        scanned,
        location=location,
        deny=raw_deny,
        allowed_deny_literals=allowed_deny_literals,
    ))
    if suffix == ".pdf":
        findings.extend(_scan_pdf(data, location=location, deny=deny))
    elif zipfile.is_zipfile(io.BytesIO(data)):
        findings.extend(_scan_archive(data, location=location, deny=deny))
    if suffix in IMAGE_TYPES:
        findings.extend(_scan_image(data, name=name, location=location, deny=deny))
    elif suffix in MEDIA_SUFFIXES:
        findings.extend(_scan_media(data, name=name, location=location, deny=deny))
    return findings


def scan_tree(
    root: Path,
    deny: tuple[bytes, ...],
    *,
    public_clone_urls: tuple[bytes, ...] = (),
    reviewed_media_digests: tuple[str, ...] = (),
) -> list[Finding]:
    findings: list[Finding] = []
    for index, path in enumerate(_candidate_files(root), start=1):
        relative = path.relative_to(root).as_posix()
        name_findings = _scan_bytes(
            relative.encode("utf-8", errors="surrogateescape"),
            location=f"tree-entry[{index}]-name",
            deny=deny,
        )
        findings.extend(name_findings)
        location = relative if not name_findings else f"tree-entry[{index}]"
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            findings.append(Finding(location, "symbolic-link"))
            continue
        if not stat.S_ISREG(metadata.st_mode):
            findings.append(Finding(location, "non-regular-file"))
            continue
        if path.name in FORBIDDEN_NAMES or path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            findings.append(Finding(location, "runtime-or-credential-file"))
            continue
        if metadata.st_size > 25 * 1024 * 1024:
            findings.append(Finding(location, "unexpected-large-file"))
            continue
        try:
            data = path.read_bytes()
        except OSError:
            findings.append(Finding(location, "unreadable-file"))
            continue
        findings.extend(
            _scan_content(
                data,
                name=relative,
                location=location,
                deny=deny,
                allowed_deny_literals=(
                    public_clone_urls if relative == "README.md" else ()
                ),
                reviewed_media_digests=reviewed_media_digests,
            )
        )
    return findings


def scan_history(
    root: Path,
    deny: tuple[bytes, ...],
    *,
    public_clone_urls: tuple[bytes, ...] = (),
    public_git_identities: tuple[tuple[bytes, bytes], ...] = (),
    baseline_public_git_identities: tuple[tuple[str, bytes, bytes], ...] = (),
    reviewed_media_digests: tuple[str, ...] = (),
    public_merge_commits: tuple[str, ...] = (),
) -> list[Finding]:
    if not (root / ".git").exists():
        return [Finding(".git", "git-history-unavailable")]
    check = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check.returncode != 0:
        return []
    revisions = subprocess.run(
        ["git", "rev-list", "--all"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if revisions.returncode != 0:
        return [Finding("git-history", "history-scan-failed")]
    findings: list[Finding] = []
    revision_ids = {
        raw.decode("ascii", errors="strict")
        for raw in revisions.stdout.splitlines()
    }
    baseline_identities: dict[str, set[tuple[bytes, bytes]]] = {}
    for boundary, name, email in baseline_public_git_identities:
        if boundary not in revision_ids:
            findings.append(Finding("git-metadata", "history-scan-failed"))
            continue
        if hashlib.sha256(name + b"\0" + email).hexdigest() in REVIEWED_COMMIT_EMAIL_IDENTITIES.get(boundary, ()):
            # A reviewed personal address is accepted at this commit only.
            # Unlike legacy no-reply baselines, it never covers ancestors.
            baseline_identities.setdefault(boundary, set()).add((name, email))
            continue
        baseline = subprocess.run(
            ["git", "rev-list", boundary],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if baseline.returncode != 0:
            findings.append(Finding("git-metadata", "history-scan-failed"))
            continue
        for raw_commit in baseline.stdout.splitlines():
            commit = raw_commit.decode("ascii", errors="strict")
            baseline_identities.setdefault(commit, set()).add((name, email))
    scanned: set[tuple[str, str]] = set()
    for raw_commit in revisions.stdout.splitlines():
        commit = raw_commit.decode("ascii", errors="strict")
        tree = subprocess.run(
            ["git", "ls-tree", "-r", "-z", "--long", commit],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if tree.returncode != 0:
            findings.append(Finding(f"git:{commit[:12]}", "history-scan-failed"))
            continue
        for index, entry in enumerate(tree.stdout.split(b"\0"), start=1):
            if not entry:
                continue
            try:
                metadata, raw_name = entry.split(b"\t", 1)
                mode, kind, raw_oid, raw_size = metadata.split(b" ", 3)
                name = raw_name.decode("utf-8", errors="surrogateescape")
                oid = raw_oid.decode("ascii")
                size = int(raw_size)
            except (ValueError, UnicodeError):
                findings.append(Finding(f"git:{commit[:12]}", "history-scan-failed"))
                continue
            name_findings = _scan_bytes(
                raw_name,
                location=f"git:{commit[:12]}:entry[{index}]-name",
                deny=deny,
            )
            findings.extend(name_findings)
            location = (
                f"git:{commit[:12]}:{name}"
                if not name_findings
                else f"git:{commit[:12]}:entry[{index}]"
            )
            if mode == b"120000":
                findings.append(Finding(location, "symbolic-link"))
                continue
            if kind != b"blob":
                findings.append(Finding(location, "non-regular-file"))
                continue
            path = Path(name)
            if path.name in FORBIDDEN_NAMES or path.suffix.casefold() in FORBIDDEN_SUFFIXES:
                findings.append(Finding(location, "runtime-or-credential-file"))
                continue
            if size > 25 * 1024 * 1024:
                findings.append(Finding(location, "unexpected-large-file"))
                continue
            identity = (oid, name)
            if identity in scanned:
                continue
            scanned.add(identity)
            blob = subprocess.run(
                ["git", "cat-file", "blob", oid],
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if blob.returncode != 0:
                findings.append(Finding(location, "history-scan-failed"))
                continue
            findings.extend(
                _scan_content(
                    blob.stdout,
                    name=name,
                    location=location,
                    deny=deny,
                    allowed_deny_literals=(
                        public_clone_urls if name == "README.md" else ()
                    ),
                    reviewed_media_digests=reviewed_media_digests,
                )
            )
    for commit in sorted(revision_ids):
        metadata = subprocess.run(
            [
                "git",
                "show",
                "-s",
                "--format=%an%x00%ae%x00%cn%x00%ce%x00%B",
                commit,
            ],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        parts = metadata.stdout.split(b"\0", 4)
        if metadata.returncode != 0 or len(parts) != 5:
            findings.append(Finding("git-metadata", "history-scan-failed"))
            continue
        author_name, author_email, committer_name, committer_email, message = parts
        allowed_identities = set(public_git_identities)
        allowed_identities.update(baseline_identities.get(commit, set()))
        for name, email in (
            (author_name, author_email),
            (committer_name, committer_email),
        ):
            identity_findings = _scan_bytes(
                name + b"\0" + email,
                location="git-metadata",
                deny=deny,
                allowed_deny_literals=(
                    (name, email) if (name, email) in allowed_identities else ()
                ),
            )
            identity_digest = hashlib.sha256(name + b"\0" + email).hexdigest()
            if (identity_digest in REVIEWED_COMMIT_EMAIL_IDENTITIES.get(commit, ())
                    or identity_digest in REVIEWED_CONTRIBUTOR_EMAIL_IDENTITIES):
                identity_findings = [
                    item for item in identity_findings
                    if item.rule != "non-example-email-address"
                ]
            findings.extend(identity_findings)
        allowed_spans = []
        if commit in public_merge_commits:
            for name, email in public_git_identities:
                pattern = rb"\AMerge pull request #[1-9][0-9]* from (" + re.escape(name) + rb")/"
                match = re.match(pattern, message)
                if match:
                    allowed_spans.append(match.span(1))
                # GitHub squash merges can retain an explicit co-author trailer.
                # Only the reviewed no-reply identity on this exact commit is
                # eligible, and neither surrounding text nor other names pass.
                trailer = (rb"(?m)^Co-authored-by: (" + re.escape(name)
                           + rb" <" + re.escape(email) + rb">)$")
                allowed_spans.extend(match.span(1) for match in re.finditer(trailer, message))
        # Scan generic rules against the original bytes. Only deny matches wholly
        # inside the exact reviewed attribution username may be adjudicated.
        message_findings = _scan_bytes(message, location="git-metadata", deny=())
        masked = _mask_reviewed_contributor_trailers(message)
        if masked != message:
            # Every other rule keeps the original bytes; only the email rule
            # is re-judged with exact reviewed contributor trailers masked.
            message_findings = [
                item for item in message_findings if item.rule != "non-example-email-address"
            ] + [
                item for item in _scan_bytes(masked, location="git-metadata", deny=())
                if item.rule == "non-example-email-address"
            ]
        findings.extend(message_findings)
        lowered = message.lower()
        for term in deny:
            offset = 0
            while term and (start := lowered.find(term.lower(), offset)) >= 0:
                end = start + len(term)
                if not any(left <= start and end <= right for left, right in allowed_spans):
                    findings.append(Finding("git-metadata", "operator-deny-term"))
                    break
                offset = start + 1
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)%00%(objectname)", "refs/heads", "refs/tags"],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if refs.returncode != 0:
        findings.append(Finding("git-refs", "history-scan-failed"))
        return findings
    inspected_tags: set[bytes] = set()
    for ref in refs.stdout.splitlines():
        name, oid = ref.split(b"\0", 1)
        findings.extend(_scan_bytes(name, location="git-refs", deny=deny))
        if not name.startswith(b"refs/tags/"):
            continue
        for _ in range(10_000):
            if oid in inspected_tags:
                break
            inspected_tags.add(oid)
            kind = subprocess.run(["git", "cat-file", "-t", oid], cwd=root,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if kind.returncode:
                findings.append(Finding("git-refs", "history-scan-failed"))
                break
            if kind.stdout.strip() == b"commit":
                break
            if kind.stdout.strip() != b"tag":
                findings.append(Finding("git-refs", "unsupported-tag-target"))
                break
            tag = subprocess.run(["git", "cat-file", "tag", oid], cwd=root,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if tag.returncode:
                findings.append(Finding("git-refs", "history-scan-failed"))
                break
            target = re.match(rb"object ([0-9a-f]{40}|[0-9a-f]{64})\ntype (?:commit|tag|tree|blob)\n", tag.stdout)
            if not target:
                findings.append(Finding("git-refs", "history-scan-failed"))
                break
            findings.extend(_scan_bytes(tag.stdout[target.end():], location="git-refs", deny=deny))
            oid = target.group(1)
        else:
            findings.append(Finding("git-refs", "history-scan-failed"))
    return findings


def _deny_terms(path: Path | None) -> tuple[bytes, ...]:
    if path is None:
        return ()
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("deny-term file is unsafe")
    result = []
    for raw in path.read_bytes().splitlines():
        value = raw.strip()
        if value and not value.startswith(b"#"):
            if len(value) < 3 or len(value) > 512:
                raise RuntimeError("deny-term file contains an invalid entry")
            result.append(value)
    return tuple(result)


def _ascii_value(value: str, *, label: str, maximum: int) -> bytes:
    if (
        not value
        or len(value) > maximum
        or any(character in value for character in "\r\n\0")
    ):
        raise RuntimeError(f"{label} is invalid")
    try:
        return value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise RuntimeError(f"{label} must be ASCII") from exc


def _public_clone_urls(values: Iterable[str]) -> tuple[bytes, ...]:
    result: list[bytes] = []
    for value in values:
        if PUBLIC_GITHUB_CLONE_URL.fullmatch(value) is None:
            raise RuntimeError(
                "public clone URL must be an exact GitHub SSH or HTTPS clone URL"
            )
        result.append(value.encode("ascii"))
    return tuple(result)


def _public_identity(name: str, email: str, *, current: bool) -> tuple[bytes, bytes]:
    encoded_name = _ascii_value(name, label="Public Git identity name", maximum=100)
    encoded_email = _ascii_value(email, label="Public Git identity email", maximum=254)
    matched = GITHUB_NOREPLY_IDENTITY.fullmatch(email)
    if matched is None:
        raise RuntimeError(
            "public Git identity must use a verified GitHub no-reply address"
        )
    if current and name != matched.group("username"):
        raise RuntimeError(
            "current public Git identity name must match its GitHub username"
        )
    return encoded_name, encoded_email


def _public_git_identities(
    values: Iterable[tuple[str, str]],
) -> tuple[tuple[bytes, bytes], ...]:
    return tuple(_public_identity(name, email, current=True) for name, email in values)


def _baseline_public_git_identities(
    values: Iterable[tuple[str, str, str]],
) -> tuple[tuple[str, bytes, bytes], ...]:
    result: list[tuple[str, bytes, bytes]] = []
    for boundary, name, email in values:
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", boundary) is None:
            raise RuntimeError("public baseline boundary must be an exact commit SHA")
        encoded_name = _ascii_value(name, label="Public Git identity name", maximum=100)
        encoded_email = _ascii_value(email, label="Public Git identity email", maximum=254)
        digest = hashlib.sha256(encoded_name + b"\0" + encoded_email).hexdigest()
        if digest not in REVIEWED_COMMIT_EMAIL_IDENTITIES.get(boundary, ()):
            encoded_name, encoded_email = _public_identity(name, email, current=False)
        result.append((boundary, encoded_name, encoded_email))
    return tuple(result)


def _legacy_product_name_findings(root: Path) -> list[Finding]:
    """Working tree only. History still contains the previous product name."""
    checker = Path(__file__).with_name("legacy_name_check.py")
    if not checker.is_file():
        return []
    import importlib.util

    spec = importlib.util.spec_from_file_location("_legacy_name_check", checker)
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [Finding(str(item.location), "legacy-product-name") for item in module.findings(root)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--deny-file", type=Path)
    parser.add_argument("--allow-public-clone-url", action="append", default=[])
    parser.add_argument(
        "--allow-public-git-identity",
        action="append",
        nargs=2,
        default=[],
        metavar=("NAME", "EMAIL"),
    )
    parser.add_argument(
        "--allow-public-baseline-git-identity",
        action="append",
        nargs=3,
        default=[],
        metavar=("COMMIT", "NAME", "EMAIL"),
    )
    parser.add_argument("--skip-history", action="store_true")
    parser.add_argument("--allow-reviewed-media-digest", action="append", default=[])
    parser.add_argument("--allow-public-merge-commit", action="append", default=[])
    args = parser.parse_args()
    root = args.root.expanduser().resolve(strict=True)
    deny = _deny_terms(args.deny_file.expanduser()) if args.deny_file else ()
    clone_urls = _public_clone_urls(args.allow_public_clone_url)
    identities = _public_git_identities(args.allow_public_git_identity)
    baseline_identities = _baseline_public_git_identities(
        args.allow_public_baseline_git_identity
    )
    media_digests = tuple(dict.fromkeys((*REVIEWED_MEDIA_DIGESTS, *args.allow_reviewed_media_digest)))
    merge_commits = tuple(args.allow_public_merge_commit)
    if any(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None for value in merge_commits):
        raise RuntimeError("Public merge must be an exact commit")
    if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in media_digests):
        raise RuntimeError("Reviewed media digest must be an exact SHA-256")
    findings = scan_tree(root, deny, public_clone_urls=clone_urls, reviewed_media_digests=media_digests)
    findings.extend(_legacy_product_name_findings(root))
    if not args.skip_history:
        findings.extend(
            scan_history(
                root,
                deny,
                public_clone_urls=clone_urls,
                public_git_identities=identities,
                baseline_public_git_identities=baseline_identities,
                reviewed_media_digests=media_digests,
                public_merge_commits=merge_commits,
            )
        )
    unique = sorted(set(findings), key=lambda item: (item.location, item.rule))
    if unique:
        print("PUBLICATION GATE: BLOCKED", file=sys.stderr)
        for finding in unique:
            print(f"  {finding.rule}: {finding.location}", file=sys.stderr)
        print("No matched secret value was printed.", file=sys.stderr)
        return 1
    print("PUBLICATION GATE: CLEAN")
    if args.skip_history:
        print("Working tree passed generic residue checks; Git history was not scanned.")
    else:
        print("Tree and available Git history passed generic residue checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
