#!/usr/bin/env python3
"""Fail when the previous product name appears outside the compatibility allowlist.

The scan reads the working tree only. Published history still uses the previous
name, and rewriting those commits is not part of this check.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOKEN = re.compile(r"recordbench", re.IGNORECASE)
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", "artifacts", ".tmp"}

# Whole files that must keep the previous spelling, with the reason.
ALLOWLIST: dict[str, str] = {
    "src/case_intelligence/legacy_names.py": "Defines the previous spelling once.",
    "scripts/legacy_name_check.py": "This checker and the reasons below.",
    "tests/test_legacy_name_gate.py": "Proves an unlisted use fails the gate.",
    "CHANGELOG.md": "Historical release entries keep the name they shipped with.",
    "docs/BRANDING.md": "States that the previous product name is now Exculpata.",
    "pyproject.toml": "Legacy console scripts remain installed and print a deprecation notice.",
    "scripts/pre-push-publication.py": "Reads both the current and previous git-config prefixes.",
    "scripts/recordbench_install.py": "Installer shim. Existing commands keep working.",
    "scripts/recordbench_backup.py": "Backup shim. Existing commands keep working.",
    "scripts/recordbench-backup.sh": "Shell shim for existing backup units.",
    "scripts/recordbench-backup-status.sh": "Shell shim for existing backup units.",
    "scripts/recordbench-restore-drill.sh": "Shell shim for existing restore drills.",
    "deploy/systemd/recordbench-backup.service.in": "Unit name for nodes already scheduled under the previous generation.",
    "deploy/systemd/recordbench-backup.timer.in": "Timer unit name for nodes already scheduled under the previous generation.",
    "docs/assets/recordbench-banner.svg": "Historical wordmark. The title text is the previous name.",
    "compose.yaml": "Postgres role/database, container volume targets, and env alias pairs stay so existing nodes start unchanged. Fresh project, image, HOME, and portable served-model defaults use the current name.",
    "compose.kerberos.yaml": "Kerberos overlay keeps the same volume targets and alias pairs as existing nodes.",
    "compose.local-accounts.yaml": "Local-account overlay keeps the accounts and secrets mounts existing nodes already use.",
    "scripts/compose-check.sh": "Renders both env prefixes and asserts the retained container mount targets.",
    "deploy/gateway/default.conf.template": "Clears previous proxy headers so a client cannot inject them.",
    ".gitignore": "Ignores local node files created under the previous generation.",
    ".dockerignore": "Ignores local node files created under the previous generation.",
    "config/auth.kerberos.env.example": "Example still shows retained secrets paths and group defaults.",
    "config/auth.local.env.example": "Example still shows the retained secrets path.",
    "config/auth.oidc.env.example": "Example still shows retained secrets paths and group defaults.",
    "config/exculpata.env.example": "Example still shows the retained secrets path and saved model id.",
    "config/transcription.env.example": "Example still shows retained worker and lock paths.",
}

# Subtrees whose in-image names match existing containers.
ALLOW_PREFIXES: dict[str, str] = {
    "deploy/kerberos-proxy/": "Apache site, keytab, and secret paths match containers already deployed.",
    "src/case_intelligence/migrations/": "Applied SQL function names cannot change without a migration rewrite.",
    "migrations/": "Applied SQL function names cannot change without a migration rewrite.",
}

# Tokens that remain in otherwise-renamed files, removed before the scan.
# Each pattern is the compatibility exception, not a general wildcard.
KEPT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"3\.8\.6\+recordbench\.1"), "Pinned WhisperX local version. Renaming it changes the wheel digest."),
    (re.compile(r"/opt/recordbench"), "Image workdir. It is rebuilt with the image and is not a host data path."),
    (re.compile(r"/var/lib/recordbench"), "Container data volume. Existing installs mount this path."),
    (re.compile(r"/run/recordbench-secrets"), "Container secrets mount. Existing installs mount this path."),
    (re.compile(r"/var/lib/recordbench-accounts"), "Container accounts volume. Existing installs mount this path."),
    (re.compile(r"/run/recordbench-auth"), "Kerberos runtime directory inside the proxy image."),
    (re.compile(r"recordbench\.keytab"), "Keytab filename stored for an existing node."),
    (re.compile(r"recordbench\.env"), "Configuration filename for a node created under the previous generation."),
    (re.compile(r"\.recordbench-managed-storage\.json"), "Storage marker filename for an existing node."),
    (re.compile(r"recordbench_principal_enabled"), "Applied SQLite function."),
    (re.compile(r"recordbench_case_lifecycle_writer"), "Applied SQLite function."),
    (re.compile(r"recordbench_case_content_cleanup"), "Applied SQLite function."),
    (re.compile(r"recordbench-nemotron-v1"), "Wire schema shared with the isolated Nemotron interpreter."),
    (re.compile(r"recordbench-(?!qwen35-|home\b)"), "Persisted format ids, schema names, and temp prefixes already stored or exchanged. The portable served-model id and container HOME are not in this exception."),
    (re.compile(r"recordbench:"), "Browser storage keys already saved in sessions."),
    (re.compile(r"recordbenchSource"), "Browser history-state key already saved in sessions."),
    (re.compile(r"X-RecordBench-"), "Previous response and proxy headers stay so existing clients and header clearing keep working."),
    (re.compile(r"RECORDBENCH_"), "Previous environment prefix. Reads still accept it and warn when it is set."),
    (re.compile(r'\("recordbench", "exculpata"\)'), "CI and tests exercise both naming generations."),
    (re.compile(r"recordbench-backup-@NODE_ID@"), "Systemd instance name for an existing legacy timer."),
)


class Finding:
    def __init__(self, location: str, rule: str = "legacy-product-name") -> None:
        self.location = location
        self.rule = rule

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Finding) and (self.location, self.rule) == (other.location, other.rule)

    def __hash__(self) -> int:
        return hash((self.location, self.rule))

    def __repr__(self) -> str:
        return f"Finding({self.location!r}, {self.rule!r})"


def _allowed(relative: str) -> bool:
    if relative in ALLOWLIST:
        return True
    return any(relative.startswith(prefix) for prefix in ALLOW_PREFIXES)


def _strip(text: str) -> str:
    for pattern, _reason in KEPT_PATTERNS:
        text = pattern.sub("", text)
    return text


def scan_text(location: str, text: str) -> list[Finding]:
    if TOKEN.search(_strip(text)) is None:
        return []
    return [Finding(location)]


def findings(root: Path | None = None) -> list[Finding]:
    root = (root or ROOT).resolve()
    found: list[Finding] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in path.relative_to(root).parts):
            continue
        relative = path.relative_to(root).as_posix()
        if _allowed(relative):
            continue
        if TOKEN.search(path.name):
            found.append(Finding(relative))
            continue
        data = path.read_bytes()
        if b"\0" in data[:1024]:
            text = data.decode("latin-1", errors="ignore")
        else:
            text = data.decode("utf-8", errors="ignore")
        found.extend(scan_text(relative, text))
    return found


def main() -> int:
    found = findings()
    if found:
        print("LEGACY NAME GATE: BLOCKED", file=sys.stderr)
        for item in found:
            print(f"  {item.rule}: {item.location}", file=sys.stderr)
        return 1
    print("LEGACY NAME GATE: CLEAN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
