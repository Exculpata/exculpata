#!/usr/bin/env python3
"""Reject operational legacy identifiers absent from the rename contract.

This additive working-tree check does not replace publication, secret, legacy
name, or documentation gates. Tests retain independent historical expectations.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re
from string import Formatter
import sys
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from case_intelligence.legacy_names import (  # noqa: E402
    LEGACY_STEM, NAME_CONTRACTS, NameContract, contract_errors,
)

SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", "artifacts", ".tmp", "tests",
}
SOURCE_SUFFIXES = {
    ".py", ".md", ".js", ".cjs", ".mjs", ".html", ".toml", ".yaml", ".yml", ".json",
    ".sh", ".sql", ".conf", ".template", ".example", ".in", ".service", ".timer",
}
# The existing independently reviewed guard contains literal matching rules,
# not operational names. Neither its implementation nor its policy is changed.
SKIP_FILES = {"CHANGELOG.md", "scripts/legacy_name_check.py"}
SOURCE_NAMES = {"Dockerfile", "Makefile", "install", ".gitignore", ".dockerignore"}
HISTORICAL_ASSET = f"{LEGACY_STEM}-banner.svg"

_STEM = re.escape(LEGACY_STEM)
_TOKENS = re.compile(
    rf"(?P<path>/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]*{_STEM}[A-Za-z0-9_.@/-]*)"
    rf"|(?P<version>\d[\w.+-]*{_STEM}[\w.+-]*)"
    rf"|(?P<browser>{_STEM}:(?:[A-Za-z][A-Za-z0-9_:-]*)?)"
    rf"|(?P<name>(?:X-|\.)?{_STEM}[A-Za-z0-9_.-]*)",
    re.IGNORECASE,
)


class Finding(NamedTuple):
    location: str
    identifier: str


def _patterns(contracts: Mapping[str, NameContract]) -> list[tuple[str, re.Pattern[str]]]:
    result = []
    for key, pair in contracts.items():
        parts = []
        for literal, field, _format, _conversion in Formatter().parse(pair.legacy):
            parts.append(re.escape(literal))
            if field is not None:
                if key == "environment":
                    parts.append(r"[A-Z0-9_]*")
                elif key == "header":
                    parts.append(r"[A-Za-z0-9-]*")
                elif key == "git_config":
                    parts.append(r"[A-Za-z0-9]*")
                else:
                    parts.append(r"[A-Za-z0-9_.:-]+")
        result.append((pair.legacy, re.compile("".join(parts))))
    return result


def _registered(token: str, patterns: list[tuple[str, re.Pattern[str]]]) -> bool:
    for template, pattern in patterns:
        if pattern.fullmatch(token):
            return True
        # A declared directory also owns its descendants, not sibling names.
        if template.startswith("/") and "{" not in template:
            if token.startswith(template.rstrip("/") + "/"):
                return True
        # Generated/template examples may show a known family's fixed prefix.
        if "{" in template and token == template.split("{", 1)[0]:
            return True
        # File references may be relative to a configuration/release directory;
        # the filename itself still has to be explicitly present in the table.
        if token.startswith("/") and not template.startswith("/"):
            leaf = token.rstrip("/").rsplit("/", 1)[-1].split("@", 1)[0]
            if ("." in template or "-" in template) and pattern.fullmatch(leaf):
                return True
    return False


def scan_text(location: str, text: str, contracts: Mapping[str, NameContract] = NAME_CONTRACTS) -> list[Finding]:
    patterns = _patterns(contracts)
    found = []
    for match in _TOKENS.finditer(text):
        token = match.group()
        if token.rsplit("/", 1)[-1] == HISTORICAL_ASSET:
            continue
        if not any(_registered(candidate, patterns) for candidate in (token, token.rstrip("."), token.lstrip("."))):
            line = text.count("\n", 0, match.start()) + 1
            finding = Finding(f"{location}:{line}", token)
            if finding not in found:
                found.append(finding)
    return found


def findings(root: Path = ROOT, contracts: Mapping[str, NameContract] = NAME_CONTRACTS) -> list[Finding]:
    errors = contract_errors(contracts)
    if errors:
        return [Finding("src/case_intelligence/legacy_names.py", error) for error in errors]
    found = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            continue
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in relative.parts):
            continue
        if relative.as_posix() in SKIP_FILES or relative.as_posix().startswith("docs/assets/"):
            continue
        if relative.as_posix() == "src/case_intelligence/legacy_names.py":
            continue  # The authority is validated above, rather than scanning itself.
        # rglob visits each directory and file, so every relative-path component
        # is checked once, independently of its descendants or content type.
        found.extend(scan_text(relative.as_posix(), path.name, contracts))
        if not path.is_file():
            continue
        if path.suffix not in SOURCE_SUFFIXES and path.name not in SOURCE_NAMES:
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        found.extend(scan_text(relative.as_posix(), text, contracts))
    return found


def main() -> int:
    found = findings()
    if found:
        print("RENAME CONTRACT GATE: BLOCKED", file=sys.stderr)
        for item in found:
            print(f"  {item.location}: {item.identifier}", file=sys.stderr)
        return 1
    print("RENAME CONTRACT GATE: CLEAN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
