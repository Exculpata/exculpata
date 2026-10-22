"""Pytest process defaults for the application suite."""

from __future__ import annotations

import ast
from collections import deque
from functools import cache
import inspect
import operator
import os
from pathlib import Path
import sys
import sysconfig
import textwrap

import pytest

from tests.sandbox_probe import probe_testclient

# Reserve 0 is the suite's disk escape. A real node must set this switch itself.
os.environ.setdefault("CASE_INTELLIGENCE_TEST_MODE", "1")

pytest_plugins = ['tests.worker_diagnostics']


_probe_result = pytest.StashKey[str | None]()
_worker_failures = pytest.StashKey[set[str]]()
_repository_root = Path(__file__).resolve().parents[1]
_stdlib_roots = tuple(
    Path(sysconfig.get_path(name)).resolve() for name in ('stdlib', 'platstdlib')
)
_helper_depth_limit = 6
_unresolved = object()


def _namespace_uses_testclient(namespace):
    for value in namespace.values():
        if isinstance(value, type) and any(
            cls.__name__ == 'TestClient' and cls.__module__ == 'starlette.testclient'
            for cls in value.__mro__
        ):
            return True
    return False


def _uses_testclient(module):
    if _namespace_uses_testclient(vars(module)):
        return True
    path = getattr(module, '__file__', None)
    return _file_uses_testclient(path) if path is not None else False


@cache
def _file_uses_testclient(path):
    try:
        with open(path, encoding='utf-8') as source:
            tree = ast.parse(source.read())
    except (OSError, SyntaxError, UnicodeError):
        return True
    return _tree_uses_testclient(tree)


def _tree_uses_testclient(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.alias) and node.name == 'TestClient':
            return True
        if isinstance(node, ast.Name) and node.id == 'TestClient':
            return True
        if isinstance(node, ast.Attribute) and node.attr == 'TestClient':
            return True
        if isinstance(node, ast.ImportFrom) and node.module in {
            'fastapi.testclient', 'starlette.testclient'
        } and any(alias.name in {'TestClient', '*'} for alias in node.names):
            return True
        if isinstance(node, ast.Import) and any(
            alias.name in {'fastapi.testclient', 'starlette.testclient'}
            for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and node.module in {'fastapi', 'starlette'}:
            if any(alias.name in {'testclient', '*'} for alias in node.names):
                return True
    return False


@cache
def _import_paths(name, directory, rootdir):
    parts = name.split('.')
    roots = dict.fromkeys((directory, rootdir, _repository_root, *map(Path, sys.path)))
    for root in roots:
        base = root.joinpath(*parts)
        candidates = (base.with_suffix('.py'), base / '__init__.py')
        for path in candidates:
            if path.is_file() and _local_path(path, rootdir):
                parents = [root.joinpath(*parts[:index]) / '__init__.py'
                           for index in range(1, len(parts))]
                return tuple(p.resolve() for p in (*parents, path) if p.is_file())
    return ()


@cache
def _module_bound_names(path):
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except (OSError, SyntaxError, UnicodeError):
        return frozenset()
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(target.id for target in targets if isinstance(target, ast.Name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.split('.')[0] for alias in node.names)
    return frozenset(names)


@cache
def _module_imports(path, rootdir):
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except (OSError, SyntaxError, UnicodeError):
        return None
    if _tree_uses_testclient(tree):
        return None
    dependencies = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                node.func.id if isinstance(node.func, ast.Name) else None)
            if name in {'__import__', 'exec', 'eval', 'import_module', 'spec_from_file_location',
                        'module_from_spec', 'exec_module', 'run_module', 'run_path',
                        'getfixturevalue'}:
                return None
        if isinstance(node, ast.Import):
            if any(alias.name in {'importlib', 'importlib.util', 'runpy'}
                   for alias in node.names):
                return None
            names = [alias.name for alias in node.names]
            directory = path.parent
        elif isinstance(node, ast.ImportFrom):
            if node.module in {'importlib', 'importlib.util', 'runpy', 'builtins'} and any(
                alias.name in {'import_module', 'spec_from_file_location',
                               'module_from_spec', 'run_module', 'run_path', '__import__',
                               'exec', 'eval', '*'}
                for alias in node.names
            ):
                return None
            directory = path.parent
            if node.level:
                initializers = [directory / '__init__.py']
                for _ in range(node.level - 1):
                    if not (directory / '__init__.py').is_file():
                        return None
                    directory = directory.parent
                    initializers.append(directory / '__init__.py')
                dependencies.update(p.resolve() for p in initializers if p.is_file())
                base = directory.joinpath(*(node.module or '').split('.'))
                candidates = (base.with_suffix('.py'), base / '__init__.py')
                resolved = [p.resolve() for p in candidates if p.is_file()]
                if not resolved or not all(_local_path(p, rootdir) for p in resolved):
                    return None
                dependencies.update(resolved)
                for alias in node.names:
                    candidates = (base / f'{alias.name}.py', base / alias.name / '__init__.py')
                    imported = [p.resolve() for p in candidates if p.is_file()]
                    if (node.module is None and not imported
                            and alias.name not in _module_bound_names(base / '__init__.py')):
                        return None
                    if not all(_local_path(p, rootdir) for p in imported):
                        return None
                    dependencies.update(imported)
                continue
            names = [node.module] if node.module else []
            names += [f'{node.module}.{alias.name}' for alias in node.names
                      if alias.name != '*']
        else:
            continue
        for name in names:
            dependencies.update(_import_paths(name, directory, rootdir))
    return frozenset(dependencies)


@cache
def _module_is_testclient_free(path, rootdir):
    pending = [Path(path).resolve()]
    visited = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        dependencies = _module_imports(current, rootdir)
        if dependencies is None:
            return False
        pending.extend(dependencies - visited)
    return True


@cache
def _testclient_free_callable(function, rootdir):
    try:
        path = inspect.getsourcefile(inspect.unwrap(function))
    except (OSError, TypeError, ValueError):
        return False
    return (path is not None and _local_path(path, rootdir)
            and _module_is_testclient_free(Path(path).resolve(), rootdir))


def _parameter_nodes(tree, branches):
    pending = [tree]
    while pending:
        node = pending.pop()
        yield node
        if isinstance(node, (ast.If, ast.IfExp)):
            condition = branches.get(node, _unresolved)
            if condition is not _unresolved:
                selected = node.body if condition else node.orelse
                pending.extend(selected if isinstance(selected, list) else [selected])
                pending.append(node.test)
                continue
        pending.extend(ast.iter_child_nodes(node))


@cache
def _callable_source(function):
    try:
        function = inspect.unwrap(function)
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    except (OSError, TypeError, ValueError, SyntaxError, UnicodeError):
        return None
    if inspect.isclass(function):
        module = inspect.getmodule(function)
        namespace = dict(vars(module)) if module is not None else {}
        namespace.update(vars(function))
    else:
        namespace = dict(getattr(function, '__globals__', {}))
        if inspect.isfunction(function) or inspect.ismethod(function):
            namespace.update(inspect.getclosurevars(function).nonlocals)
            for name in function.__code__.co_varnames + function.__code__.co_cellvars:
                namespace.pop(name, None)
    branches = tuple(node for node in ast.walk(tree)
                     if isinstance(node, (ast.If, ast.IfExp)))
    conditions = [node.test for node in branches]
    conditions.extend(node.args[0] for node in ast.walk(tree)
                      if isinstance(node, ast.Call) and node.args
                      and isinstance(node.func, ast.Attribute)
                      and node.func.attr == 'getfixturevalue')
    parameters = frozenset(node.id for condition in conditions
                           for node in ast.walk(condition) if isinstance(node, ast.Name))
    return tree, namespace, parameters, branches


def _callable_testclient_references(function, parameters=()):
    source = _callable_source(function)
    if source is None:
        return None
    params = {name: value for name, _, value in parameters}
    branches = []
    for node in source[3]:
        value = _parameter_expression(node.test, params)
        branches.append((node, _unresolved if value is _unresolved else bool(value)))
    return _selected_references(function, tuple(branches))


@cache
def _selected_references(function, branches):
    tree, namespace, _, _ = _callable_source(function)
    nodes = tuple(_parameter_nodes(tree, dict(branches)))
    names = {node.id for node in nodes if isinstance(node, ast.Name)}
    if any(isinstance(node, ast.Attribute) and node.attr == 'TestClient' for node in nodes):
        return True, (), (), (), False, ()
    references = {name: namespace[name] for name in names if name in namespace}
    direct = 'TestClient' in names or _namespace_uses_testclient(references)
    callables = [value for value in references.values()
                 if inspect.isfunction(value) or inspect.isclass(value)]
    if inspect.isclass(function):
        for name, method in inspect.getmembers_static(function):
            if isinstance(method, (classmethod, staticmethod)):
                method = method.__func__
            if inspect.isfunction(method):
                callables.append(method)
    attributes = []
    fixture_calls = []
    dynamic = False
    strings = set()
    for node in nodes:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.add(node.value)
        if isinstance(node, ast.Call):
            callee = node.func
            name = callee.attr if isinstance(callee, ast.Attribute) else (
                callee.id if isinstance(callee, ast.Name) else None
            )
            if isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name):
                module = references.get(callee.value.id)
                if inspect.ismodule(module):
                    attributes.append((module, callee.attr))
                elif callee.value.id not in namespace and callee.attr != 'getfixturevalue':
                    attributes.append((callee.value.id, callee.attr))
            if isinstance(callee, ast.Attribute) and name == 'getfixturevalue':
                fixture_calls.append(node.args[0] if node.args else None)
            if name in {'spec_from_file_location', 'module_from_spec', 'exec_module',
                        'import_module', 'run_module', 'run_path'}:
                dynamic = True
    return direct, tuple(callables), tuple(attributes), tuple(fixture_calls), dynamic, frozenset(strings)


@cache
def _local_path(path, rootdir):
    path = Path(path).resolve()
    if {'site-packages', 'dist-packages'}.intersection(path.parts):
        return False
    if any(path.is_relative_to(root) for root in _stdlib_roots):
        return False
    return path.is_relative_to(_repository_root) or path.is_relative_to(rootdir)


@cache
def _local_callable(function, rootdir):
    if getattr(getattr(function, '__code__', None), 'co_name', None) == '__annotate__':
        return False
    if getattr(function, '__module__', None) in sys.builtin_module_names:
        return False
    try:
        path = inspect.getsourcefile(inspect.unwrap(function))
    except (OSError, TypeError, ValueError):
        path = None
    if path is not None and path.startswith('<'):
        return False
    if path is None:
        module = inspect.getmodule(function)
        path = getattr(module, '__file__', None)
    return (path is not None and _local_path(path, rootdir)
            and _callable_source(function) is not None)


_comparisons = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
    ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.Is: operator.is_, ast.IsNot: operator.is_not,
    ast.In: lambda left, right: left in right,
    ast.NotIn: lambda left, right: left not in right,
}
_unary_operations = {
    ast.Not: operator.not_, ast.UAdd: operator.pos,
    ast.USub: operator.neg, ast.Invert: operator.invert,
}


def _parameter_expression(node, params):
    def primitive(value):
        if type(value) not in (str, int, float, bool, bytes, type(None)):
            raise ValueError('Nonconstant parameter')
        return value

    def resolve(current):
        if isinstance(current, ast.Constant):
            return primitive(current.value)
        if isinstance(current, ast.Name) and current.id in params:
            return primitive(params[current.id])
        if isinstance(current, ast.IfExp):
            condition, yes, no = resolve(current.test), resolve(current.body), resolve(current.orelse)
            return yes if condition else no
        if isinstance(current, ast.Compare):
            values = [resolve(current.left), *(resolve(value) for value in current.comparators)]
            return all(_comparisons[type(op)](left, right)
                       for op, left, right in zip(current.ops, values, values[1:]))
        if isinstance(current, ast.BoolOp):
            values = [resolve(value) for value in current.values]
            result = values[0]
            for value in values[1:]:
                result = (result and value) if isinstance(current.op, ast.And) else (result or value)
            return result
        if isinstance(current, ast.UnaryOp):
            return _unary_operations[type(current.op)](resolve(current.operand))
        raise ValueError('Unresolved fixture expression')

    try:
        value = resolve(node)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return _unresolved
    return value


def _fixture_name_expression(node, params):
    value = _parameter_expression(node, params)
    return value if isinstance(value, str) else None


@cache
def _dynamic_files(strings, rootdir):
    paths = set()
    for value in strings:
        if '\n' in value or not value or '\x00' in value:
            continue
        candidates = [Path(value)]
        if all(part.isidentifier() for part in value.split('.')):
            module_path = value.replace('.', '/')
            candidates += [Path(module_path + '.py'), Path(module_path) / '__init__.py']
        for candidate in candidates:
            for root in (rootdir, _repository_root):
                for directory in (root, root / 'scripts', root / 'src'):
                    path = directory / candidate
                    try:
                        if path.is_file() and _local_path(path, rootdir):
                            paths.add(path.resolve())
                    except (OSError, ValueError):
                        continue
    return tuple(paths)


def _bind_loader_target(bindings, target, value):
    if isinstance(target, ast.Name):
        bindings.setdefault(target.id, []).append(value)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for index, element in enumerate(target.elts):
            _bind_loader_target(bindings, element, ast.Subscript(value, ast.Constant(index)))


@cache
def _loader_bindings(function):
    source = _callable_source(function)
    if source is None:
        return {}, (), {}
    tree, namespace, _, _ = source
    bindings = {}
    returned = []
    pending = list(getattr(tree.body[0], 'body', ()))
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                _bind_loader_target(bindings, target, node.value)
        if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
            for value in node.iter.elts:
                _bind_loader_target(bindings, node.target, value)
        if isinstance(node, (ast.Return, ast.Yield)) and node.value is not None:
            returned.append(node.value)
        pending.extend(ast.iter_child_nodes(node))
    return bindings, tuple(returned), namespace


def _loader_values(function, node, params, seen=frozenset()):
    if node in seen:
        return (_unresolved,)
    seen = seen | {node}
    bindings, _, namespace = _loader_bindings(function)

    def values(child):
        return _loader_values(function, child, params, seen)

    try:
        if isinstance(node, ast.Constant):
            return (node.value,) if type(node.value) in (str, int, bool, type(None)) else (_unresolved,)
        if isinstance(node, ast.Name):
            if node.id in params:
                return (params[node.id],)
            if node.id in bindings:
                return tuple(value for binding in bindings[node.id] for value in values(binding))
            value = namespace.get(node.id, _unresolved)
            return (value,) if type(value) in (str, int, bool) or isinstance(value, Path) else (_unresolved,)
        if isinstance(node, ast.IfExp):
            conditions = values(node.test)
            if len(conditions) == 1 and conditions[0] is not _unresolved:
                return values(node.body if conditions[0] else node.orelse)
            return (*values(node.body), *values(node.orelse))
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            return tuple(_comparisons[type(node.ops[0])](left, right)
                         if left is not _unresolved and right is not _unresolved else _unresolved
                         for left in values(node.left) for right in values(node.comparators[0]))
        if isinstance(node, (ast.Tuple, ast.List, ast.JoinedStr)):
            combinations = [()]
            for element in (node.values if isinstance(node, ast.JoinedStr) else node.elts):
                combinations = [(*parts, value) for parts in combinations for value in values(element)]
            if isinstance(node, ast.JoinedStr):
                return tuple(''.join(parts) if all(isinstance(p, str) for p in parts)
                             else _unresolved for parts in combinations)
            return tuple(combinations)
        if (isinstance(node, ast.FormattedValue) and node.format_spec is None
                and node.conversion in {-1, ord('s')}):
            return tuple(str(value) if type(value) in (str, int, bool) else _unresolved
                         for value in values(node.value))
        if isinstance(node, ast.BinOp):
            results = []
            for left in values(node.left):
                for right in values(node.right):
                    if isinstance(node.op, ast.Div) and isinstance(left, Path) and isinstance(right, (str, Path)):
                        results.append(left / right)
                    elif isinstance(node.op, ast.Add) and isinstance(left, str) and isinstance(right, str):
                        results.append(left + right)
                    else:
                        results.append(_unresolved)
            return tuple(results)
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == 'request' and node.attr == 'param':
                return (params.get('request.param', _unresolved),)
            if node.attr in {'parent', 'parents'}:
                return tuple((value.parent if node.attr == 'parent' else tuple(value.parents))
                             if isinstance(value, Path) else _unresolved for value in values(node.value))
        if isinstance(node, ast.Subscript):
            return tuple(value[index] if isinstance(value, tuple) and type(index) is int
                         else _unresolved for value in values(node.value) for index in values(node.slice))
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and namespace.get(node.func.id) is Path and len(node.args) == 1:
                return tuple(Path(value) if isinstance(value, (str, Path)) else _unresolved
                             for value in values(node.args[0]))
            if isinstance(node.func, ast.Attribute) and node.func.attr in {'resolve', 'absolute'} and not node.args:
                return tuple(value.resolve() if isinstance(value, Path) else _unresolved
                             for value in values(node.func.value))
    except (IndexError, KeyError, TypeError, ValueError, OSError):
        pass
    return (_unresolved,)


@cache
def _loader_info(function, rootdir, binding=None, parameters=(), attribute=None):
    bindings, returned, _ = _loader_bindings(function)
    params = {name: value for name, _, value in parameters}
    selected_attributes = (attribute,)
    source = _callable_source(function)
    if source is not None:
        for node in ast.walk(source[0]):
            if (isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Attribute) and target.attr == attribute
                    for target in node.targets)):
                value = node.value
                if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                        and value.func.id == 'getattr' and len(value.args) == 2):
                    selected_attributes = _loader_values(function, value.args[1], params)
                else:
                    selected_attributes = (_unresolved,)
    pending = [(function, node, parameters, 0) for node in
               (bindings.get(binding, ()) if binding is not None else returned)]
    visited = set()
    paths = set()
    dynamic = False
    unresolved = False
    while pending:
        current, node, arguments, depth = pending.pop()
        key = (current, node, arguments)
        if key in visited:
            continue
        if depth > _helper_depth_limit:
            unresolved = True
            continue
        visited.add(key)
        params = {name: value for name, _, value in arguments}
        bindings, _, namespace = _loader_bindings(current)
        if isinstance(node, ast.Name):
            pending.extend((current, value, arguments, depth) for value in bindings.get(node.id, ()))
        elif isinstance(node, ast.Call):
            callee = node.func
            name = callee.attr if isinstance(callee, ast.Attribute) else (
                callee.id if isinstance(callee, ast.Name) else None)
            if name == 'module_from_spec':
                dynamic = True
                pending.extend((current, value, arguments, depth) for value in node.args)
            elif name in {'spec_from_file_location', 'import_module', 'run_module', 'run_path'}:
                dynamic = True
                index = 1 if name == 'spec_from_file_location' else 0
                values = (_loader_values(current, node.args[index], params)
                          if len(node.args) > index else (_unresolved,))
                for value in values:
                    found = _dynamic_files(frozenset({str(value)}), rootdir) if isinstance(value, (str, Path)) else ()
                    if not found:
                        unresolved = True
                    paths.update(found)
            elif isinstance(callee, ast.Name):
                helper = namespace.get(callee.id)
                if (inspect.isfunction(helper) and not _testclient_free_callable(helper, rootdir)
                        and _local_callable(helper, rootdir)):
                    combinations = [dict(params)]
                    names = list(inspect.signature(helper).parameters)
                    inputs = list(zip(names, node.args)) + [
                        (kw.arg, kw.value) for kw in node.keywords if kw.arg is not None]
                    for name, expression in inputs:
                        combinations = [dict(args, **{name: value}) for args in combinations
                                        for value in _loader_values(current, expression, params)]
                    for args in combinations:
                        encoded = tuple((name, type(value), value) for name, value in args.items())
                        pending.extend((helper, value, encoded, depth + 1)
                                       for value in _loader_bindings(helper)[1])
        elif isinstance(node, ast.IfExp):
            pending.extend((current, value, arguments, depth) for value in (node.body, node.orelse))
    if unresolved or any(not isinstance(attr, str) for attr in selected_attributes):
        return dynamic, ()
    return dynamic, tuple((path, attr) for path in sorted(paths) for attr in selected_attributes)


@cache
def _loaded_module_source(path):
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except (OSError, SyntaxError, UnicodeError):
        return None
    definitions = {node.name: node for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    clients = {'TestClient'}
    imports = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module in sys.modules:
            namespace = vars(sys.modules[node.module])
            imports.update((alias.asname or alias.name, namespace.get(alias.name))
                           for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            paths = _import_paths(node.module, path.parent, path.parent)
            if paths:
                imports.update((alias.asname or alias.name, (paths[-1], alias.name))
                               for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module in {
            'starlette.testclient', 'fastapi.testclient'
        }:
            clients.update(alias.asname or alias.name for alias in node.names
                           if alias.name == 'TestClient')
    return definitions, clients, imports


@cache
def _loaded_attribute_uses_testclient(path, attribute):
    pending = deque([(path, attribute, 0)])
    visited = set()
    while pending:
        current, name, depth = pending.popleft()
        if (current, name) in visited:
            continue
        visited.add((current, name))
        source = _loaded_module_source(current)
        if source is None:
            return True
        definitions, clients, imports = source
        if name in definitions and _module_is_testclient_free(current, path.parent):
            continue
        if depth > _helper_depth_limit:
            return True
        if name not in definitions:
            target = imports.get(name)
            if isinstance(target, tuple):
                pending.append((*target, depth + 1))
                continue
            if inspect.isfunction(target) or inspect.isclass(target):
                if (_local_callable(target, path.parent)
                        and _function_uses_testclient(target, path.parent) is True):
                    return True
                continue
            return True
        nodes = tuple(ast.walk(definitions[name]))
        names = {node.id for node in nodes if isinstance(node, ast.Name)}
        if clients.intersection(names) or any(
            isinstance(node, ast.Attribute) and node.attr == 'TestClient' for node in nodes
        ):
            return True
        pending.extend((current, target, depth + 1) for target in names
                       if target in definitions or isinstance(imports.get(target), tuple))
    return False


@cache
def _scan_plan(function, rootdir, parameters):
    params = {name: value for name, _, value in parameters}
    pending = deque()
    visited = set()
    fixture_names = []
    fixture_attributes = []

    pending.append((function, _helper_depth_limit))
    while pending:
        current, depth = pending.popleft()
        if current in visited:
            continue
        if depth < 0:
            return True, (), ()
        visited.add(current)
        references = _callable_testclient_references(current, parameters)
        if references is None:
            if current is function:
                return None, (), ()
            continue
        direct, callables, attributes, fixture_calls, _, _ = references
        if direct:
            return True, (), ()
        for expression in fixture_calls:
            fixture_names.append(_fixture_name_expression(expression, params))
        targets = list(callables)
        for module, attribute in attributes:
            if isinstance(module, str):
                fixture_attributes.append((current, module, attribute))
                continue
            path = getattr(module, '__file__', None)
            if path is None or not _local_path(path, rootdir):
                continue
            target = vars(module).get(attribute)
            if inspect.isfunction(target) or inspect.isclass(target):
                targets.append(target)
        for target in targets:
            if (not _testclient_free_callable(target, rootdir)
                    and _local_callable(target, rootdir)):
                pending.append((target, depth - 1))
    return False, tuple(fixture_names), tuple(fixture_attributes)


def _fixture_definitions(item, name):
    manager = getattr(getattr(item, 'session', None), '_fixturemanager', None)
    if manager is not None:
        return manager.getfixturedefs(name, item)
    return getattr(getattr(item, '_fixtureinfo', None), 'name2fixturedefs', {}).get(name)


def _fixture_source_unavailable(function):
    module = inspect.getmodule(inspect.unwrap(function))
    return module is None or _uses_testclient(module)


def _scan_testclient(function, rootdir, item=None, *, fixture=False, visited=None):
    params = getattr(getattr(item, 'callspec', None), 'params', {})
    source = _callable_source(function)
    parameters = tuple((name, type(value), value) for name, value in params.items()
                       if source is not None and name in source[2]
                       and type(value) in (str, int, float, bool, bytes, type(None)))
    direct, fixture_names, attributes = _scan_plan(function, rootdir, parameters)
    if direct is not False:
        return _fixture_source_unavailable(function) if direct is None and fixture else direct
    for current, name, attribute in attributes:
        definitions = _fixture_definitions(item, name) if item is not None else None
        loader_params = tuple((key, type(value), value) for key, value in params.items()
                              if type(value) in (str, int, float, bool, bytes, type(None)))
        loaders = [_loader_info(
            definition.func, rootdir, parameters=loader_params + (
                (('request.param', type(params[name]), params[name]),)
                if any(key == name for key, _, _ in loader_params) else ()),
            attribute=attribute) for definition in definitions or ()]
        loaders.append(_loader_info(current, rootdir, name, loader_params, attribute))
        for dynamic, paths in loaders:
            if dynamic and (not paths or any(
                _loaded_attribute_uses_testclient(path, selected) for path, selected in paths
            )):
                return True
    visited = set() if visited is None else visited
    for name in fixture_names:
        if name is None or item is None:
            return True
        if name == 'request' or name in visited:
            continue
        visited.add(name)
        definitions = _fixture_definitions(item, name)
        if not definitions:
            return True
        pending = list(definitions)
        while pending:
            definition = pending.pop()
            if _scan_testclient(definition.func, rootdir, item, fixture=True, visited=visited):
                return True
            for dependency in getattr(definition, 'argnames', ()):
                if dependency != 'request' and dependency not in visited:
                    visited.add(dependency)
                    dependencies = _fixture_definitions(item, dependency)
                    if not dependencies:
                        return True
                    pending.extend(dependencies)
    return False


@cache
def _function_uses_testclient(function, rootdir=_repository_root):
    return _scan_testclient(function, rootdir)


def _item_uses_testclient(item):
    rootdir = item.config.rootpath
    if _scan_testclient(item.obj, rootdir, item) is not False:
        return True
    if item.cls is not None and _scan_testclient(item.cls, rootdir, item) is not False:
        return True
    for name in item.fixturenames:
        for fixture in item._fixtureinfo.name2fixturedefs.get(name, ()):
            if _scan_testclient(fixture.func, rootdir, item, fixture=True):
                return True
    return False


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    if item.getparent(pytest.Module) is None or not _item_uses_testclient(item):
        return
    if _probe_result not in item.config.stash:
        item.config.stash[_probe_result] = probe_testclient()
    failure = item.config.stash[_probe_result]
    if failure is not None:
        if os.environ.get('CI', '').lower() not in {'', '0', 'false'}:
            pytest.fail(failure, pytrace=False)
        pytest.skip(failure)


def pytest_sessionfinish(session):
    failure = session.config.stash.get(_probe_result, None)
    if failure is not None and hasattr(session.config, 'workeroutput'):
        session.config.workeroutput['testclient_sandbox_failure'] = failure


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    failure = getattr(node, 'workeroutput', {}).get('testclient_sandbox_failure')
    if failure is not None:
        if _worker_failures not in node.config.stash:
            node.config.stash[_worker_failures] = set()
        node.config.stash[_worker_failures].add(failure)


def pytest_terminal_summary(terminalreporter):
    failures = set(terminalreporter.config.stash.get(_worker_failures, set()))
    failure = terminalreporter.config.stash.get(_probe_result, None)
    if failure is not None:
        failures.add(failure)
    for message in sorted(failures):
        terminalreporter.write_line(message)
