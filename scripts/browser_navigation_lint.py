"""Find direct WebDriver navigation without confusing mappings or HTTP clients."""
from __future__ import annotations

import ast

_DRIVER = 'driver'
_WAIT = 'driver-wait'
_CONSTRUCTORS = {'Chrome', 'ChromiumEdge', 'Edge', 'Firefox', 'Ie', 'Remote', 'Safari', 'WebDriver'}
_MODULES = {'selenium', 'selenium.webdriver', 'selenium.webdriver.support', 'selenium.webdriver.support.ui'}
for _browser in ('chrome', 'chromium', 'edge', 'firefox', 'ie', 'remote', 'safari'):
    _MODULES.update({f'selenium.webdriver.{_browser}', f'selenium.webdriver.{_browser}.webdriver'})


def navigation_violations(source: str) -> list[tuple[int, str]]:
    """Follow Selenium constructors, lexical bindings, aliases and helper arguments.

    This is a static contract for journey source, not arbitrary Python execution.
    A binding that can hold a driver is treated conservatively as a driver.
    """
    tree = ast.parse(source)
    scopes = {0: {'parent': None, 'locals': set()}}
    owners = {}
    functions = {}
    values = {}

    def bind(scope, name, tags):
        key = (scope, name)
        previous = values.setdefault(key, set())
        before = len(previous)
        previous.update(tags)
        return len(previous) != before

    def index(node, scope):
        owners[id(node)] = scope
        if isinstance(node, ast.ClassDef):
            child = id(node)
            scopes[child] = {'parent': scope, 'locals': set(), 'class': True}
            for item in node.body:
                index(item, child)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            child = id(node)
            scopes[child] = {'parent': scope, 'locals': set()}
            functions[child] = node
            if not isinstance(node, ast.Lambda):
                scopes[scope]['locals'].add(node.name)
                bind(scope, node.name, {f'function:{child}'})
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                scopes[child]['locals'].add(arg.arg)
            for item in ast.iter_child_nodes(node):
                index(item, child)
            # Defaults are evaluated in the defining scope, not the callee.
            for item in (*node.args.defaults, *node.args.kw_defaults):
                if item is not None:
                    index(item, scope)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            scopes[scope]['locals'].add(node.id)
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split('.')[0]
                scopes[scope]['locals'].add(name)
                bind(scope, name, {alias.name if alias.asname else name})
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                scopes[scope]['locals'].add(name)
                bind(scope, name, {f'{node.module}.{alias.name}'})
        for item in ast.iter_child_nodes(node):
            index(item, scope)

    index(tree, 0)

    def key(node):
        root = node
        while isinstance(root, (ast.Attribute, ast.Subscript)):
            root = root.value
        if not isinstance(root, ast.Name):
            return None
        scope = owners[id(node)]
        while root.id not in scopes[scope]['locals'] and scopes[scope]['parent'] is not None:
            scope = scopes[scope]['parent']
        if isinstance(node, (ast.Attribute, ast.Subscript)) and scope in functions:
            parameters = [*functions[scope].args.posonlyargs, *functions[scope].args.args]
            parent = scopes[scope]['parent']
            if parameters and root.id == parameters[0].arg and scopes[parent].get('class'):
                scope = parent
        return scope, ast.unparse(node)

    def tags(node):
        if node is None:
            return set()
        result = set(values.get(key(node), ()))
        if isinstance(node, ast.Attribute):
            base = tags(node.value)
            # Expand actual module paths only; rebinding an import to its own
            # constructor must not grow fictional paths forever in the fixed point.
            result.update(f'{name}.{node.attr}' for name in base if name in _MODULES)
            if _DRIVER in base and node.attr in {'get', 'refresh'}:
                result.add(f'raw:{node.attr}')
        elif isinstance(node, ast.Call):
            called = tags(node.func)
            if any(name.startswith('selenium.webdriver.') and name.rsplit('.', 1)[-1] in _CONSTRUCTORS
                   for name in called):
                result.add(_DRIVER)
            if (any(name.endswith('.WebDriverWait') for name in called)
                    and node.args and _DRIVER in tags(node.args[0])):
                result.add(_WAIT)
            for name in called:
                if name.startswith('function:'):
                    result.update(values.get((int(name.split(':')[1]), '<return>'), ()))
            if (isinstance(node.func, ast.Name) and node.func.id == 'getattr' and len(node.args) >= 2
                    and _DRIVER in tags(node.args[0]) and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value in {'get', 'refresh'}):
                result.add(f'raw:{node.args[1].value}')
        elif isinstance(node, ast.Lambda):
            result.add(f'function:{id(node)}')
        elif isinstance(node, ast.BoolOp):
            for value in node.values:
                result.update(tags(value))
        elif isinstance(node, ast.IfExp):
            result.update(tags(node.body) | tags(node.orelse))
        return result

    def assign(target, value):
        changed = False
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
            for left, right in zip(target.elts, value.elts):
                changed |= assign(left, right)
        elif key(target) is not None:
            changed |= bind(*key(target), tags(value))
            scope, name = key(target)
            if isinstance(value, (ast.List, ast.Tuple)):
                for position, item in enumerate(value.elts):
                    changed |= bind(scope, f'{name}[{position}]', tags(item))
            elif isinstance(value, ast.Dict):
                for position, item in zip(value.keys, value.values):
                    if isinstance(position, ast.Constant):
                        changed |= bind(scope, f'{name}[{ast.unparse(position)}]', tags(item))
        return changed

    nodes = list(ast.walk(tree))
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                parameters = [*node.args.posonlyargs, *node.args.args]
                for parameter, default in zip(reversed(parameters), reversed(node.args.defaults)):
                    changed |= bind(id(node), parameter.arg, tags(default))
                for parameter, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
                    changed |= bind(id(node), parameter.arg, tags(default))
                if isinstance(node, ast.Lambda):
                    changed |= bind(id(node), '<return>', tags(node.body))
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    changed |= assign(target, node.value)
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
                changed |= assign(node.target, node.value)
            if isinstance(node, ast.arg) and node.annotation is not None:
                annotation = tags(node.annotation)
                if any(name.startswith('selenium.webdriver.') and name.endswith('.WebDriver') for name in annotation):
                    changed |= bind(owners[id(node)], node.arg, {_DRIVER})
            if isinstance(node, ast.Return):
                changed |= bind(owners[id(node)], '<return>', tags(node.value))
            if isinstance(node, ast.Call):
                for name in tags(node.func):
                    if not name.startswith('function:'):
                        continue
                    scope = int(name.split(':')[1])
                    args = functions[scope].args
                    for parameter, argument in zip((*args.posonlyargs, *args.args), node.args):
                        changed |= bind(scope, parameter.arg, tags(argument))
                    for argument in node.keywords:
                        if argument.arg in scopes[scope]['locals']:
                            changed |= bind(scope, argument.arg, tags(argument.value))
                if (isinstance(node.func, ast.Attribute) and node.func.attr in {'until', 'until_not'}
                        and _WAIT in tags(node.func.value) and node.args):
                    for name in tags(node.args[0]):
                        if name.startswith('function:'):
                            scope = int(name.split(':')[1])
                            args = functions[scope].args.args
                            if args:
                                changed |= bind(scope, args[0].arg, {_DRIVER})
    return sorted({(node.lineno, tag.split(':')[1]) for node in nodes
                   if isinstance(node, (ast.Attribute, ast.Call))
                   for tag in tags(node) if tag.startswith('raw:')})
