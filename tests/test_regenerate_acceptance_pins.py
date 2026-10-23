"""Synthetic CLI coverage for acceptance pin regeneration."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks.review_acceptance.integrity import verify_pack_integrity


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/regenerate-acceptance-pins.py"


def test_regeneration_script_has_no_network_inventory(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "network_contract", SCRIPT.with_name("network-contract.py")
    )
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    # fixture-write: isolated copy of the local-only regeneration script for inventory.
    (scripts / SCRIPT.name).write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    assert checker.source_inventory(tmp_path) == []


def run_script(root, *arguments):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--pack", "pack.json", *arguments],
        capture_output=True, text=True, check=False, timeout=15,
    )


@pytest.fixture
def synthetic_pack(tmp_path):
    # fixture-write: isolated synthetic Python source, never executed.
    (tmp_path / "source.py").write_text("def test_example():\n    assert True\n", encoding="utf-8")
    # fixture-write: isolated synthetic scenario bytes.
    (tmp_path / "scenario.json").write_text('{"expected": "synthetic"}\n', encoding="utf-8")
    payload = {
        "schema_version": 2,
        "cases": [{"node_ids": ["source.py::test_example"],
                   "scenario_path": "scenario.json", "expected": "synthetic"}],
        "case_fingerprint": "0" * 64,
        "integrity": {"algorithm": "sha256"},
    }
    # fixture-write: temporary synthetic acceptance manifest with missing pins.
    (tmp_path / "pack.json").write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("pin", [
    "case_fingerprint", "content_fingerprint", "test_node_sha256",
    "test_file_sha256", "fixture_file_sha256",
])
def test_check_detects_stale_pin_and_write_repairs_it(synthetic_pack, pin):
    root = synthetic_pack
    manifest = root / "pack.json"
    cases = json.loads(manifest.read_text())["cases"]
    first = run_script(root)
    assert first.returncode == 0, first.stderr
    original = manifest.read_bytes()
    payload = json.loads(original)
    verify_pack_integrity(root, payload)
    target = payload if pin == "case_fingerprint" else payload["integrity"]
    if isinstance(target[pin], dict):
        target[pin][next(iter(target[pin]))] = "0" * 64
    else:
        target[pin] = "0" * 64
    # fixture-write: deliberately stale pin in an isolated synthetic manifest.
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    stale = manifest.read_bytes()
    checked = run_script(root, "--check")
    assert checked.returncode == 1, checked.stderr
    assert "stale" in checked.stdout.lower()
    assert manifest.read_bytes() == stale
    repaired = run_script(root)
    assert repaired.returncode == 0, repaired.stderr
    assert manifest.read_bytes() == original
    assert json.loads(original)["cases"] == cases
    assert run_script(root, "--check").returncode == 0
    assert run_script(root).returncode == 0
    assert manifest.read_bytes() == original


def test_invalid_input_does_not_rewrite_manifest(synthetic_pack):
    root = synthetic_pack
    manifest = root / "pack.json"
    original = manifest.read_bytes()
    (root / "source.py").unlink()
    for arguments in [(), ("--check",)]:
        result = run_script(root, *arguments)
        assert result.returncode != 0
        assert "missing" in result.stderr
        assert manifest.read_bytes() == original
