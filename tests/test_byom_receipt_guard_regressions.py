"""Receipt and embedding-identity regressions for the review at a4f65ff."""
import ast
import hashlib
import json

import pytest

from case_intelligence import embedding_index as indexes
from case_intelligence.legacy_names import NAME_CONTRACTS
from tests.test_rename_contract_gate import ROOT, checker


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
    findings = list({finding: None for finding in checker.scan_python(location, source, NAME_CONTRACTS)})
    assert target in findings
    assert checker.apply_source_forms(location, source, findings, records, require_current=True) == []

    assert source.count(original) == 1
    edited = source.replace(original, replacement)
    findings = list({finding: None for finding in checker.scan_python(location, edited, NAME_CONTRACTS)})
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
