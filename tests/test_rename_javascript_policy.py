"""Construction policy keeps source review separate from runtime inference."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('checker', ROOT / 'scripts/rename_contract_check.py')
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)
import rename_javascript_policy as policy


@pytest.mark.parametrize('expression', ['a+b', 'a+=b', '`${a}${b}`', '[a,b].join("")', '[a,b]["join"]("")', 'a.concat(b)', 'a[method](b)', '(a.join)(b)', '((a.concat))(b)'])
def test_unmodeled_constructions_reject_without_type_or_sink_inference(expression):
    found = policy.constructions(expression)
    assert any('unsupported JavaScript construction' in diagnostic for _, diagnostic in found)


@pytest.mark.parametrize('source', ['const value="99+";', 'const pattern=/a+b/;', '// a+b\nconst value="synthetic";', 'arbitraryRuntime(value);'])
def test_noncomposition_syntax_and_documented_runtime_exclusion(source):
    assert policy.constructions(source) == []


def test_unknown_syntax_nodes_fail_closed_even_without_construction():
    class FutureNode:
        type = 'future_syntax'
        start_point = (0, 0)
        is_error = False
        is_missing = False
        named_children = []
    assert policy.inspect_tree(FutureNode(), b'') == [(1, 'unsupported JavaScript syntax node (future_syntax); use the rename-compat contract helper')]


def test_parse_size_and_node_budget_failures_are_blocking(monkeypatch):
    assert policy.constructions('const = ;') == [(1, 'unsupported ambiguous JavaScript syntax')]
    monkeypatch.setattr(policy, 'MAX_SOURCE_BYTES', 4)
    assert policy.constructions('"éé"') == [(1, 'unsupported JavaScript source size')]
    monkeypatch.setattr(policy, 'MAX_SOURCE_BYTES', 1000)
    monkeypatch.setattr(policy, 'MAX_NODES', 1)
    assert any('budget exhausted' in finding for _, finding in policy.constructions('const value="synthetic";'))


def test_each_construction_has_distinct_exact_source_identity():
    source = 'const a=x+y; const b=x+y;'
    found = policy.constructions(source)
    assert len(found) == 2 and found[0] != found[1]
    assert all(hashlib.sha256(b'x+y').hexdigest() in diagnostic for _, diagnostic in found)


def test_one_exact_receipt_cannot_hide_other_sites_literals_or_parse_failures():
    source = 'const a=x+y; const b=x+y;'
    location = 'synthetic.js'
    findings = checker.scan_javascript(location, source, checker.NAME_CONTRACTS)
    first = next(item for item in findings if 'unsupported JavaScript construction' in item.identifier)
    record = {location: {'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
                        'diagnostics': [{'line': 1, 'identifier': first.identifier}]}}
    literal = checker.Finding(location + ':1', checker.LEGACY_STEM + '-unregistered-v99')
    ambiguous = checker.Finding(location + ':1', 'unsupported ambiguous JavaScript syntax')
    remaining = checker.apply_source_forms(location, source, [*findings, literal, ambiguous], record)
    assert first not in remaining
    assert len([item for item in remaining if 'unsupported JavaScript construction' in item.identifier]) == 1
    assert literal in remaining and ambiguous in remaining
    assert checker.apply_source_forms(location, source + ' ', findings, record) == findings


@pytest.mark.parametrize('diagnostic', ['unsupported JavaScript syntax node (future_syntax)', 'unsupported ambiguous JavaScript syntax', 'unsupported JavaScript analysis budget exhausted', 'unsupported JavaScript parse timeout', 'unsupported JavaScript parser unavailable'])
def test_source_receipts_cannot_waive_parser_or_resource_failures(tmp_path, monkeypatch, diagnostic):
    path = tmp_path / 'forms.json'
    path.write_text(json.dumps({'version': 1, 'files': {'synthetic.js': {'source_sha256': 'a' * 64,
        'diagnostics': [{'line': 1, 'identifier': diagnostic, 'reason': 'synthetic', 'validation': 'synthetic'}]}}}))
    monkeypatch.setattr(checker, 'SOURCE_FORM_CONTRACT_PATH', path)
    with pytest.raises(ValueError, match='diagnostic contract'):
        checker.source_form_contracts()
