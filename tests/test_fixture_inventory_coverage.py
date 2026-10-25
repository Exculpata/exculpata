"""Keep the static fixture inventory at main's tests-only boundary."""
from pathlib import Path
import subprocess

from tests.fixture_inventory import check_repository_fixtures, inventory_paths


def test_inventory_matches_main_test_directory_scope(tmp_path):
    filenames = [
        "tests/test_synthetic.py",
        "tests/nested/support.py",
        "tests/fixture_write.py",
        "scripts/rename_contract_check.py",
        "benchmarks/nested/support.py",
        "services/transcription/tests/test_synthetic.py",
        "services/transcription/compatibility/support.py",
    ]
    for filename in filenames:
        source = tmp_path / filename
        source.parent.mkdir(parents=True, exist_ok=True)
        # fixture-write: Synthetic source inventory; no imported code executes.
        source.write_text("bench.workspace.queue_media_job('synthetic')\n")

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=15)
    subprocess.run(["git", "add", "scripts", "benchmarks", "services"],
                   cwd=tmp_path, check=True, timeout=15)
    expected = {Path("tests/test_synthetic.py"), Path("tests/nested/support.py")}
    assert set(inventory_paths(tmp_path)) == expected
    violations = check_repository_fixtures(tmp_path)
    assert {Path(item.filename) for item in violations} == expected
    assert all("fixture_write" in item.rule for item in violations)


def test_rename_checker_is_outside_real_fixture_inventory():
    selected = inventory_paths(Path(__file__).resolve().parents[1])
    assert Path("scripts/rename_contract_check.py") not in selected
    assert all(path.parts[0] == "tests" for path in selected)
