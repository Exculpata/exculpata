"""Receipt and embedding-identity regressions for the review at a4f65ff."""
import ast
import hashlib
import json

import pytest

from case_intelligence import embedding_index as indexes
from case_intelligence.legacy_names import NAME_CONTRACTS
from tests.test_rename_contract_gate import ROOT, checker


REGEN_INSTALLATION_RECEIPT = (
    '.venv/bin/python scripts/rename_contract_check.py --regen-source-form '
    "'scripts/exculpata_install.py:_configure:unsupported legacy syntax "
    "(mapping unpacking); use immutable contract-based construction'"
)


def test_installation_source_form_receipt_is_current():
    location = "scripts/exculpata_install.py"
    lines = (ROOT / location).read_text().splitlines()
    receipt = next(item for item in checker.source_form_contracts()[location]["diagnostics"]
                   if item["function"] == "_configure" and "(mapping unpacking)" in item["identifier"])
    start = receipt["line"] - 1
    span = "\n".join(lines[start:start + receipt["source_line_count"]])
    assert hashlib.sha256(span.encode()).hexdigest() == receipt["source_line_sha256"], (
        "Installation source-form receipt drifted. Review the source, then run: "
        + REGEN_INSTALLATION_RECEIPT
    )


def test_regenerate_source_form_changes_only_target_anchor(tmp_path, monkeypatch):
    source = ('from case_intelligence.legacy_names import LEGACY_STEM\n'
              'def configure():\n    payload = {**{"names": LEGACY_STEM}}\n')
    receipt = {
        "line": 1, "identifier": "unsupported legacy syntax (mapping unpacking); use immutable contract-based construction",
        "function": "configure", "reason": "Synthetic reviewed mapping.", "validation": "synthetic suite",
        "source_line_sha256": "a" * 64,
    }
    original = {"version": 2, "files": {
        "synthetic.py": {"diagnostics": [receipt]},
        "untouched.py": {"diagnostics": [dict(receipt, function="untouched")]},
    }}
    manifest = tmp_path / "forms.json"
    # fixture-write: Synthetic source and receipt for targeted regeneration.
    (tmp_path / "synthetic.py").write_text(source)
    # fixture-write: Seed a stale synthetic receipt without real configuration.
    manifest.write_text(json.dumps(original, indent=2) + "\n")
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    monkeypatch.setattr(checker, "SOURCE_FORM_CONTRACT_PATH", manifest)
    selector = f'synthetic.py:configure:{receipt["identifier"]}'
    checker.regenerate_source_form(selector)
    actual = json.loads(manifest.read_text())
    expected = json.loads(json.dumps(original))
    expected_receipt = expected["files"]["synthetic.py"]["diagnostics"][0]
    expected_receipt["line"] = 3
    expected_receipt["source_line_sha256"] = hashlib.sha256(source.splitlines()[2].encode()).hexdigest()
    assert actual == expected
    once = manifest.read_bytes()
    checker.regenerate_source_form(selector)
    assert manifest.read_bytes() == once


@pytest.mark.parametrize("change", ["missing", "duplicate-record", "duplicate-finding", "wider-span"])
def test_regeneration_refuses_missing_or_ambiguous_targets(tmp_path, monkeypatch, change):
    identifier = "unsupported legacy syntax (mapping unpacking); use immutable contract-based construction"
    source = 'def configure():\n    payload = {**{"names": LEGACY_STEM}}\n'
    if change == "duplicate-finding":
        source = 'def configure():\n    first = {**{"names": LEGACY_STEM}}\n    second = {**{"names": LEGACY_STEM}}\n'
    if change == "wider-span":
        source = 'def configure():\n    payload = {\n        **{"names": LEGACY_STEM}\n    }\n'
    receipts = [{"line": 1, "identifier": identifier, "function": "configure",
                 "reason": "Synthetic reviewed mapping.", "validation": "synthetic suite",
                 "source_line_sha256": "a" * 64}]
    if change == "duplicate-record":
        receipts *= 2
    if change == "missing":
        source = 'def configure():\n    return {}\n'
    source = 'from case_intelligence.legacy_names import LEGACY_STEM\n' + source
    manifest = tmp_path / "forms.json"
    # fixture-write: Synthetic source exercises fail-closed receipt regeneration.
    (tmp_path / "synthetic.py").write_text(source)
    # fixture-write: Synthetic receipt must remain byte-identical on failure.
    manifest.write_text(json.dumps({"version": 2, "files": {"synthetic.py": {"diagnostics": receipts}}}))
    before = manifest.read_bytes()
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    monkeypatch.setattr(checker, "SOURCE_FORM_CONTRACT_PATH", manifest)
    expected_error = {
        "missing": "exactly one current diagnostic",
        "duplicate-record": "exactly one existing Python receipt",
        "duplicate-finding": "exactly one current diagnostic",
        "wider-span": "source span changed",
    }[change]
    with pytest.raises(ValueError, match=expected_error):
        checker.regenerate_source_form(f"synthetic.py:configure:{identifier}")
    assert manifest.read_bytes() == before


@pytest.mark.parametrize("original,replacement", [
    ('"auth": auth,', '"auth": "synthetic-changed-auth",'),
    ('"url": external.url,', '"url": "https://synthetic-generator.example",'),
])
def test_installation_dict_edits_invalidate_receipt(original, replacement):
    location = "scripts/exculpata_install.py"
    source = (ROOT / location).read_text()
    records = checker.source_form_contracts()
    receipt = next(item for item in records[location]["diagnostics"]
                   if item["function"] == "_configure" and "(mapping unpacking)" in item["identifier"])
    target = checker.Finding(f'{location}:{receipt["line"]}', receipt["identifier"])
    findings = list(dict.fromkeys(checker.scan_python(location, source, NAME_CONTRACTS)))
    assert target in findings
    assert checker.apply_source_forms(location, source, findings, records, require_current=True) == []

    assert source.count(original) == 1
    edited = source.replace(original, replacement)
    findings = list(dict.fromkeys(checker.scan_python(location, edited, NAME_CONTRACTS)))
    remaining = checker.apply_source_forms(location, edited, findings, records, require_current=True)
    assert target in remaining
    assert any("stale source-form contract" in item.identifier for item in remaining)


def test_installation_receipt_covers_complete_dict():
    location = "scripts/exculpata_install.py"
    source = (ROOT / location).read_text()
    receipt = next(item for item in checker.source_form_contracts()[location]["diagnostics"]
                   if item["function"] == "_configure" and "(mapping unpacking)" in item["identifier"])
    node = next(node for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Dict) and node.lineno == receipt["line"])
    assert receipt.get("source_line_count", 1) == node.end_lineno - node.lineno + 1
    span = "\n".join(source.splitlines()[node.lineno - 1:node.end_lineno])
    assert receipt["source_line_sha256"] == hashlib.sha256(span.encode()).hexdigest()


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_multiline_receipt_relocation_and_exact_consumption(newline):
    location = "synthetic.py"
    diagnostic = "unsupported legacy composition; use a contract key"
    source = "start\nprotected\nend"
    records = {location: {"diagnostics": [{
        "line": 1, "identifier": diagnostic, "source_line_count": 3,
        "source_line_sha256": hashlib.sha256(source.encode()).hexdigest(),
    }]}}
    relocated = newline * 2 + source.replace("\n", newline) + newline
    allowed = checker.Finding(location + ":3", diagnostic)
    literal = checker.Finding(location + ":3", "recordbench-unregistered-v99")
    assert checker.apply_source_forms(location, relocated, [allowed], records, require_current=True) == []
    assert checker.apply_source_forms(location, relocated, [allowed, allowed, literal], records) == [allowed, literal]
    for edited in (relocated.replace("protected", "changed"), relocated.replace("end", "")):
        remaining = checker.apply_source_forms(location, edited, [allowed], records, require_current=True)
        assert remaining[0] == allowed
        assert "stale source-form contract" in remaining[1].identifier


@pytest.mark.parametrize("count", [0, -1, True, 1.5, "2", None])
def test_source_receipt_rejects_invalid_line_counts(tmp_path, monkeypatch, count):
    manifest = tmp_path / "forms.json"
    # fixture-write: Synthetic malformed receipt exercises the real contract loader.
    manifest.write_text(json.dumps({"version": 2, "files": {"synthetic.py": {"diagnostics": [{
        "line": 1, "identifier": "unsupported legacy composition; use a contract key",
        "source_line_count": count, "source_line_sha256": "a" * 64,
        "reason": "synthetic", "validation": "synthetic",
    }]}}}))
    monkeypatch.setattr(checker, "SOURCE_FORM_CONTRACT_PATH", manifest)
    with pytest.raises(ValueError, match="source-form diagnostic contract"):
        checker.source_form_contracts()


@pytest.mark.parametrize("model,revision", [("", ""), ("", "r"), ("m", "")])
def test_empty_identity_is_not_promoted(tmp_path, model, revision):
    path = tmp_path / "embedding-index.json"
    original = json.dumps({"model": model, "revision": revision, "populated": False})
    # fixture-write: Seed an empty index record with synthetic missing identity fields.
    path.write_text(original, encoding="utf-8")
    warning = indexes.record_embedding_index(path, model=model, revision=revision, populated=True)
    assert warning == indexes.UNKNOWN_WARNING
    assert path.read_bytes() == original.encode("utf-8")
