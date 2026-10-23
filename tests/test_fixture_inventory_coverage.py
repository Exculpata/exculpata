"""Tracked support roots participate without executing imported fixture code."""
from pathlib import Path
import subprocess

import pytest

from tests.fixture_inventory import check_repository_fixtures, inventory_paths


@pytest.mark.parametrize("filename", [
    "benchmarks/review_acceptance/integrity.py",
    "benchmarks/nested/synthetic_support.py",
    "services/transcription/compatibility/build_whisperx.py",
    "services/transcription/compatibility/nested/synthetic_support.py",
])
def test_imported_support_roots_are_statically_checked(tmp_path, filename):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    source = tmp_path / filename
    source.parent.mkdir(parents=True)
    source.write_text(
        "raise AssertionError('inventory must not execute imports')\n"
        "bench.workspace.queue_media_job('synthetic')\n"
    )
    subprocess.run(["git", "add", filename], cwd=tmp_path, check=True)
    assert inventory_paths(tmp_path) == [Path(filename)]
    violations = check_repository_fixtures(tmp_path)
    assert violations
    assert all(item.filename == filename and "fixture_write" in item.rule for item in violations)
    source.write_text(
        "from tests.fixture_write import fixture_write\n"
        "with fixture_write(bench.workspace, media=True):\n"
        "    bench.workspace.queue_media_job('synthetic')\n"
    )
    assert check_repository_fixtures(tmp_path) == []


def test_real_imported_support_modules_are_in_inventory():
    root = Path(__file__).resolve().parents[1]
    selected = set(inventory_paths(root))
    assert Path("benchmarks/review_acceptance/integrity.py") in selected
    assert Path("services/transcription/compatibility/build_whisperx.py") in selected
