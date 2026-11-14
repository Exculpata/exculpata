"""Exercise the unchanged layer checker and inspect the new package boundary."""
import ast
import json

import pytest

from tests.test_import_boundaries import (
    LAYER_SNAPSHOT, PACKAGE, _check, _resolve_imports, _synthetic_baseline, _write_module,
)


def test_package_contract_and_isolation():
    layers = json.loads(LAYER_SNAPSHOT.read_text())
    assert layers["case_intelligence.modules"] == "core.contracts"
    assert layers["case_intelligence.modules.registry"] == "app_shell"
    paths = list((PACKAGE / "modules" / "discovery_review").glob("*.py"))
    assert len(paths) >= 3
    contract = PACKAGE / "modules" / "__init__.py"
    assert not _resolve_imports("case_intelligence.modules", contract, set(layers))
    for path in paths:
        name = "case_intelligence.modules.discovery_review"
        if path.stem != "__init__":
            name += "." + path.stem
        assert layers[name] == "modules.discovery_review"
        imports = _resolve_imports(name, path, set(layers))
        assert not imports & {"case_intelligence.workbench", "case_intelligence.workspace_store"}
        tree = ast.parse(path.read_text())
        assert not [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr.startswith("_")]
        constants = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id.isupper()]
        assert not [name for name in constants if "FIELD" in name or "COLUMN" in name]


@pytest.mark.parametrize("target, layer", [
    ("registry", "app_shell"), ("workbench", "app_shell"), ("other_feature", "modules.answers"),
])
def test_feature_import_rejected_by_unchanged_checker(tmp_path, target, layer):
    package = tmp_path / "synthetic"
    _write_module(package, "discovery_review")
    _write_module(package, target)
    layers = {"synthetic.discovery_review": "modules.discovery_review", f"synthetic.{target}": layer}
    baseline = _synthetic_baseline(package, layers)
    _write_module(package, "discovery_review", f"from synthetic import {target}\n")
    with pytest.raises(AssertionError, match="New forbidden import edges"):
        _check(package, layers, baseline)


def test_recursive_package_contract_and_isolation():
    layers = json.loads(LAYER_SNAPSHOT.read_text())
    assert layers["case_intelligence.modules"] == "core.contracts"
    assert layers["case_intelligence.modules.registry"] == "app_shell"
    paths = list((PACKAGE / "modules" / "discovery_review").rglob("*.py"))
    assert len(paths) >= 3
    contract = PACKAGE / "modules" / "__init__.py"
    assert not _resolve_imports("case_intelligence.modules", contract, set(layers))
    for path in paths:
        parts = list(path.relative_to(PACKAGE.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        name = ".".join(parts)
        assert layers[name] == "modules.discovery_review"
        imports = _resolve_imports(name, path, set(layers))
        assert not imports & {"case_intelligence.workbench", "case_intelligence.workspace_store"}
        tree = ast.parse(path.read_text())
        assert not [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr.startswith("_")]
        constants = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id.isupper()]
        assert not [name for name in constants if "FIELD" in name or "COLUMN" in name]


def nested_contract(tmp_path, monkeypatch, body):
    import tests.test_discovery_review_boundaries as contract
    package = tmp_path / "case_intelligence"
    names = ("modules.__init__", "modules.discovery_review.__init__", "modules.discovery_review.first",
             "modules.discovery_review.second", "modules.discovery_review.nested.child")
    for name in names:
        _write_module(package, name)
    _write_module(package, names[-1], body)
    layers = {"case_intelligence.modules": "core.contracts",
              "case_intelligence.modules.registry": "app_shell"}
    layers.update({"case_intelligence." + name.removesuffix(".__init__"): "modules.discovery_review"
                   for name in names[1:]})
    snapshot = tmp_path / "layers.json"
    snapshot.write_text(json.dumps(layers))  # fixture-write: synthetic module boundary inventory
    monkeypatch.setattr(contract, "PACKAGE", package)
    monkeypatch.setattr(contract, "LAYER_SNAPSHOT", snapshot)
    contract.test_recursive_package_contract_and_isolation()


def test_nested_public_module_passes(tmp_path, monkeypatch):
    nested_contract(tmp_path, monkeypatch, "value = source.document_count()\n")


def test_nested_private_attribute_is_rejected(tmp_path, monkeypatch):
    with pytest.raises(AssertionError):
        nested_contract(tmp_path, monkeypatch, "value = source._registry\n")


_EXECUTION_AST_FIELDS = {
    ast.Module: ('body', 'type_ignores'),
    ast.FunctionDef: ('name', 'args', 'body', 'decorator_list', 'returns', 'type_comment', 'type_params'),
    ast.ClassDef: ('name', 'bases', 'keywords', 'body', 'decorator_list', 'type_params'),
    ast.Return: ('value',),
    ast.Delete: ('targets',),
    ast.Assign: ('targets', 'value', 'type_comment'),
    ast.AnnAssign: ('target', 'annotation', 'value', 'simple'),
    ast.AugAssign: ('target', 'op', 'value'),
    ast.For: ('target', 'iter', 'body', 'orelse', 'type_comment'),
    ast.While: ('test', 'body', 'orelse'),
    ast.If: ('test', 'body', 'orelse'),
    ast.With: ('items', 'body', 'type_comment'),
    ast.Raise: ('exc', 'cause'),
    ast.Try: ('body', 'handlers', 'orelse', 'finalbody'),
    ast.Assert: ('test', 'msg'),
    ast.Import: ('names',),
    ast.ImportFrom: ('module', 'names', 'level'),
    ast.Global: ('names',),
    ast.Nonlocal: ('names',),
    ast.Expr: ('value',),
    ast.Pass: (),
    ast.Break: (),
    ast.Continue: (),
    ast.BoolOp: ('op', 'values'),
    ast.NamedExpr: ('target', 'value'),
    ast.BinOp: ('left', 'op', 'right'),
    ast.UnaryOp: ('op', 'operand'),
    ast.Lambda: ('args', 'body'),
    ast.IfExp: ('test', 'body', 'orelse'),
    ast.Dict: ('keys', 'values'),
    ast.Set: ('elts',),
    ast.ListComp: ('elt', 'generators'),
    ast.SetComp: ('elt', 'generators'),
    ast.DictComp: ('key', 'value', 'generators'),
    ast.GeneratorExp: ('elt', 'generators'),
    ast.Yield: ('value',),
    ast.YieldFrom: ('value',),
    ast.Compare: ('left', 'ops', 'comparators'),
    ast.Call: ('func', 'args', 'keywords'),
    ast.FormattedValue: ('value', 'conversion', 'format_spec'),
    ast.JoinedStr: ('values',),
    ast.Constant: ('value', 'kind'),
    ast.Attribute: ('value', 'attr', 'ctx'),
    ast.Subscript: ('value', 'slice', 'ctx'),
    ast.Starred: ('value', 'ctx'),
    ast.Name: ('id', 'ctx'),
    ast.List: ('elts', 'ctx'),
    ast.Tuple: ('elts', 'ctx'),
    ast.Slice: ('lower', 'upper', 'step'),
    ast.Load: (),
    ast.Store: (),
    ast.Del: (),
    ast.And: (),
    ast.Or: (),
    ast.Add: (),
    ast.Sub: (),
    ast.Mult: (),
    ast.MatMult: (),
    ast.Div: (),
    ast.Mod: (),
    ast.Pow: (),
    ast.LShift: (),
    ast.RShift: (),
    ast.BitOr: (),
    ast.BitXor: (),
    ast.BitAnd: (),
    ast.FloorDiv: (),
    ast.Invert: (),
    ast.Not: (),
    ast.UAdd: (),
    ast.USub: (),
    ast.Eq: (),
    ast.NotEq: (),
    ast.Lt: (),
    ast.LtE: (),
    ast.Gt: (),
    ast.GtE: (),
    ast.Is: (),
    ast.IsNot: (),
    ast.In: (),
    ast.NotIn: (),
    ast.comprehension: ('target', 'iter', 'ifs', 'is_async'),
    ast.ExceptHandler: ('type', 'name', 'body'),
    ast.arguments: ('posonlyargs', 'args', 'vararg', 'kwonlyargs', 'kw_defaults', 'kwarg', 'defaults'),
    ast.arg: ('arg', 'annotation', 'type_comment'),
    ast.keyword: ('arg', 'value'),
    ast.alias: ('name', 'asname'),
    ast.withitem: ('context_expr', 'optional_vars'),
    ast.TypeIgnore: ('lineno', 'tag'),
}

_EXECUTION_GUIDANCE = 'stored_index.rebuild.Service.advance'
_EXECUTION_NAMESPACES = {'threading', '_thread', 'concurrent', 'multiprocessing', 'subprocess', 'asyncio', 'anyio', 'os', 'importlib'}
_EXECUTION_MEMBERS = {'start_soon', 'start_task', 'create_task', 'ensure_future', 'run_in_executor', 'run_sync',
                      'call_soon', 'call_later', 'call_at', 'add_done_callback', 'add_task', 'submit', 'fork',
                      'spawn', 'spawnl', 'spawnv', 'system', 'popen', 'Thread', 'Timer', 'Process', 'Pool',
                      'ThreadPoolExecutor', 'ProcessPoolExecutor', 'BackgroundTask', 'BackgroundTasks',
                      'start_new_thread', 'start_new', 'create_task_group', 'run_until_complete', 'run_forever',
                      'run_sync_in_worker_thread', 'to_thread', 'start'}
_SYNC_CONSTRUCTORS = {'threading.Lock', 'threading.RLock', 'threading.Event'}
_SYNC_OPERATIONS = {'acquire', 'release', 'set', 'clear', 'is_set', 'wait'}


def check_no_background_execution(source, filename):
    """Bounded synchronous syntax policy; unsupported provenance fails closed."""
    def diagnostic(node, reason):
        return f'{filename}:{getattr(node, "lineno", 1)}: unsupported {reason}; use {_EXECUTION_GUIDANCE}'

    if len(source) > 300_000:
        return [diagnostic(None, 'source size')]
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return [diagnostic(None, 'source syntax')]
    pending, parents, nodes = [(tree, 0)], {}, []
    while pending:
        node, depth = pending.pop()
        if depth > 100 or len(nodes) > 30_000:
            return [diagnostic(node, 'analysis size or depth')]
        if type(node) not in _EXECUTION_AST_FIELDS or node._fields != _EXECUTION_AST_FIELDS[type(node)]:
            return [diagnostic(node, 'AST type or fields')]
        if isinstance(node, ast.comprehension) and node.is_async:
            return [diagnostic(node, 'asynchronous comprehension')]
        nodes.append(node)
        for child in ast.iter_child_nodes(node):
            parents[child] = node
            pending.append((child, depth + 1))
    origins, bindings, findings, imported = {}, {}, [], set()

    def symbol(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = symbol(node.value)
            return base + '.' + node.attr if base else ''
        return ''

    def origin(node):
        name = symbol(node)
        if name in origins:
            return origins[name]
        if isinstance(node, ast.Attribute):
            base = origin(node.value)
            return base + '.' + node.attr if base else ''
        return ''

    def bind(name, value, node):
        if name in origins and origins[name] != value:
            findings.append(diagnostic(node, 'capability alias rebinding'))
        origins[name] = value

    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split('.')[0]
                imported.add(name)
                bind(name, alias.name if alias.asname else alias.name.split('.')[0], node)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == '*':
                    findings.append(diagnostic(node, 'wildcard import'))
                else:
                    imported.add(alias.asname or alias.name)
                    value = (node.module or '') + '.' + alias.name
                    bind(alias.asname or alias.name, value, node)
                    safe_import = value in _SYNC_CONSTRUCTORS or value in {
                        'os.open', 'os.read', 'os.close', 'os.O_RDONLY', 'os.O_NOFOLLOW', 'os.O_CLOEXEC', 'os.fstat',
                        'importlib.resources', 'importlib.resources.files', 'importlib.resources.as_file'}
                    sensitive_import = value.split('.')[0] in _EXECUTION_NAMESPACES
                    if alias.name in _EXECUTION_MEMBERS or (sensitive_import and not safe_import):
                        findings.append(diagnostic(node, 'execution or unknown imported capability'))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bindings.setdefault(node.id, []).append(node)
    # Resolve immutable direct aliases, bounded by the already bounded tree.
    for _ in range(8):
        changed = False
        for node in nodes:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = origin(node.value)
                if isinstance(node.value, ast.Call) and origin(node.value.func) in _SYNC_CONSTRUCTORS:
                    value = 'synchronization'
                if value and (value.split('.')[0] in _EXECUTION_NAMESPACES or value == 'synchronization'):
                    for target in targets:
                        name = symbol(target)
                        if not name:
                            findings.append(diagnostic(node, 'capability destructuring'))
                        elif origins.get(name) != value:
                            bind(name, value, node)
                            changed = True
        if not changed:
            break
    else:
        return [diagnostic(tree, 'alias depth')]
    for name, value in origins.items():
        if value.split('.')[0] in _EXECUTION_NAMESPACES and len(bindings.get(name, ())) > (0 if name in imported else 1):
            findings.append(diagnostic(bindings[name][-1], 'capability alias rebinding'))
    for node in nodes:
        if isinstance(node, ast.arg) and origins.get(node.arg, '').split('.')[0] in _EXECUTION_NAMESPACES:
            findings.append(diagnostic(node, 'ambiguous capability parameter'))
        if isinstance(node, ast.Name) and node.id in {'exec', 'eval', 'compile', '__import__', 'getattr', 'setattr', 'globals', 'locals', 'vars'}:
            findings.append(diagnostic(node, 'reflection or dynamic execution'))
        if isinstance(node, ast.Attribute) and (node.attr in _EXECUTION_MEMBERS or node.attr in {'__dict__', '__getattribute__'}):
            findings.append(diagnostic(node, 'execution or reflective member'))
        if not isinstance(node, (ast.Name, ast.Attribute)) or not isinstance(node.ctx, ast.Load):
            continue
        value = origin(node)
        parent = parents.get(node)
        if value.startswith('synchronization.') and value.removeprefix('synchronization.') not in _SYNC_OPERATIONS:
            findings.append(diagnostic(node, 'synchronization member'))
        if value.rsplit('.', 1)[-1] in _EXECUTION_MEMBERS:
            findings.append(diagnostic(node, 'execution capability reference'))
        root = value.split('.')[0]
        if root not in _EXECUTION_NAMESPACES:
            continue
        if isinstance(parent, ast.Attribute) and parent.value is node:
            continue
        permitted = (value in _SYNC_CONSTRUCTORS or value in {'os.open', 'os.read', 'os.close', 'os.O_RDONLY', 'os.O_NOFOLLOW',
                      'os.O_CLOEXEC', 'os.fstat', 'importlib.resources.files', 'importlib.resources.as_file'})
        alias = isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is node
        if alias:
            if not permitted and value not in _EXECUTION_NAMESPACES and value not in {'importlib.resources', 'concurrent.futures'}:
                findings.append(diagnostic(node, 'execution capability alias'))
            continue
        direct = isinstance(parent, ast.Call) and parent.func is node
        factory = (isinstance(parent, ast.keyword) and parent.arg == 'default_factory'
                   and isinstance(parents.get(parent), ast.Call)
                   and origin(parents[parent].func) == 'dataclasses.field')
        if not permitted or not (direct or factory or value.startswith('os.O_')):
            findings.append(diagnostic(node, 'capability reference or escape'))
    unique = []
    for finding in findings:
        if finding not in unique:
            unique.append(finding)
    return unique


def test_stored_index_module_creates_no_threads():
    for path in (PACKAGE / 'modules' / 'discovery_review').rglob('*.py'):
        assert check_no_background_execution(path.read_text(), str(path.relative_to(PACKAGE))) == []


@pytest.mark.parametrize('source', [
    'import threading as t\nl = t.Lock()\nwith l:\n    pass\n',
    'from threading import RLock as L\nA = L\nl = A()\nl.acquire(blocking=False)\nl.release()\n',
    'from threading import Event as E\ne = E()\ne.set()\ne.clear()\ne.is_set()\ne.wait()\n',
    'from dataclasses import dataclass, field\nfrom threading import Lock as L\n@dataclass\nclass State:\n    lock: object = field(default_factory=L)\n',
    'from threading import Lock as L\nl = L()\ntry:\n    with l:\n        pass\nexcept Exception:\n    pass\n',
    'import os\nfd = os.open("synthetic", os.O_RDONLY)\nos.read(fd, 1)\nos.close(fd)\n',
    'from importlib import resources\nvalue = resources.files("synthetic").joinpath("data").read_text()\n',
])
def test_execution_checker_accepts_synchronization_aliases(source):
    assert check_no_background_execution(source, 'synthetic.py') == []


@pytest.mark.parametrize('source', [
    'from threading import Thread as T\n',
    'from subprocess import run as R\n',
    'from asyncio import get_event_loop as L\n',
    'loop.run_until_complete(work)\n',
    'import threading as t\nt = other\nt.Lock()\n',
    'import threading as t\ndef f(t):\n    return t.Lock()\n',
    'import threading\nworker = threading.Thread\n',
    'import threading as t\nmake = t.Timer\n',
    'from concurrent.futures import ThreadPoolExecutor as E\nE()\n',
    'from multiprocessing import Process as P\nP()\n',
    'import anyio as a\na.create_task_group()\n',
    'group.start_soon(work)\n', 'loop.call_later(1, work)\n',
    'from starlette.background import BackgroundTask as B\nB(work)\n',
    'import threading as t\nbox = [t.Lock]\n',
    'import threading as t\ngetattr(t, "Thread")\n',
    'import threading as t\nt = other\nt.Thread()\n',
    'import threading as t\nfactory(t.Lock)\n',
    'import threading as t\ndef escaped():\n    return t.Lock\n',
    'import threading as t\nt.unknown()\n',
    'from threading import *\n', 'import importlib\nimportlib.import_module("threading")\n',
])
def test_execution_checker_rejects_execution_aliases(source):
    findings = check_no_background_execution(source, 'synthetic.py')
    assert findings and all(_EXECUTION_GUIDANCE in finding for finding in findings)


def test_execution_checker_rejects_unmodeled_syntax(monkeypatch):
    assert check_no_background_execution('try:\n    pass\nexcept* Exception:\n    pass\n', 'synthetic.py')
    assert check_no_background_execution('value = ' + '[' * 110 + '0' + ']' * 110, 'synthetic.py')
    assert check_no_background_execution('pass\n' * 100_000, 'synthetic.py')
    with monkeypatch.context() as patch:
        patch.setattr(ast.Pass, '_fields', ('new_field',))
        assert check_no_background_execution('pass\n', 'synthetic.py')
