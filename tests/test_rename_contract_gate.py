"""New legacy identifiers require an explicit compatibility contract."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from case_intelligence.legacy_names import NAME_CONTRACTS, NameContract

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "rename_contract_check", ROOT / "scripts/rename_contract_check.py"
)
assert SPEC is not None and SPEC.loader is not None
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def test_repository_operational_names_are_registered():
    assert checker.findings(ROOT) == []


@pytest.mark.parametrize("relative", ["src/synthetic.py", "docs/SYNTHETIC.md", "docs/BRANDING.md"])
def test_unknown_legacy_identifier_in_code_or_docs_fails(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text('schema = "recordbench-unregistered-format-v99"\n')
    found = checker.findings(tmp_path)
    assert found == [checker.Finding(relative + ":1", "recordbench-unregistered-format-v99")]


def test_unknown_legacy_filename_fails_even_with_empty_contents(tmp_path):
    path = tmp_path / "recordbench-unknown-tool.py"
    path.touch()
    assert checker.findings(tmp_path) == [checker.Finding(path.name + ":1", "recordbench-unknown-tool.py")]


@pytest.mark.parametrize("filename,data", [
    ("recordbench-unknown-tool", b""),
    ("recordbench-unknown-tool.bin", b"\0synthetic"),
    ("recordbench-unknown-tool.py", b"\0synthetic"),
    ("recordbench-unknown-tool.sh", b"\xffsynthetic"),
])
def test_legacy_filenames_are_checked_before_content_filters(tmp_path, filename, data):
    path = tmp_path / "scripts" / filename
    path.parent.mkdir()
    path.write_bytes(data)
    assert checker.findings(tmp_path) == [checker.Finding(f"scripts/{filename}:1", filename)]


@pytest.mark.parametrize("child", [None, "config.py", "config", "config.bin"])
def test_legacy_directory_names_are_checked_independently_of_children(tmp_path, child):
    directory = tmp_path / "services" / "recordbench-unknown-service"
    directory.mkdir(parents=True)
    if child is not None:
        (directory / child).touch()
    assert checker.findings(tmp_path) == [
        checker.Finding("services/recordbench-unknown-service:1", "recordbench-unknown-service")
    ]


def test_registered_parent_does_not_hide_undeclared_path_components(tmp_path):
    directory = tmp_path / "recordbench-node" / "recordbench-unknown-service"
    directory.mkdir(parents=True)
    (directory / "recordbench-unknown-tool").touch()
    assert checker.findings(tmp_path) == [
        checker.Finding("recordbench-node/recordbench-unknown-service:1", "recordbench-unknown-service"),
        checker.Finding("recordbench-node/recordbench-unknown-service/recordbench-unknown-tool:1", "recordbench-unknown-tool"),
    ]


@pytest.mark.parametrize("relative", [
    "config/recordbench.env",
    "recordbench-node/config.py",
    "docs/assets/recordbench-historical/image.bin",
    "tests/recordbench-historical/tool",
    ".venv/recordbench-dependency/tool",
    "node_modules/recordbench-dependency/tool",
])
def test_declared_and_excluded_legacy_paths_remain_allowed(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.touch()
    assert checker.findings(tmp_path) == []


@pytest.mark.parametrize("identifier", [
    "RECORDBENCH_SYNTHETIC_SETTING",
    "X-RecordBench-Synthetic-Header",
    "recordbench.syntheticGitSetting",
    "config/recordbench.env",
    "/srv/recordbench/config",
    "/var/lib/recordbench-accounts/local-accounts.json",
    "recordbench-backup-0123456789.service",
    "recordbench-backup-@NODE_ID@.service",
    ".recordbench-managed-storage.json",
    "recordbench-exact-v1",
    "3.8.6+recordbench.1",
    "recordbench:assistant:conversation:",
])
def test_declared_alias_generation_and_retained_names_are_recognized(identifier):
    assert checker.scan_text("synthetic", identifier) == []


@pytest.mark.parametrize("key", sorted(NAME_CONTRACTS))
def test_each_declared_template_expands_to_a_recognized_identifier(key):
    value = NAME_CONTRACTS[key].legacy.format(suffix="SYNTHETIC", node_id="0123456789", kind="service")
    assert checker.scan_text("synthetic", value) == []


@pytest.mark.parametrize("identifier", [
    "recordbench-unregistered-v1",
    "recordbench-qwen99-999b",
    "/var/lib/recordbench-unregistered/cache",
    "/srv/recordbench-unregistered/config",
    "recordbench-exact-v99",
])
def test_declared_roots_and_formats_do_not_allow_undeclared_siblings(identifier):
    assert checker.scan_text("synthetic", identifier)


def test_removing_registered_format_rejects_its_existing_use(tmp_path):
    (tmp_path / "reader.py").write_text('FORMAT = "recordbench-exact-v1"\n')
    assert checker.findings(tmp_path) == []
    contracts = dict(NAME_CONTRACTS)
    del contracts["exact_v1"]
    assert checker.findings(tmp_path, contracts) == [checker.Finding("reader.py:1", "recordbench-exact-v1")]


@pytest.mark.parametrize("invalid", [
    NameContract("", "exculpata-new-v1", "alias"),
    NameContract("recordbench-new-{suffix}", "exculpata-new", "alias"),
    NameContract("recordbench-new", "exculpata-new", "retained"),
])
def test_incomplete_or_inconsistent_fallback_table_is_rejected_before_scanning(tmp_path, invalid):
    contracts = dict(NAME_CONTRACTS, synthetic=invalid)
    found = checker.findings(tmp_path, contracts)
    assert found
    assert all(item.location == "src/case_intelligence/legacy_names.py" for item in found)
    assert all(item.identifier.startswith("synthetic:") for item in found)


@pytest.mark.parametrize("relative", [
    "tests/test_historical.py",
    "services/example/tests/test_historical.py",
    "docs/assets/historical.md",
    "CHANGELOG.md",
    ".venv/source.py",
    "__pycache__/cached.py",
    "node_modules/dependency.js",
])
def test_independent_historical_fixtures_and_caches_are_not_rewritten(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("recordbench-independent-historical-fixture\n")
    assert checker.findings(tmp_path) == []


def test_binary_assets_and_symlinks_are_not_followed(tmp_path):
    (tmp_path / "binary.py").write_bytes(b"\0recordbench-binary-fixture")
    (tmp_path / "image.png").write_bytes(b"recordbench-binary-fixture")
    hidden = tmp_path / "tests"
    hidden.mkdir()
    fixture = hidden / "historical.py"
    fixture.write_text("recordbench-independent-historical-fixture")
    (tmp_path / "linked.py").symlink_to(fixture)
    assert checker.findings(tmp_path) == []


def test_unknown_operational_json_identifier_fails(tmp_path):
    path = tmp_path / "config" / "models.json"
    path.parent.mkdir()
    path.write_text('{"format": "recordbench-unregistered-format-v99"}\n')
    assert checker.findings(tmp_path) == [
        checker.Finding("config/models.json:1", "recordbench-unregistered-format-v99")
    ]
