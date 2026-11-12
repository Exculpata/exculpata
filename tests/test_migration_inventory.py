"""Disk catalog and bounded resource-declaration contracts for SQLite migrations."""

import ast
from collections import Counter
from pathlib import Path
import re
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
MIRRORS = ('migrations/sqlite', 'src/case_intelligence/migrations/sqlite')
OWNERS = ('sqlite_store.py', 'workspace_store.py', 'source_roles.py',
          'case_lifecycle_migration.py', 'appearance.py')
PREFIX = 'migrations/sqlite/'


def catalog_inputs():
    catalogs = {directory: {path.name: path.read_bytes()
                           for path in (ROOT / directory).iterdir()}
                for directory in MIRRORS}
    sources = {f'src/case_intelligence/{name}':
               (ROOT / 'src/case_intelligence' / name).read_text() for name in OWNERS}
    package_data = tomllib.loads((ROOT / 'pyproject.toml').read_text())['tool']['setuptools']['package-data']
    return catalogs, sources, package_data


NAME = re.compile(r"[0-9]{4}_[a-z0-9_]+\.sql")


def declaration_names(source, filename):
    """Credit literal resource reads; diagnose unsupported variable arguments."""
    findings, names = [], set()

    def unsupported(node):
        findings.append(f'{filename}:{node.lineno}: unsupported migration declaration; '
                        'use literal names/tuples with validate_inventory')

    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as error:
        findings.append(f'{filename}:{error.lineno}: unsupported declaration syntax; '
                        'use literal names/tuples with validate_inventory')
        return names, findings
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def literal(node):
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

    def call(node, attribute):
        return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attribute

    for node in ast.walk(tree):
        if not call(node, 'read_text'):
            continue
        join = node.func.value
        if not call(join, 'joinpath') or len(join.args) != 1 or join.keywords:
            unsupported(node)
            continue
        resource = join.func.value
        if (not call(resource, 'files') or not isinstance(resource.func.value, ast.Name)
                or resource.func.value.id != 'resources' or len(resource.args) != 1
                or literal(resource.args[0]) != 'case_intelligence' or resource.keywords):
            unsupported(node)
            continue
        expression = join.args[0]
        value = literal(expression)
        if value is not None:
            entries = [value]
            location = expression
        else:
            if (isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add)
                    and literal(expression.left) == PREFIX):
                expression = expression.right
            parent = parents.get(node)
            while parent is not None and not isinstance(parent, (ast.For, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                parent = parents.get(parent)
            if (not isinstance(expression, ast.Name) or not isinstance(parent, ast.For)
                    or not isinstance(parent.target, ast.Name) or parent.target.id != expression.id
                    or not isinstance(parent.iter, ast.Tuple)
                    or not parent.iter.elts or any(literal(item) is None for item in parent.iter.elts)):
                unsupported(node)
                continue
            if any((isinstance(item, ast.Name) and item.id == expression.id
                    and isinstance(item.ctx, (ast.Store, ast.Del)) and item is not parent.target)
                   or (isinstance(item, ast.arg) and item.arg == expression.id)
                   for item in ast.walk(parent)):
                unsupported(node)
                continue
            continue
        for entry in entries:
            if not entry.startswith(PREFIX) or not NAME.fullmatch(entry[len(PREFIX):]):
                unsupported(location)
            else:
                names.add(entry[len(PREFIX):])
    return names, findings


def validate_inventory(catalogs, sources, package_data):
    """Validate copied path/byte mappings without importing migration runners."""
    findings = []
    for directory in MIRRORS:
        catalog = catalogs.get(directory, {})
        if not catalog:
            findings.append(f'{directory}: empty catalog')
        numbers = []
        for name in catalog:
            if not NAME.fullmatch(name):
                findings.append(f'{directory}: invalid filename {name}')
            else:
                numbers.append(int(name[:4]))
        for number, count in Counter(numbers).items():
            if count > 1:
                findings.append(f'{directory}: duplicate number {number:04d}')
        for number in sorted(set(range(1, max(numbers, default=0) + 1)) - set(numbers)):
            findings.append(f'{directory}: missing number {number:04d}')
        if 0 in numbers:
            findings.append(f'{directory}: invalid number 0000')
    first, second = (catalogs.get(directory, {}) for directory in MIRRORS)
    if first.keys() != second.keys():
        findings.append(f'{MIRRORS}: mirror names differ: {sorted(first.keys() ^ second.keys())}')
    for name in sorted(first.keys() & second.keys()):
        if first[name] != second[name]:
            findings.append(f'{MIRRORS}: mirror bytes differ: {name}')
    declared = set()
    for filename, source in sources.items():
        names, errors = declaration_names(source, filename)
        declared.update(names)
        findings.extend(errors)
    if declared - set(first):
        findings.append(f'{MIRRORS[0]}: declaration mismatch: '
                        f'absent={sorted(declared - set(first))}')
    if 'migrations/sqlite/*.sql' not in package_data.get('case_intelligence', ()):
        findings.append('pyproject.toml: package-data must include migrations/sqlite/*.sql for case_intelligence')
    return findings


@pytest.mark.parametrize('declaration', [
    pytest.param(
        "for name in ('migrations/sqlite/0001_synthetic.sql',):\n"
        "    resources.files('case_intelligence').joinpath(name).read_text()",
        id='plain-tuple'),
    pytest.param(
        "for name in ('0001_synthetic.sql',):\n"
        "    resources.files('case_intelligence').joinpath('migrations/sqlite/' + name).read_text()",
        id='prefix-tuple'),
    pytest.param(
        "for name in ('migrations/sqlite/0001_synthetic.sql',):\n"
        "    pending = (resources.files('case_intelligence').joinpath(name).read_text() for unused in (0,))\n"
        "name = computed_name\nlist(pending)",
        id='generator-expression'),
    pytest.param(
        "for name in ('0001_synthetic.sql',):\n"
        "    pending = (resources.files('case_intelligence').joinpath('migrations/sqlite/' + name).read_text() for unused in (0,))\n"
        "name = computed_name\nlist(pending)",
        id='prefix-generator-expression'),
    pytest.param(
        "for name in ('migrations/sqlite/0001_synthetic.sql',):\n"
        "    match computed_name:\n        case name:\n            pass\n"
        "    resources.files('case_intelligence').joinpath(name).read_text()",
        id='match-case'),
    pytest.param(
        "for name in ('0001_synthetic.sql',):\n"
        "    match computed_name:\n        case name:\n            pass\n"
        "    resources.files('case_intelligence').joinpath('migrations/sqlite/' + name).read_text()",
        id='prefix-match-case'),
])
def test_loop_variable_reads_are_never_credited(declaration):
    names, findings = declaration_names(declaration, 'synthetic.py')
    assert names == set()
    assert findings == []


def test_pristine_disk_catalog():
    assert validate_inventory(*catalog_inputs()) == []


@pytest.mark.parametrize('mirrors', [(0,), (1,), (0, 1)])
def test_duplicate_number_in_either_or_both_mirrors(mirrors):
    catalogs, sources, package_data = catalog_inputs()
    for index in mirrors:
        catalogs[MIRRORS[index]]['0047_synthetic_duplicate.sql'] = b'SELECT 1;'
    findings = validate_inventory(catalogs, sources, package_data)
    for index in mirrors:
        assert any(MIRRORS[index] in item and 'duplicate number 0047' in item for item in findings), findings


@pytest.mark.parametrize('fault,expected', [
    ('gap', 'missing number 0020'),
    ('names', 'mirror names'),
    ('bytes', 'mirror bytes'),
    ('declared-absent', 'declaration mismatch'),
    ('computed', 'unsupported'),
    ('package', 'package-data'),
    ('empty', 'empty catalog'),
    ('filename', 'invalid filename'),
])
def test_catalog_faults_are_rejected(fault, expected):
    catalogs, sources, package_data = catalog_inputs()
    workspace = 'src/case_intelligence/workspace_store.py'
    first, second = catalogs.values()
    interior = next(name for name in first if name.startswith('0020_'))
    if fault == 'gap':
        del first[interior]
        del second[interior]
    elif fault == 'names':
        second['0020_synthetic_renamed.sql'] = second.pop(interior)
    elif fault == 'bytes':
        second[interior] += b'\n-- synthetic byte difference\n'
    elif fault == 'declared-absent':
        sources['synthetic.py'] = (
            "resources.files('case_intelligence').joinpath("
            "'migrations/sqlite/0099_synthetic.sql').read_text()")
    elif fault == 'computed':
        sources[workspace] = sources[workspace].replace('joinpath(name)', 'joinpath(str(name))')
    elif fault == 'package':
        package_data['case_intelligence'].remove('migrations/sqlite/*.sql')
    elif fault == 'empty':
        first.clear()
    elif fault == 'filename':
        first['synthetic.sql'] = b'SELECT 1;'
    findings = validate_inventory(catalogs, sources, package_data)
    assert any(expected in item for item in findings), findings
    if fault == 'declared-absent':
        assert any(expected in item and '0099_synthetic.sql' in item for item in findings), findings


@pytest.mark.parametrize('declaration', [
    "name = 'migrations/sqlite/0001_synthetic.sql'\nresources.files('case_intelligence').joinpath(name).read_text()",
    "resources.files('case_intelligence').joinpath(f'migrations/sqlite/{name}').read_text()",
    "for name in migration_names:\n    resources.files('case_intelligence').joinpath(name).read_text()",
    "for name in ('migrations/sqlite/0001_synthetic.sql',):\n    name = computed_name\n    resources.files('case_intelligence').joinpath(name).read_text()",
])
def test_unsupported_declarations_name_location_and_supported_form(declaration):
    catalogs = {directory: {'0001_synthetic.sql': b'SELECT 1;'} for directory in MIRRORS}
    findings = validate_inventory(catalogs, {'synthetic.py': declaration},
                                  {'case_intelligence': ['migrations/sqlite/*.sql']})
    assert any('synthetic.py:' in item and 'unsupported' in item
               and 'literal names/tuples' in item and 'validate_inventory' in item for item in findings)


@pytest.mark.parametrize('prefix', ['', PREFIX])
@pytest.mark.parametrize('body,line', [
    ("    def load(name):\n        return {read}\n    load(computed_name)", 3),
    ("    async def load(name):\n        return {read}\n    load(computed_name)", 3),
    ("    load = lambda name: {read}\n    load(computed_name)", 2),
    ("    def load(name):\n        name = computed_name\n        return {read}\n    load(computed_name)", 4),
    ("    def load():\n        return {read}", 3),
    ("    async def load():\n        return {read}", 3),
    ("    load = lambda: {read}", 2),
])
def test_function_scopes_cannot_supply_loop_declarations(prefix, body, line):
    expression = 'name' if prefix else repr(PREFIX) + ' + name'
    read = f"resources.files('case_intelligence').joinpath({expression}).read_text()"
    source = (f'for name in {(prefix + "0001_synthetic.sql",)!r}:\n'
              + body.format(read=read))
    names, findings = declaration_names(source, 'synthetic.py')
    assert findings == [f'synthetic.py:{line}: unsupported migration declaration; '
                        'use literal names/tuples with validate_inventory']
    assert names == set()


@pytest.mark.parametrize('parameters', ['name', 'name, /', '*, name', '*name', '**name'])
@pytest.mark.parametrize('definition', [
    '    def load({parameters}):\n        pass',
    '    async def load({parameters}):\n        pass',
    '    load = lambda {parameters}: None',
])
def test_function_arguments_count_as_loop_name_stores(parameters, definition):
    source = ("for name in ('migrations/sqlite/0001_synthetic.sql',):\n"
              + definition.format(parameters=parameters) + '\n'
              "    resources.files('case_intelligence').joinpath(name).read_text()")
    names, findings = declaration_names(source, 'synthetic.py')
    line = len(source.splitlines())
    assert findings == [f'synthetic.py:{line}: unsupported migration declaration; '
                        'use literal names/tuples with validate_inventory']
    assert names == set()


def test_exception_mentions_do_not_duplicate_actual_declarations():
    source = ("resources.files('case_intelligence').joinpath('migrations/sqlite/0001_synthetic.sql').read_text()\n"
              "if name == '0001_synthetic.sql':\n    raise RuntimeError('0001_synthetic.sql')\n")
    assert declaration_names(source, 'synthetic.py') == ({'0001_synthetic.sql'}, [])
