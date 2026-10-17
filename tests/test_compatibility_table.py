"""Synthetic regressions for the public tester-program schema and link checks."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


table = load('check-compatibility-table')
links = load('check-tester-doc-links')


def document(**changes):
    values = ['Laptop, 16 GiB RAM, no GPU', 'Synthetic OS', 'CPU-only', 'None',
              'none', 'synthetic-revision', 'pass', 'pass', 'Not run',
              '[Report](https://github.com/Exculpata/exculpata/issues/274)', 'Synthetic trial']
    for key, value in changes.items():
        values[table.COLUMNS.index(key)] = value
    return '\n'.join('| ' + ' | '.join(row) + ' |' for row in (
        table.COLUMNS, ['---'] * len(table.COLUMNS), values))


def test_valid_table_and_repository_reference_rows():
    assert table.validate(document()) == []
    current = (ROOT / 'COMPATIBILITY.md').read_text()
    assert table.validate(current) == []
    assert 'd232e1ec66ddcfd38d38ff4a553bb80d3f639d02' in current
    assert 'not-recorded' in current and 'exact trial commit not recorded' in current
    assert 'GPU reference:' in current and 'docs/COMPATIBILITY.md' in current
    assert links.check() == []


@pytest.mark.parametrize('change', [
    lambda s: s.replace(' | CPU-only |', ' | CPU-only | extra |'),
    lambda s: s.replace('Hardware (generic)', 'Hardware'),
    lambda s: s.replace('---', '--', 1),
    lambda s: s.replace('| Laptop', 'Laptop'),
    lambda s: s.replace('Synthetic trial', ''),
    lambda s: s.splitlines()[0],
])
def test_malformed_table_fails(change):
    assert table.validate(change(document()))


@pytest.mark.parametrize('column', ['Install', 'doctor'])
@pytest.mark.parametrize('value', ['success', 'PASS', '', 'not-tested'])
def test_invalid_enums_fail(column, value):
    assert table.validate(document(**{column: value}))


@pytest.mark.parametrize('column', ['Hardware (generic)', 'Notes'])
@pytest.mark.parametrize('value', [
    'node.example.test', 'operator@', 'operator@example.test', '192.0.2.8',
    '2001:db8::1', '[::1]', r'node\.example\.test', r'operator\@', 'node%2eexample%2etest', 'operator&#64;example.test',
])
def test_identifiers_fail_without_echoing_value(column, value):
    errors = table.validate(document(**{column: value}))
    assert any('possible identifier' in e for e in errors)
    assert all(value not in e for e in errors)


@pytest.mark.parametrize('value', ['24.5 GiB RAM', 'PCIe 4.0; 313.26 s', r'CPU \| GPU'])
def test_generic_numbers_and_escaped_pipes_pass(value):
    assert table.validate(document(Notes=value)) == []


@pytest.mark.parametrize('value', [
    '#274', 'https://github.com/Exculpata/exculpata/issues/274',
    '[Report](https://github.com/Exculpata/exculpata/pull/274)',
    '[Report](https://github.com/Exculpata/exculpata/issues/0)',
    '[Report](https://example.test/issues/274)',
    '[Report](https://github.com/Exculpata/exculpata/issues/274) extra',
])
def test_issue_link_format(value):
    assert table.validate(document(Report=value))


def test_form_fields_map_one_to_one_to_table_columns():
    # Parse only the form's bounded id/label/options layout; no YAML dependency.
    form = (ROOT / '.github/ISSUE_TEMPLATE/hardware_report.yml').read_text()
    blocks = re.split(r'^  - type: ', form, flags=re.M)[1:]
    fields = {}
    for block in blocks:
        match = re.search(r'^    id: (.+)$', block, re.M)
        if match:
            field_id = match.group(1)
            assert field_id not in fields
            fields[field_id] = block
    assert set(fields) == {f for f in table.FORM_FIELDS if f} | {'privacy'}
    assert len(table.FORM_FIELDS) == len(table.COLUMNS)
    assert table.FORM_FIELDS[table.COLUMNS.index('Report')] is None
    for column, field_id in zip(table.COLUMNS, table.FORM_FIELDS, strict=True):
        if field_id:
            assert f'      label: {column}\n' in fields[field_id]
            assert '      required: true' in fields[field_id]
    for column, field_id in [('Install', 'install-result'), ('doctor', 'doctor-result')]:
        assert set(re.findall(r'^        - (.+)$', fields[field_id], re.M)) == table.ENUMS[column]
    assert 'labels: [hardware-report]' in form
    assert 'only synthetic demo data' in fields['privacy']
    assert 'required: true' in fields['privacy']
    assert 'hostnames, usernames, employer names' in fields['privacy']


def test_cli_failure_returns_nonzero(tmp_path):
    path = tmp_path / 'compatibility.md'
    path.write_text(document(Install='success'))
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/check-compatibility-table.py'), str(path)],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert 'invalid Install enum' in result.stdout


def test_offline_links_detect_missing_file_anchor_and_escape(tmp_path):
    (tmp_path / 'docs').mkdir()
    (tmp_path / 'COMPATIBILITY.md').write_text('# Table\n[guide](docs/TRY_IT_ON_YOUR_HARDWARE.md#guide)')
    guide = tmp_path / 'docs/TRY_IT_ON_YOUR_HARDWARE.md'
    guide.write_text('# Guide\n[table](../COMPATIBILITY.md#table)')
    assert links.check(tmp_path) == []
    for target in ('missing.md', '../COMPATIBILITY.md#absent', '../../outside.md'):
        guide.write_text(f'# Guide\n[bad]({target})')
        assert links.check(tmp_path)


def test_make_and_ci_run_both_checks_without_bypassing_gates():
    make = (ROOT / 'Makefile').read_text()
    workflow = (ROOT / '.github/workflows/quality-gates.yml').read_text()
    assert 'check-fast: compatibility-check compile deployment-check' in make
    for script in ('check-compatibility-table.py', 'check-tester-doc-links.py'):
        assert f'scripts/{script}' in make
        assert f'python scripts/{script}' in workflow


def test_even_backslashes_do_not_hide_extra_columns():
    assert table.validate(document(Notes=r"Synthetic \\| extra"))
    assert table.validate(document(Notes=r"Synthetic \\\| literal")) == []
