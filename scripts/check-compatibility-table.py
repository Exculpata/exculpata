#!/usr/bin/env python3
"""Validate the public compatibility table without network access."""
from __future__ import annotations

import argparse
import html
import ipaddress
from pathlib import Path
import re
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
COLUMNS = (
    "Hardware (generic)", "OS", "Backend", "Model + quantization", "Profile",
    "Exculpata version", "Install", "doctor", "Eval summary", "Report", "Notes",
)
# GitHub supplies Report on submission; it is not a user-input field.
FORM_FIELDS = (
    "hardware", "os", "backend", "models", "profile", "exculpata-version",
    "install-result", "doctor-result", "eval-summary", None, "notes",
)
ENUMS = {"Install": {"pass", "fail", "partial"},
         "doctor": {"pass", "fail", "partial", "not-run", "not-recorded"}}
ISSUE = re.compile(r"\[([^\[\]]+)\]\(https://github\.com/Exculpata/exculpata/issues/([1-9][0-9]*)\)")
FQDN = re.compile(r"(?<![\w-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]*(?![\w-])", re.I)
USER_AT = re.compile(r"[\w.+-]+\s*@", re.UNICODE)
IPV4 = re.compile(r"(?<![\w.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![\w.])")


def identifying_pattern(cell: str) -> bool:
    # Decode common disguises; never echo potentially identifying values in CI.
    cell = html.unescape(unquote(cell)).replace("`", "")
    cell = re.sub(r"\\([\\`*{}\[\]()#+\-.!_>|@])", r"\1", cell)
    if FQDN.search(cell) or USER_AT.search(cell) or IPV4.search(cell):
        return True
    for token in re.findall(r"[0-9a-fA-F:]+(?:%[\w.-]+)?", cell):
        if ":" not in token:
            continue
        try:
            ipaddress.IPv6Address(token)
        except ValueError:
            continue
        return True
    return False


def cells(line: str) -> list[str]:
    if not line.startswith("|") or not line.endswith("|"):
        raise ValueError("table rows must start and end with a pipe")
    parts, current = [], []
    backslashes = 0
    for char in line[1:-1]:
        if char == "|" and backslashes % 2 == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        backslashes = backslashes + 1 if char == "\\" else 0
    parts.append("".join(current).strip())
    return [part.replace(r"\|", "|") for part in parts]


def validate(text: str) -> list[str]:
    errors: list[str] = []
    # This document owns exactly one table, including any stray malformed rows.
    lines = [(n, line.strip()) for n, line in enumerate(text.splitlines(), 1) if "|" in line]
    if len(lines) < 3:
        return ["expected a header, separator and at least one reference row"]
    rows: list[tuple[int, list[str]]] = []
    for number, line in lines:
        try:
            row = cells(line)
        except ValueError as exc:
            errors.append(f"line {number}: {exc}")
            continue
        if len(row) != len(COLUMNS):
            errors.append(f"line {number}: expected {len(COLUMNS)} columns")
        rows.append((number, row))
    if len(rows) < 3:
        return errors + ["incomplete compatibility table"]
    if tuple(rows[0][1]) != COLUMNS:
        errors.append("table header does not match the compatibility schema")
    if len(rows[1][1]) != len(COLUMNS) or not all(re.fullmatch(r":?-{3,}:?", c) for c in rows[1][1]):
        errors.append("invalid table separator")
    for number, row in rows[2:]:
        if len(row) != len(COLUMNS):
            continue
        values = dict(zip(COLUMNS, row, strict=True))
        if not all(row):
            errors.append(f"line {number}: empty cells must explicitly say unknown or not recorded")
        for column, allowed in ENUMS.items():
            if values[column] not in allowed:
                errors.append(f"line {number}: invalid {column} enum")
        if not ISSUE.fullmatch(values["Report"]):
            errors.append(f"line {number}: Report must link to an issue in this repository")
        for column in ("Hardware (generic)", "Notes"):
            if identifying_pattern(values[column]):
                errors.append(f"line {number}: possible identifier in {column}; use generic wording")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=ROOT / "COMPATIBILITY.md")
    args = parser.parse_args()
    try:
        errors = validate(args.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        errors = ["could not read compatibility table as UTF-8"]
    for error in errors:
        print(error)
    if not errors:
        print("Compatibility table: valid")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
