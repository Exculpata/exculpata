"""Keep raw cursor reads from growing while callers move to repository/DB APIs.

Counts include only AST calls to attributes named fetchall. Existing counts may
shrink without a baseline edit, but must never exceed their recorded count.
Lowering a baseline after shrinking is optional; never raise one. Files without
a baseline are allowed zero calls. Remove entries for deleted modules.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).parent / "snapshots" / "db_fetchall_counts.json"


def _fetchall_count(source: str) -> int:
    return sum(
        1 for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "fetchall"
    )


def _db_access_failures(current: dict[str, int], baseline: dict[str, int]) -> list[str]:
    failures = []
    for path, count in sorted(current.items()):
        limit = baseline.get(path, 0)
        if count > limit:
            failures.append(
                f"{path}: has {count} raw .fetchall() calls; baseline allows {limit}. "
                "Use the repository/DB interface instead of raw cursor reads."
            )

    for path in sorted(baseline.keys() - current.keys()):
        failures.append(
            f"{path}: file was removed. Remove its entry from "
            "tests/snapshots/db_fetchall_counts.json."
        )

    return failures


def test_db_access_ratchet():
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    current = {}
    for source_root in (ROOT / "src", ROOT / "services/transcription/src"):
        for path in sorted(source_root.rglob("*.py")):
            current[path.relative_to(ROOT).as_posix()] = _fetchall_count(
                path.read_text(encoding="utf-8")
            )

    failures = _db_access_failures(current, baseline)
    assert not failures, "DB access ratchet failed:\n" + "\n".join(failures)


@pytest.mark.parametrize(
    ("path", "baseline", "source", "count"),
    (
        ("src/synthetic.py", {"src/synthetic.py": 1},
         "cursor.fetchall()\ncursor.fetchall()", 2),
        ("services/transcription/src/synthetic.py", {}, "cursor.fetchall()", 1),
    ),
)
def test_extra_fetchall_call_is_rejected(path, baseline, source, count):
    failures = _db_access_failures({path: _fetchall_count(source)}, baseline)
    assert len(failures) == 1
    assert path in failures[0]
    assert f"has {count} raw .fetchall() calls" in failures[0]
    assert "repository/DB interface" in failures[0]
    assert "raise" not in failures[0]


@pytest.mark.parametrize("count", (0, 1, 2))
def test_file_can_shrink_or_stay_at_baseline_without_a_baseline_edit(count):
    assert _db_access_failures(
        {"src/synthetic.py": count}, {"src/synthetic.py": 2}
    ) == []


def test_file_without_baseline_allows_zero_calls():
    assert _db_access_failures({"src/synthetic.py": 0}, {}) == []


def test_deleted_file_requires_baseline_cleanup():
    failures = _db_access_failures({}, {"src/synthetic.py": 2})
    assert len(failures) == 1
    assert "src/synthetic.py: file was removed. Remove its entry" in failures[0]
    assert "tests/snapshots/db_fetchall_counts.json" in failures[0]


@pytest.mark.parametrize(
    ("source", "count"),
    (
        ("# cursor.fetchall()\ntext = 'cursor.fetchall()'", 0),
        ("cursor.fetchall\nfetchall()\ncursor.fetchone()", 0),
        ("cursor.fetchall()", 1),
        ("connection.execute('SELECT 1').fetchall()", 1),
        ("async def read(cursor):\n    return await cursor.fetchall()", 1),
        ("cursor.fetchall(other.fetchall())", 2),
    ),
)
def test_counts_only_calls_to_fetchall_attributes(source, count):
    assert _fetchall_count(source) == count


def test_invalid_source_cannot_silently_pass():
    with pytest.raises(SyntaxError):
        _fetchall_count("cursor.fetchall(")
