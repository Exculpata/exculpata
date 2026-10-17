"""The previous product name is rejected outside the explicit allowlist."""
from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "legacy_name_check", ROOT / "scripts" / "legacy_name_check.py"
)
assert SPEC is not None and SPEC.loader is not None
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def test_unlisted_name_is_reported(tmp_path):
    sample = tmp_path / "note.txt"
    sample.write_text("The previous spelling RecordBench must not land here.\n", encoding="utf-8")
    found = checker.scan_text(str(sample), sample.read_text(encoding="utf-8"))
    assert found


def test_kept_container_path_is_not_a_new_use():
    text = "target: /var/lib/recordbench/runtime\n"
    assert checker.scan_text("fixture", text) == []


def test_repository_tree_has_no_unlisted_legacy_names():
    assert checker.findings(ROOT) == []
