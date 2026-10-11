"""Keep source modules from regrowing while the workbench and store are split.

Counts include blank lines, comments, and a final line without a newline.
Modules at or below 1,500 lines need no baseline. Oversized modules may shrink
without a baseline edit, but must not exceed their recorded count. Lowering a
baseline after shrinking is optional; never raise one. Split modules that cross
the ceiling, and remove entries for deleted modules.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).parent / "snapshots" / "source_line_counts.json"
NEW_FILE_LIMIT = 1_500


def _source_size_failures(current: dict[str, int], baseline: dict[str, int]) -> list[str]:
    failures = []
    for path, lines in current.items():
        if lines <= NEW_FILE_LIMIT:
            continue
        if baseline.get(path, 0) <= NEW_FILE_LIMIT:
            failures.append(
                f"{path}: has {lines:,} lines and crossed the "
                f"{NEW_FILE_LIMIT:,}-line limit. Split it into smaller modules."
            )
        elif lines > baseline[path]:
            failures.append(
                f"{path}: grew from {baseline[path]:,} to {lines:,} lines. "
                "Extract or reduce code instead of raising its baseline."
            )

    for path in sorted(baseline.keys() - current.keys()):
        failures.append(
            f"{path}: file was removed. Remove its entry from "
            "tests/snapshots/source_line_counts.json."
        )

    return failures


def test_source_size_ratchet():
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    current = {}
    for path in sorted((ROOT / "src").rglob("*.py")):
        with path.open(encoding="utf-8") as source:
            current[path.relative_to(ROOT).as_posix()] = sum(1 for _ in source)

    failures = _source_size_failures(current, baseline)
    assert not failures, "Source size ratchet failed:\n" + "\n".join(failures)


@pytest.mark.parametrize("lines", (101, 1_499, 1_500))
@pytest.mark.parametrize("baseline", ({}, {"src/synthetic.py": 100}))
def test_small_file_can_grow_without_a_baseline_edit(lines, baseline):
    assert _source_size_failures({"src/synthetic.py": lines}, baseline) == []


def test_oversized_file_cannot_grow():
    failures = _source_size_failures(
        {"src/synthetic.py": 1_601}, {"src/synthetic.py": 1_600}
    )
    assert len(failures) == 1
    assert "grew from 1,600 to 1,601" in failures[0]
    assert "instead of raising its baseline" in failures[0]


@pytest.mark.parametrize("lines", (1_599, 1_500, 100))
def test_oversized_file_can_shrink_without_a_baseline_edit(lines):
    assert _source_size_failures(
        {"src/synthetic.py": lines}, {"src/synthetic.py": 1_600}
    ) == []


def test_oversized_file_can_stay_at_its_baseline():
    assert _source_size_failures(
        {"src/synthetic.py": 1_600}, {"src/synthetic.py": 1_600}
    ) == []


@pytest.mark.parametrize("baseline", ({}, {"src/synthetic.py": 1_500}))
def test_file_crossing_ceiling_must_be_split(baseline):
    failures = _source_size_failures({"src/synthetic.py": 1_501}, baseline)
    assert len(failures) == 1
    assert "crossed the 1,500-line limit" in failures[0]
    assert "Split it into smaller modules" in failures[0]


def test_deleted_file_requires_baseline_cleanup():
    failures = _source_size_failures({}, {"src/synthetic.py": 1_600})
    assert len(failures) == 1
    assert "file was removed. Remove its entry" in failures[0]
