"""Static tripwire for fixture mutations that can race live workspace workers.

This deliberately small, local data-flow check is not a Python/SQL interpreter.
It follows ordinary assignment aliases, SQL literals/concatenation/f-strings and
SQLite imports, and direct local helper arguments with known SQLite provenance.
Known reflective builtin namespaces are rejected at exposure, before aliases or
opaque containers can hide dynamic execution. Explicit safe builtin attributes
remain supported. Scope dictionaries and the module registry retain their known
builtin entry through deterministic key lookups, shallow copies and pure dictionary
unions. Namespace-bearing mutations and value enumeration are rejected rather
than interpreting aliased mutable state. Unresolved keys into exact mappings with
namespace-bearing entries are rejected without reconstructing iterated keys.
Computed imports and arbitrary reflection
are not interpreted. Supported pure dictionary operations retain dictionary type
separately from exact literal entries; unknown keys never become an empty mapping.
Keep fixture setup explicit so that this check and reviewers can
recognize its write boundary. A deliberate raw concurrency, migration or restore
operation may carry ``# fixture-write: <specific reason>`` on its own statement.
That exception never exempts a surrounding function or the body of a ``with``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, replace
import io
import re
import tokenize


_WRITE = re.compile(r"(?:^|;)\s*(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|REINDEX|VACUUM|ATTACH|DETACH)\b", re.I)
_TABLE = re.compile(r"\bworkbench_", re.I)
_TRANSACTION = re.compile(r"^\s*(?:BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b", re.I)
_ROLLBACK_CONFLICT = re.compile(r"\b(?:OR|ON\s+CONFLICT)\s+ROLLBACK\b|\bRAISE\s*\(\s*ROLLBACK\b", re.I)
_SQL_IDENTIFIER = r'''(?:\w+|"(?:""|[^"])*"|'(?:''|[^'])*'|`(?:``|[^`])*`|\[[^\]]*\])'''
_PRAGMA = re.compile(rf"^\s*PRAGMA\s+(?:{_SQL_IDENTIFIER}\s*\.\s*)?({_SQL_IDENTIFIER})\s*(\()?", re.I)
_ACTION_PRAGMAS = {"optimize", "wal_checkpoint", "incremental_vacuum", "shrink_memory"}
_READ_ARGUMENT_PRAGMAS = {
    "table_info", "table_xinfo", "table_list", "index_info", "index_xinfo",
    "index_list", "foreign_key_list", "foreign_key_check", "integrity_check", "quick_check",
}
_SQL_METHODS = {"execute", "executemany", "executescript"}
_TRANSACTION_METHODS = {"commit", "rollback"}
_CONTEXT_METHODS = {"__enter__", "__exit__"}
_UNSUPPORTED_SQLITE_METHODS = {"deserialize", "blobopen", "setconfig", "enable_load_extension", "load_extension", "__call__"}
_NON_SQL_METHODS = _UNSUPPORTED_SQLITE_METHODS | {"backup", "setlimit", "interrupt"}
_CALLBACK_METHODS = {
    "set_trace_callback", "set_authorizer", "set_progress_handler",
    "create_function", "create_aggregate", "create_collation", "create_window_function",
}
_CONNECTION_PARAMETERS = {"connection", "conn", "db", "database"}
_MEDIA_SEED = {"queue_media_job", "import_media_transcript", "finish_media_job"}
_DICT_VALUE_METHODS = {"values", "items", "get", "pop", "popitem", "setdefault", "copy",
                       "__getitem__", "__or__", "__ror__", "__ior__"}
_BUILTIN_NAMESPACE_KEYS = {"<python-scope>": "__builtins__", "sys.modules": "builtins"}
_NAMESPACE_PROVIDERS = {
    "globals", "locals", "vars", "builtins.globals", "builtins.locals", "builtins.vars",
    "__import__", "builtins.__import__", "importlib.import_module",
    "getattr", "builtins.getattr", "builtins.__getattribute__",
}
_NAMESPACE_LOOKUPS = {"get", "__getitem__", "pop", "setdefault"}
_NAMESPACE_MUTATIONS = {"update", "__ior__", "__setitem__", "setdefault", "__init__"}
_NAMESPACE_ENUMERATIONS = {"values", "items", "popitem"}
_NAMESPACE_SEQUENCE_STORES = {"append", "extend", "insert", "add", "update", "__iadd__"}


# Reviewed Python AST shapes; unknown nodes or fields fail closed before data flow.
_AST_FIELDS = {
    ast.Module: ('body', 'type_ignores'),
    ast.FunctionDef: ('name', 'args', 'body', 'decorator_list', 'returns', 'type_comment', 'type_params'),
    ast.AsyncFunctionDef: ('name', 'args', 'body', 'decorator_list', 'returns', 'type_comment', 'type_params'),
    ast.ClassDef: ('name', 'bases', 'keywords', 'body', 'decorator_list', 'type_params'),
    ast.Return: ('value',),
    ast.Delete: ('targets',),
    ast.Assign: ('targets', 'value', 'type_comment'),
    ast.AnnAssign: ('target', 'annotation', 'value', 'simple'),
    ast.AugAssign: ('target', 'op', 'value'),
    ast.For: ('target', 'iter', 'body', 'orelse', 'type_comment'),
    ast.AsyncFor: ('target', 'iter', 'body', 'orelse', 'type_comment'),
    ast.While: ('test', 'body', 'orelse'),
    ast.If: ('test', 'body', 'orelse'),
    ast.With: ('items', 'body', 'type_comment'),
    ast.AsyncWith: ('items', 'body', 'type_comment'),
    ast.Match: ('subject', 'cases'),
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
    ast.Await: ('value',),
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
    ast.match_case: ('pattern', 'guard', 'body'),
    ast.MatchValue: ('value',),
    ast.MatchSingleton: ('value',),
    ast.MatchSequence: ('patterns',),
    ast.MatchMapping: ('keys', 'patterns', 'rest'),
    ast.MatchClass: ('cls', 'patterns', 'kwd_attrs', 'kwd_patterns'),
    ast.MatchStar: ('name',),
    ast.MatchAs: ('pattern', 'name'),
    ast.MatchOr: ('patterns',),
    ast.TypeIgnore: ('lineno', 'tag'),
}


@dataclass(frozen=True)
class Violation:
    filename: str
    line: int
    rule: str
    structural: bool = False

    def __str__(self) -> str:
        if self.structural:
            return f"{self.filename}:{self.line}: {self.rule}; use explicit named handles with fixture_write(); comments cannot exempt unsupported syntax"
        return f"{self.filename}:{self.line}: {self.rule}; use fixture_write(workspace) or document this exact intentional raw operation"


@dataclass(frozen=True)
class _Value:
    symbol: str = ""
    text: str = ""
    complete_text: bool = False
    nonstring_constant: bool = False
    connection: str | None = None
    live_workspace: bool = False
    workspace: bool = False
    active_path: bool = False
    imported_function: bool = False
    fixture_handle: bool = False
    elements: tuple[_Value, ...] | None = None
    mapping: tuple[tuple[str, _Value], ...] | None = None
    dictionary_instance: bool = False
    methods: frozenset[str] = frozenset()
    receiver: _Value | None = None
    sqlite_cursor: bool = False
    lock_owner: str | None = None
    unannotated_parameter: bool = False
    descriptor: bool = False
    setter_attribute: str | None = None
    unresolved_shared_container: bool = False
    unresolved_active_path: bool = False
    imported_module: bool = False
    scalar_converter: bool | None = None
    dictionary_accessor: bool = False
    dictionary_fromkeys: bool = False
    dictionary_method: str = ""


class _Check(ast.NodeVisitor):
    def __init__(self, source: str, filename: str, tree: ast.AST):
        self.filename = filename
        self.aliases: dict[str, _Value] = {}
        self.class_outer_aliases: dict[str, _Value] | None = None
        self.class_outer_names: set[str] = set()
        self.eager_variable_annotations = True
        self.protected: set[str] = set()
        self.nested_protected: set[str] = set()
        self.media_protected: set[str] = set()
        self.local_functions: dict[str, tuple] = {}
        self.active_calls: set[str] = set()
        # Evaluated calls and literal containers capture values before later rebindings.
        self.evaluated_values: dict[ast.AST, _Value] = {}
        self.return_values: list[_Value] = []
        self.outer_write_names: set[str] | None = None
        # Owning locks permit connection configuration, not SQL transactions.
        self.held_locks: set[str] = set()
        self.eager_walrus: dict[str, list[_Value]] | None = None
        self.deferred: list[tuple] = []
        self.late_bindings: dict[str, list[_Value]] = {}
        self.collect_bindings = True
        self.comprehension_locals: set[str] = set()
        self.violations: list[Violation] = []
        self.postponed_annotations = any(
            isinstance(node, ast.ImportFrom) and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
            for node in getattr(tree, "body", [])
        )
        self.exempt: set[ast.AST] = set()
        self.parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        statements = [node for node in ast.walk(tree) if isinstance(node, ast.stmt)]
        lines = source.splitlines()
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type != tokenize.COMMENT:
                continue
            reason = token.string.removeprefix("#").strip()
            if not reason.startswith("fixture-write:"):
                continue
            reason = reason.partition(":")[2].strip()
            if len(reason) < 20 or len(reason.split()) < 4:
                continue
            line = token.start[0]
            if not lines[line - 1][:token.start[1]].strip():
                candidates = [node for node in statements if node.lineno == line + 1 and node.col_offset == token.start[1]]
            else:
                candidates = [node for node in statements if
                              (node.end_lineno == line and node.end_col_offset <= token.start[1]) or
                              (isinstance(node, (ast.With, ast.AsyncWith)) and
                               any(item.context_expr.end_lineno == line for item in node.items))]
            if candidates:
                # One comment exempts one statement, even on a semicolon line.
                self.exempt.add(max(candidates, key=lambda node: (node.lineno, node.col_offset)))

    def value(self, node: ast.AST | None) -> _Value:
        if node is None:
            return _Value()
        if node in self.evaluated_values:
            return self.evaluated_values[node]
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, _Value(symbol=node.id, scalar_converter=True if node.id == "str" else None))
        if isinstance(node, ast.Lambda):
            return _Value(symbol=f"lambda@{node.lineno}:{node.col_offset}")
        if isinstance(node, ast.Constant):
            return _Value(symbol=repr(node.value), text=node.value if isinstance(node.value, str) else "",
                          active_path=isinstance(node.value, str) and "workbench.sqlite" in node.value,
                          complete_text=isinstance(node.value, str), nonstring_constant=not isinstance(node.value, str))
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            elements = []
            inputs = []
            opaque = False
            for item in node.elts:
                if isinstance(item, ast.Starred):
                    value = self.iteration_value(self.value(item.value))
                    inputs.append(value)
                    expanded = value.elements
                    if expanded is None:
                        opaque = True
                    else:
                        elements.extend(expanded)
                else:
                    value = self.value(item)
                    inputs.append(value)
                    elements.append(value)
            if opaque:
                return self.payload_value(inputs)
            return _Value(elements=tuple(elements))
        if isinstance(node, ast.Dict):
            mapping = {}
            inputs = []
            opaque = False
            for key, item in zip(node.keys, node.values):
                value = self.value(item)
                inputs.append(value)
                if key is None:
                    expanded = self.namespace_mapping(value)
                    if expanded is None:
                        opaque = True
                    else:
                        mapping.update(expanded)
                else:
                    name = self.value(key)
                    inputs.append(name)
                    if not name.complete_text or self.has_active_path(name):
                        opaque = True
                    else:
                        mapping[name.text] = value
            if opaque:
                return self.opaque_dictionary_value(inputs)
            return _Value(mapping=tuple(mapping.items()))
        if isinstance(node, ast.Attribute):
            base = self.value(node.value)
            symbol = base.symbol + "." + node.attr
            if node.attr in {"__globals__", "f_globals", "f_locals"}:
                return _Value(symbol="<python-scope>")
            if node.attr == "f_builtins":
                return _Value(symbol="<reflective-builtins>")
            if base.symbol in _BUILTIN_NAMESPACE_KEYS and node.attr in _DICT_VALUE_METHODS | _NAMESPACE_MUTATIONS:
                return _Value(symbol=symbol, receiver=base, dictionary_method=node.attr)
            if node.attr in {"append", "extend", "insert", "add", "__setitem__", "__iadd__"}:
                return _Value(symbol=symbol, receiver=base)
            if base.symbol in {"dict", "builtins.dict"} and node.attr in _DICT_VALUE_METHODS | _NAMESPACE_MUTATIONS:
                return _Value(symbol=symbol, dictionary_accessor=True, dictionary_method=node.attr)
            if node.attr == "fromkeys" and (base.symbol in {"dict", "builtins.dict"}
                                            or self.is_dictionary(base)):
                return _Value(symbol=symbol, dictionary_fromkeys=True)
            accessor = self.container_accessor_value(base, node.attr)
            if accessor is not None:
                return replace(accessor, symbol=symbol, receiver=base, dictionary_method=node.attr)
            if self.is_dictionary(base) and node.attr in _DICT_VALUE_METHODS | _NAMESPACE_MUTATIONS:
                return _Value(symbol=symbol, receiver=base, dictionary_method=node.attr)
            if base.imported_module and symbol in {"builtins.str", "os.fspath", "pathlib.Path", "pathlib.PurePath"}:
                return _Value(symbol=symbol, scalar_converter=True)
            if node.attr in {"__set__", "__delete__"} and base.setter_attribute is not None:
                return replace(base, symbol=symbol)
            if base.symbol in {"sqlite3.Connection", "sqlite3.Cursor"} and node.attr in {"row_factory", "text_factory", "isolation_level", "autocommit"}:
                return _Value(symbol=symbol, setter_attribute=node.attr)
            if node.attr == "connection":
                return _Value(symbol=symbol, connection=base.connection or base.symbol,
                              fixture_handle=base.fixture_handle)
            if node.attr == "_lock":
                return _Value(symbol=symbol, lock_owner=base.symbol)
            if base.symbol in {"WorkspaceStore", "case_intelligence.workspace_store.WorkspaceStore"}:
                return _Value(symbol=symbol, methods=frozenset({node.attr}), descriptor=True)
            if node.attr in _SQL_METHODS | _TRANSACTION_METHODS | _CONTEXT_METHODS | _CALLBACK_METHODS | _NON_SQL_METHODS | _MEDIA_SEED | {"cursor", "close"}:
                return _Value(symbol=symbol, connection=base.connection, fixture_handle=base.fixture_handle,
                              methods=frozenset({node.attr}), receiver=base,
                              descriptor=base.symbol in {"sqlite3.Connection", "sqlite3.Cursor"})
            active_path = node.attr == "path" and (base.live_workspace or base.symbol in self.workspace_symbols)
            return _Value(symbol=symbol, live_workspace=node.attr == "workspace", workspace=node.attr == "workspace",
                          active_path=active_path,
                          receiver=base if not active_path and (base.workspace or base.live_workspace or (base.symbol and base.symbol in self.protected)) else None)
        if isinstance(node, ast.JoinedStr):
            parts = [self.value(part.value if isinstance(part, ast.FormattedValue) else part) for part in node.values]
            return _Value(text="".join(part.text if part.complete_text else "?" for part in parts), active_path=any(part.active_path for part in parts),
                          complete_text=all(part.complete_text for part in parts)
                          and all(not isinstance(part, ast.FormattedValue)
                                  or part.conversion == -1 and part.format_spec is None for part in node.values))
        if isinstance(node, ast.BinOp):
            left, right = self.value(node.left), self.value(node.right)
            return self.binary_value(left, right, node.op)
        if isinstance(node, ast.NamedExpr):
            return self.value(node.value)
        if isinstance(node, ast.IfExp):
            return self.merge_aliases({"value": self.value(node.body)}, {"value": self.value(node.orelse)})["value"]
        if isinstance(node, ast.BoolOp):
            return self.merge_aliases(*({"value": self.value(value)} for value in node.values))["value"]
        if isinstance(node, ast.Call):
            if node in self.evaluated_values:
                return self.evaluated_values[node]
            function = self.value(node.func)
            if not function.unannotated_parameter:
                if (function.symbol in {"globals", "locals", "vars", "builtins.globals", "builtins.locals", "builtins.vars"}
                        and not node.args and not node.keywords):
                    return _Value(symbol="<python-scope>")
                if function.symbol in {"__import__", "builtins.__import__", "importlib.import_module"}:
                    argument = node.args[0] if node.args else next(
                        (kw.value for kw in node.keywords if kw.arg == "name"), None)
                    name = self.value(argument)
                    if name.complete_text and name.text == "builtins":
                        return _Value(symbol="<reflective-builtins>")
                if function.symbol in {"vars", "builtins.vars"} and node.args:
                    if self.value(node.args[0]).symbol == "builtins":
                        return _Value(symbol="<reflective-builtins>")
                if function.symbol == "builtins.__getattribute__" and node.args:
                    name = self.value(node.args[0])
                    return _Value(symbol="builtins." + name.text if name.complete_text
                                  else "<reflective-builtins>")
            if function.receiver is not None and (self.contains_namespace(function.receiver) or self.is_dictionary(function.receiver)):
                if function.dictionary_method == "copy" and not node.args:
                    return function.receiver
                if function.dictionary_method in _NAMESPACE_LOOKUPS and node.args:
                    default = (self.value(node.args[1]) if len(node.args) > 1 else
                               _Value(symbol="None", nonstring_constant=True) if function.dictionary_method == "get" else None)
                    return self.mapping_lookup(function.receiver, self.value(node.args[0]), default) or _Value()
            if (function.dictionary_accessor and function.dictionary_method in _NAMESPACE_LOOKUPS
                    or function.symbol == "operator.getitem") and len(node.args) >= 2:
                default = (self.value(node.args[2]) if len(node.args) > 2 else
                           _Value(symbol="None", nonstring_constant=True) if function.dictionary_method == "get" else None)
                resolved = self.mapping_lookup(self.value(node.args[0]), self.value(node.args[1]), default)
                if resolved is not None:
                    return resolved
            if function.symbol in {"dict", "builtins.dict"}:
                return self.dictionary_value([self.value(arg) for arg in node.args],
                                             [(kw.arg, self.value(kw.value)) for kw in node.keywords])
            if self.unresolved_ownership(function):
                return function
            owner, _, method = function.symbol.rpartition('.')
            if function.dictionary_fromkeys and node.args:
                return self.fromkeys_value(self.value(node.args[0]),
                                           self.value(node.args[1]) if len(node.args) > 1 else _Value())
            if function.dictionary_accessor and node.args:
                accessor = self.container_accessor_value(self.value(node.args[0]), method if owner in {"dict", "builtins.dict"} else "*")
                if accessor is not None:
                    return accessor
            if "__enter__" in function.methods:
                return self.value(node.args[0]) if function.descriptor and node.args else function.receiver or _Value()
            if function.descriptor and node.args and function.methods & {"cursor", "execute", "executemany"}:
                return replace(self.value(node.args[0]), sqlite_cursor=True)
            if function.symbol in {"getattr", "builtins.getattr"} and len(node.args) >= 2:
                name = self.value(node.args[1])
                if name.complete_text:
                    return self.value(ast.Attribute(value=node.args[0], attr=name.text, ctx=ast.Load()))
                receiver = self.value(node.args[0])
                if receiver.symbol == "builtins":
                    return _Value(symbol="<reflective-builtins>")
                if receiver.symbol in {"dict", "builtins.dict"}:
                    return _Value(dictionary_accessor=True)
                if receiver.connection is not None:
                    # A dynamically selected callable on a known SQL connection
                    # must not discard ownership just because its name is unknown.
                    return _Value(connection=receiver.connection, fixture_handle=receiver.fixture_handle,
                                  methods=frozenset(_SQL_METHODS | _TRANSACTION_METHODS | _CONTEXT_METHODS | _CALLBACK_METHODS | _NON_SQL_METHODS | {"close"}), receiver=receiver)
                if receiver.workspace or receiver.live_workspace or (receiver.symbol and receiver.symbol in self.protected):
                    # The SQL-scope rule rejects every workspace method, so an
                    # unknown name must retain its known receiver as well.
                    return _Value(receiver=receiver)
            if function.connection is not None and function.symbol.rsplit(".", 1)[-1] in {"cursor", "execute", "executemany"}:
                return replace(function, symbol=function.symbol.rsplit(".", 1)[0], methods=frozenset(), receiver=None,
                               sqlite_cursor=True)
            if (function.scalar_converter is not None or function.symbol in {
                    "str", "Path", "pathlib.Path", "pathlib.PurePath", "os.fspath", "builtins.str"}) and node.args:
                argument = self.value(node.args[0])
                if function.scalar_converter is not True:
                    return argument
                # Paths/text retain source provenance for active-database opens,
                # but do not expose the original workspace or SQL handle.
                return _Value(symbol=ast.unparse(node), text=argument.text,
                              complete_text=argument.complete_text, active_path=argument.active_path)
            if function.symbol == "sqlite3.connect":
                argument = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg == "database"), None)
                path = self.value(argument)
                kind = "workspace-sqlite" if path.active_path or "workbench.sqlite" in path.text else "sqlite"
                return _Value(symbol=f"{kind}@{node.lineno}", connection=f"{kind}@{node.lineno}")
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"cursor", "execute", "executemany"}:
                return self.value(node.func.value)
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"format", "join"}:
                base = self.value(node.func.value)
                arguments = [self.value(argument) for argument in [*node.args, *(keyword.value for keyword in node.keywords)]]
                return replace(base, active_path=base.active_path or any(argument.active_path for argument in arguments), complete_text=False)
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"as_posix", "__fspath__", "resolve", "absolute"}:
                base = self.value(node.func.value)
                if base.active_path:
                    return base
        return _Value(symbol=ast.unparse(node))

    @property
    def workspace_symbols(self) -> set[str]:
        return {value.symbol for value in self.aliases.values() if value.workspace}

    def flag(self, node: ast.AST, rule: str, *, structural: bool = False) -> None:
        statement = node
        while not isinstance(statement, ast.stmt) and statement in self.parents:
            if isinstance(statement, (ast.Lambda, ast.GeneratorExp)):
                break
            statement = self.parents[statement]
        if structural or statement not in self.exempt:
            violation = Violation(self.filename, getattr(node, "lineno", getattr(statement, "lineno", 1)), rule, structural)
            if violation not in self.violations:
                self.violations.append(violation)

    def bind(self, target: ast.AST, value: _Value, *, check_outer_write: bool = True) -> None:
        if self.unresolved_ownership(value) and hasattr(target, "lineno"):
            self.flag(target, "heterogeneous shared fixture containers are unsupported; use explicit named handles", structural=True)
        if isinstance(target, ast.Name):
            if (check_outer_write and self.outer_write_names is not None and target.id in self.outer_write_names
                    and target.id not in self.comprehension_locals
                    and self.known_ownership(value, shared_only=True)):
                self.flag(target, "shared global/nonlocal fixture writes are unsupported; return an explicit handle", structural=True)
            self.aliases[target.id] = value
            if self.collect_bindings and (self.class_outer_aliases is None or target.id in self.class_outer_names):
                self.late_bindings.setdefault(target.id, []).append(value)
            if self.class_outer_aliases is not None and target.id in self.class_outer_names:
                self.class_outer_aliases[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            values = value.elements
            stars = [i for i, item in enumerate(target.elts) if isinstance(item, ast.Starred)]
            if values is None or (not stars and len(values) != len(target.elts)) or (stars and len(values) < len(target.elts) - 1):
                for item in target.elts:
                    self.bind(item, _Value())
            elif stars:
                index = stars[0]
                end = len(values) - (len(target.elts) - index - 1)
                for item, element in zip(target.elts[:index], values[:index]):
                    self.bind(item, element)
                rest = _Value(elements=values[index:end])
                if self.known_ownership(rest, shared_only=True) or self.has_active_path(rest):
                    self.flag(target.elts[index], "starred assignment creates an unsupported fixture list; use fixed unpacking and fixture_write()", structural=True)
                self.bind(target.elts[index], rest)
                for item, element in zip(target.elts[index + 1:], values[end:]):
                    self.bind(item, element)
            else:
                for item, element in zip(target.elts, values):
                    self.bind(item, element)
        elif isinstance(target, ast.Starred):
            self.bind(target.value, value)
        elif isinstance(target, ast.Attribute):
            # Assignment leaves execute left to right. An earlier name target
            # can replace the receiver of a later SQLite descriptor setter.
            self.visit(target.value)
            self.check_transaction_assignment(target)
        elif isinstance(target, ast.Subscript):
            self.visit(target.value)
            receiver = self.value(target.value)
            self.visit(target.slice)
            if self.contains_namespace(value):
                self.flag(target, "dynamic execution namespace stores are unsupported; use pure dictionary expressions", structural=True)
            marker = self.payload_value([value])
            if self.unresolved_ownership(marker):
                self.flag(target, "shared or active-path container stores are unsupported; use explicit named handles", structural=True)
            else:
                self.visit(target)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        self.visit(node.value)
        value = self.value(node.value)
        self.visit(node.slice)
        resolved = self.mapping_lookup(value, self.value(node.slice))
        if resolved is not None and isinstance(node.ctx, ast.Load):
            self.evaluated_values[node] = resolved
            self.reject_dynamic_builtin(node, resolved)
        if isinstance(node.slice, ast.Constant):
            index = node.slice.value
            if value.elements is not None and isinstance(index, int) and -len(value.elements) <= index < len(value.elements):
                self.evaluated_values[node] = value.elements[index]
            elif value.mapping is not None and isinstance(index, str):
                self.evaluated_values[node] = dict(value.mapping).get(index, _Value())
        if (self.unresolved_ownership(value) or
                ((value.elements is not None or value.mapping is not None)
                 and self.known_ownership(value, shared_only=True))):
            self.flag(node, "subscripting a known shared fixture value is unsupported; use explicit named handles", structural=True)

    def reject_dynamic_builtin(self, node: ast.AST, value: _Value | None = None) -> None:
        value = self.value(node) if value is None else value
        if (value.symbol in {"exec", "eval", "builtins.exec", "builtins.eval",
                             "__builtins__", "builtins.__dict__", "<reflective-builtins>"}
                and not value.unannotated_parameter):
            self.flag(node, "dynamic execution is unsupported; use fixed-input reviewed probes or fixture_write()", structural=True)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.reject_dynamic_builtin(node)
        if isinstance(node.ctx, ast.Load) and self.unresolved_ownership(self.value(node)):
            self.flag(node, "heterogeneous shared fixture containers are unsupported; use explicit named handles", structural=True)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.visit(node.value)
        if isinstance(node.ctx, ast.Load):
            self.reject_dynamic_builtin(node)
        base = self.value(node.value)
        if base.elements is not None and self.known_ownership(base, shared_only=True):
            self.flag(node, "shared tuple attribute extraction is unsupported; unpack named handles and use fixture_write()", structural=True)
        if self.unresolved_ownership(self.value(node)):
            self.flag(node, "attributes of unresolved shared fixture containers are unsupported; use explicit named handles", structural=True)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        inputs, elements = [], []
        opaque = False
        for item in node.elts:
            operand = item.value if isinstance(item, ast.Starred) else item
            self.visit(operand)
            value = self.value(operand)
            if isinstance(item, ast.Starred):
                value = self.iteration_value(value)
            inputs.append(value)
            if isinstance(item, ast.Starred):
                opaque |= value.elements is None
                elements.extend(value.elements or ())
            else:
                elements.append(value)
        self.evaluated_values[node] = (self.payload_value(inputs)
            if opaque else _Value(elements=tuple(elements)))
        self.reject_dynamic_builtin(node)
        if opaque and self.unresolved_ownership(self.evaluated_values[node]):
            self.flag(node, "opaque fixture tuple expansion is unsupported; use fixed tuples and fixture_write()", structural=True)

    def binary_value(self, left, right, operator):
        shared = any(self.known_ownership(value, shared_only=True) for value in (left, right))
        container_path = any(self.has_active_path(value) and
                             (value.elements is not None or value.mapping is not None or value.unresolved_active_path)
                             for value in (left, right))
        if shared or container_path:
            return _Value(unresolved_shared_container=shared, unresolved_active_path=container_path)
        if isinstance(operator, ast.BitOr) and (any(self.contains_namespace(value) for value in (left, right))
                                               or all(self.is_dictionary(value) for value in (left, right))):
            return self.dictionary_value([left, right], [])
        return _Value(symbol=left.symbol + "/" + right.symbol,
                      text=left.text + right.text, active_path=left.active_path or right.active_path,
                      complete_text=isinstance(operator, ast.Add) and left.complete_text and right.complete_text)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        self.visit(node.left)
        left = self.value(node.left)
        self.visit(node.right)
        result = self.binary_value(left, self.value(node.right), node.op)
        self.evaluated_values[node] = result
        self.reject_dynamic_builtin(node, result)
        if self.unresolved_ownership(result):
            self.flag(node, "fixture container binary operations are unsupported; use literal tuples and fixture_write()", structural=True)

    def visit_List(self, node: ast.List) -> None:
        self.visit_Tuple(node)
        self.reject_mutable_carrier(node, self.value(node))

    def reject_mutable_carrier(self, node, value):
        if self.known_ownership(value, shared_only=True):
            self.flag(node, "mutable shared fixture containers are unsupported; use fixed tuples and fixture_write()", structural=True)


    def visit_Set(self, node: ast.Set) -> None:
        self.visit_Tuple(node)
        self.reject_mutable_carrier(node, self.value(node))
        marker = self.payload_value([self.evaluated_values[node]])
        if self.unresolved_ownership(marker) or self.contains_namespace(marker):
            # Set iteration has no positional correspondence to source order.
            self.evaluated_values[node] = marker
        self.reject_dynamic_builtin(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        # Capture each input when Python evaluates it. A later key or value
        # may rebind an alias, but cannot change a key already in the dictionary.
        inputs = []
        mapping = {}
        opaque = False
        for key, item in zip(node.keys, node.values):
            if key is not None:
                self.visit(key)
                name = self.value(key)
                inputs.append(name)
                opaque |= not name.complete_text or self.has_active_path(name)
            self.visit(item)
            value = self.value(item)
            inputs.append(value)
            if key is None:
                expanded = self.namespace_mapping(value)
                opaque |= expanded is None
                mapping.update(expanded or ())
            elif name.complete_text:
                mapping[name.text] = value
        shared = any(self.known_ownership(value, shared_only=True) for value in inputs)
        self.evaluated_values[node] = (self.opaque_dictionary_value(inputs) if opaque
                                  else _Value(mapping=tuple(mapping.items())))
        self.reject_dynamic_builtin(node)
        self.reject_mutable_carrier(node, self.evaluated_values[node])
        if opaque and self.has_active_path(self.evaluated_values[node]):
            self.flag(node, "opaque dictionary paths are unsupported; use named paths with fixture_write()", structural=True)
        if shared:
            self.flag(node, "heterogeneous shared fixture containers are unsupported; use explicit named handles", structural=True)

    @staticmethod
    def unresolved_ownership(value: _Value) -> bool:
        return value.unresolved_shared_container or value.unresolved_active_path or any(_Check.unresolved_ownership(item) for item in (
            *([value.receiver] if value.receiver is not None else []),
            *(value.elements or ()), *(item for _, item in value.mapping or ()),
        ))

    def container_accessor_value(self, receiver: _Value, method: str) -> _Value | None:
        if method not in _DICT_VALUE_METHODS | {"*"}:
            return None
        if (self.unresolved_ownership(receiver) or
                receiver.mapping is not None):
            shared = self.known_ownership(receiver, shared_only=True)
            active_path = self.has_active_path(receiver)
            if shared or active_path:
                return _Value(unresolved_shared_container=shared,
                              unresolved_active_path=active_path, active_path=active_path)
        return None

    def payload_value(self, values: list[_Value]) -> _Value:
        # Opaque aggregates lose element layout, never protected provenance.
        return _Value(symbol="<reflective-builtins>" if any(self.contains_namespace(value) for value in values) else "",
                      unresolved_shared_container=any(self.known_ownership(value, shared_only=True) for value in values),
                      unresolved_active_path=any(self.has_active_path(value) for value in values))

    def iteration_value(self, value: _Value) -> _Value:
        # Scope/module registries and exact string-key mappings yield keys,
        # not their namespace-bearing values. Keep cardinality unknown and
        # preserve the existing conservative SQL/path provenance checks.
        if value.symbol in _BUILTIN_NAMESPACE_KEYS or value.mapping is not None:
            return replace(self.payload_value([value]), symbol="")
        return value

    @staticmethod
    def mapping_lookup(receiver: _Value, key: _Value, default: _Value | None = None) -> _Value | None:
        if (not key.complete_text and not key.nonstring_constant
                and receiver.mapping is not None and _Check.contains_namespace(receiver)):
            return _Value(symbol="<reflective-builtins>")
        if key.complete_text and _BUILTIN_NAMESPACE_KEYS.get(receiver.symbol) == key.text:
            return _Value(symbol="<reflective-builtins>")
        if key.complete_text and receiver.mapping is not None:
            return dict(receiver.mapping).get(key.text, default)
        if key.nonstring_constant and receiver.mapping is not None:
            return default
        return None

    @staticmethod
    def is_dictionary(value: _Value) -> bool:
        return value.dictionary_instance or value.mapping is not None or value.symbol in _BUILTIN_NAMESPACE_KEYS

    @staticmethod
    def contains_namespace(value: _Value) -> bool:
        return (value.symbol in _BUILTIN_NAMESPACE_KEYS or value.symbol == "<reflective-builtins>"
                or not value.unannotated_parameter and value.symbol in _NAMESPACE_PROVIDERS | {"builtins"}
                or any(_Check.contains_namespace(item) for item in (
                    *([value.receiver] if value.receiver is not None else []),
                    *(value.elements or ()), *(item for _, item in value.mapping or ()),
                )))

    @staticmethod
    def namespace_mapping(value: _Value) -> tuple[tuple[str, _Value], ...] | None:
        key = _BUILTIN_NAMESPACE_KEYS.get(value.symbol)
        return ((key, _Value(symbol="<reflective-builtins>")),) if key else value.mapping

    def opaque_dictionary_value(self, values: list[_Value]) -> _Value:
        # Dictionary type is known even when keys are not; mapping=None must
        # remain distinct from the exact empty mapping used by literal {}.
        return replace(self.payload_value(values), dictionary_instance=True)

    def dictionary_value(self, arguments: list[_Value], keywords: list[tuple[str | None, _Value]]) -> _Value:
        if len(arguments) == 1 and not keywords and arguments[0].symbol in _BUILTIN_NAMESPACE_KEYS:
            return arguments[0]
        mapping = {}
        opaque = False
        inputs = [*arguments, *(value for _, value in keywords)]
        for value in arguments:
            expanded = self.namespace_mapping(value)
            if expanded is None:
                opaque = True
            else:
                mapping.update(expanded)
        for key, value in keywords:
            expanded = self.namespace_mapping(value)
            if key is not None:
                mapping[key] = value
            elif expanded is not None:
                mapping.update(expanded)
            else:
                opaque = True
        return self.opaque_dictionary_value(inputs) if opaque else _Value(mapping=tuple(mapping.items()))

    def fromkeys_value(self, keys: _Value, default: _Value) -> _Value:
        # A known mapping has literal string keys, independent of its values.
        # Other supported key inputs retain their already-evaluated ownership.
        inputs = [default] if keys.mapping is not None else [keys, default]
        names = ([key for key, _ in keys.mapping] if keys.mapping is not None else
                 [item.text for item in keys.elements] if keys.elements is not None
                 and all(item.complete_text for item in keys.elements) else None)
        return _Value(dictionary_instance=True,
                      mapping=tuple((key, default) for key in dict.fromkeys(names)) if names is not None else None,
                      unresolved_shared_container=any(self.known_ownership(value, shared_only=True) for value in inputs),
                      unresolved_active_path=any(self.has_active_path(value) for value in inputs))

    def declare_functions(self, body: list[ast.stmt]) -> None:
        # A local workflow helper named execute is inspected at its definition;
        # its callers are not connection methods just because of that spelling.
        for statement in body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.bind(ast.Name(id=statement.name), _Value(symbol=f"function@{statement.lineno}:{statement.name}"))

    def defer(self, kind, node, defaults=None, annotations=None):
        lexical = self.class_outer_aliases if self.class_outer_aliases is not None else self.aliases
        self.deferred.append((kind, node, defaults, annotations, lexical.copy(), self.comprehension_locals.copy()))
        if kind in {"function", "lambda"}:
            symbol = f"function@{node.lineno}:{node.name}" if kind == "function" else f"lambda@{node.lineno}:{node.col_offset}"
            self.local_functions[symbol] = (kind, node, defaults, annotations, lexical.copy(),
                                            self.late_bindings, self.comprehension_locals.copy())

    def flush_deferred(self):
        # Python resolves globals/closure cells when a deferred body runs. Keep
        # every observed binding in its lexical scope, including an intermediate
        # live connection later replaced by an independent one. Defaults remain
        # the values captured separately when the definition was encountered.
        late = {name: self.merge_aliases(*({name: value} for value in values))[name]
                for name, values in self.late_bindings.items()}
        while self.deferred:
            jobs, self.deferred = self.deferred, []
            for kind, node, defaults, annotations, lexical, local_names in jobs:
                previous = self.aliases
                self.aliases = {**lexical, **{name: value for name, value in late.items() if name not in local_names}}
                if kind == "function":
                    self.analyze_function(node, defaults, annotations)
                elif kind == "lambda":
                    self.analyze_lambda(node, defaults)
                else:
                    self.visit_comprehension_scope(node, deferred=True, analyzed=True, first_iterable=defaults)
                self.aliases = previous

    @staticmethod
    def merge_aliases(*states: dict[str, _Value]) -> dict[str, _Value]:
        result = {}
        for name in set().union(*(state.keys() for state in states)):
            values = [state.get(name, _Value(symbol=name, scalar_converter=True if name == "str" else None)) for state in states]
            if all(value == values[0] for value in values):
                result[name] = values[0]
            elif all(value.elements is not None for value in values) and len({len(value.elements) for value in values}) == 1:
                result[name] = _Value(elements=tuple(
                    _Check.merge_aliases(*({"item": value.elements[index]} for value in values))["item"]
                    for index in range(len(values[0].elements))))
            elif all(value.mapping is not None for value in values) and all(
                    dict(value.mapping).keys() == dict(values[0].mapping).keys() for value in values):
                result[name] = _Value(mapping=tuple(
                    (key, _Check.merge_aliases(*({"item": dict(value.mapping)[key]} for value in values))["item"])
                    for key, _ in values[0].mapping))
            elif all(value.connection is not None and value.connection.startswith("sqlite@") for value in values):
                result[name] = _Value(symbol=f"sqlite@merged:{name}", connection=f"sqlite@merged:{name}",
                                      sqlite_cursor=all(value.sqlite_cursor for value in values))
            else:
                receivers = [value.receiver for value in values if value.receiver is not None]
                same_owner = all(value.connection == values[0].connection for value in values)
                connections = [str(value.connection) for value in values if value.connection is not None]
                merged_connection = (f"unknown@merged:{name}" if any(
                    not owner.startswith("sqlite@") for owner in connections) else
                    f"sqlite@merged:{name}" if connections else None)
                result[name] = _Value(symbol=values[0].symbol if values[0].symbol and all(value.symbol == values[0].symbol for value in values) else
                                      "<reflective-builtins>" if any(_Check.contains_namespace(value) for value in values) else
                                      values[0].symbol if all(value.symbol == values[0].symbol for value in values) else name,
                                      nonstring_constant=all(value.nonstring_constant for value in values),
                                      active_path=any(value.active_path for value in values),
                                      scalar_converter=True if all(value.scalar_converter is True for value in values) else
                                      False if any(value.scalar_converter is not None for value in values) else None,
                                      dictionary_accessor=any(value.dictionary_accessor for value in values),
                                      dictionary_fromkeys=any(value.dictionary_fromkeys for value in values),
                                      dictionary_instance=all(_Check.is_dictionary(value) for value in values),
                                      dictionary_method=values[0].dictionary_method if all(value.dictionary_method == values[0].dictionary_method for value in values) else "",
                                      connection=values[0].connection if same_owner else merged_connection,
                                      fixture_handle=same_owner and all(value.fixture_handle for value in values),
                                      live_workspace=any(value.live_workspace for value in values),
                                      workspace=any(value.workspace is True for value in values),
                                      unannotated_parameter=any(value.unannotated_parameter for value in values),
                                      descriptor=any(value.descriptor for value in values),
                                      setter_attribute=values[0].setter_attribute if all(value.setter_attribute == values[0].setter_attribute for value in values) else None,
                                      unresolved_shared_container=any(
                                          value.unresolved_shared_container or
                                          ((value.elements is not None or value.mapping is not None)
                                           and _Check.known_ownership(value, shared_only=True))
                                          for value in values),
                                      unresolved_active_path=any(
                                          value.unresolved_active_path or
                                          ((value.elements is not None or value.mapping is not None)
                                           and _Check.has_active_path(value))
                                          for value in values),
                                      sqlite_cursor=all(value.sqlite_cursor for value in values),
                                      lock_owner=values[0].lock_owner if all(value.lock_owner == values[0].lock_owner for value in values) else None,
                                      methods=frozenset().union(*(value.methods for value in values)),
                                      receiver=_Check.merge_aliases(*({"receiver": value} for value in receivers))["receiver"] if receivers else None)
        return result

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        before = self.aliases.copy()
        ends = []
        for body in (node.body, node.orelse):
            self.aliases = before.copy()
            for statement in body:
                self.visit(statement)
            ends.append(self.aliases.copy())
        self.aliases = self.merge_aliases(*ends)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        subject = self.value(node.subject)
        fallthrough = self.aliases.copy()
        ends = []

        def irrefutable(pattern):
            return ((isinstance(pattern, ast.MatchAs) and
                     (pattern.pattern is None or irrefutable(pattern.pattern))) or
                    (isinstance(pattern, ast.MatchOr) and any(map(irrefutable, pattern.patterns))))

        for case in node.cases:
            self.aliases = fallthrough.copy()
            destructures = any(isinstance(pattern, (ast.MatchSequence, ast.MatchMapping, ast.MatchClass))
                               for pattern in ast.walk(case.pattern))
            if self.known_ownership(subject, shared_only=True) and destructures:
                self.flag(case.pattern, "destructuring a known shared fixture value is unsupported; use explicit named handles", structural=True)
            self.visit(case.pattern)
            for pattern in ast.walk(case.pattern):
                name = getattr(pattern, "name", None) or getattr(pattern, "rest", None)
                if name:
                    previous = self.aliases.get(name, _Value())
                    value = (subject if not destructures and isinstance(pattern, ast.MatchAs)
                             else _Value(symbol=name))
                    if value.connection is None and previous.connection is not None:
                        value = replace(value, connection=f"unknown@pattern:{name}")
                    target = ast.copy_location(ast.Name(id=name), pattern)
                    self.parents[target] = pattern
                    self.bind(target, value)
            if case.guard is not None:
                self.visit(case.guard)
                # A false guard can still rebind aliases before the next case.
                fallthrough = self.merge_aliases(fallthrough, self.aliases)
            for statement in case.body:
                self.visit(statement)
            ends.append(self.aliases.copy())
            if case.guard is None and irrefutable(case.pattern):
                break
        else:
            ends.append(fallthrough)
        self.aliases = self.merge_aliases(*ends)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.visit(node.test)
        before = self.aliases.copy()
        ends = []
        for expression in (node.body, node.orelse):
            self.aliases = before.copy()
            self.visit(expression)
            ends.append(self.aliases.copy())
        self.aliases = self.merge_aliases(*ends)
        if self.unresolved_ownership(self.value(node)):
            self.flag(node, "heterogeneous shared fixture containers are unsupported; use explicit named handles", structural=True)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        ends = []
        for expression in node.values:
            self.visit(expression)
            ends.append(self.aliases.copy())
        self.aliases = self.merge_aliases(*ends)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        if self.unresolved_ownership(self.value(node.iter)):
            self.flag(node.iter, "heterogeneous shared fixture containers are unsupported; use explicit named handles", structural=True)
        self.visit_loop(node, target=node.target)

    visit_AsyncFor = visit_For

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self.visit_loop(node)

    def visit_loop(self, node, target=None):
        before = self.aliases.copy()
        if target is not None:
            elements = self.iteration_value(self.value(node.iter)).elements
            value = self.merge_aliases(*({"item": element} for element in elements))["item"] if elements else _Value()
            self.bind(target, value)
        for statement in node.body:
            self.visit(statement)
        self.aliases = self.merge_aliases(before, self.aliases)
        # A break can bypass the else block; zero iterations can bypass body.
        before_else = self.aliases.copy()
        for statement in node.orelse:
            self.visit(statement)
        self.aliases = self.merge_aliases(before_else, self.aliases)

    def visit_Try(self, node: ast.Try) -> None:
        snapshots = [self.aliases.copy()]
        for statement in node.body:
            self.visit(statement)
            snapshots.append(self.aliases.copy())
        for statement in node.orelse:
            self.visit(statement)
        ends = [self.aliases.copy()]
        for handler in node.handlers:
            self.aliases = self.merge_aliases(*snapshots)
            if handler.type is not None:
                self.visit(handler.type)
            if handler.name:
                self.aliases[handler.name] = _Value()
            for statement in handler.body:
                self.visit(statement)
            ends.append(self.aliases.copy())
        self.aliases = self.merge_aliases(*ends, *snapshots)
        for statement in node.finalbody:
            self.visit(statement)

    visit_TryStar = visit_Try

    def visit_Global(self, node: ast.Global) -> None:
        if self.outer_write_names is not None:
            self.outer_write_names.update(node.names)
        if self.class_outer_aliases is not None:
            self.class_outer_names.update(node.names)
            for name in node.names:
                self.aliases[name] = self.class_outer_aliases.get(name, _Value(symbol=name))

    visit_Nonlocal = visit_Global

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self.visit(target)
            pending = [target]
            while pending:
                item = pending.pop()
                if isinstance(item, (ast.Tuple, ast.List)):
                    pending.extend(item.elts)
                elif isinstance(item, ast.Attribute):
                    self.check_factory_deletion(item)
                elif isinstance(item, ast.Name):
                    self.aliases.pop(item.id, None)
                    if self.class_outer_aliases is not None and item.id in self.class_outer_aliases:
                        self.aliases[item.id] = self.class_outer_aliases[item.id]

    def visit_Module(self, node: ast.Module) -> None:
        self.declare_functions(node.body)
        self.generic_visit(node)
        self.flush_deferred()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.bind(ast.Name(id=alias.asname or alias.name), _Value(symbol=alias.name, imported_module=True))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            symbol = f"{node.module}.{alias.name}" if node.module else alias.name
            self.bind(ast.Name(id=alias.asname or alias.name), _Value(symbol=symbol, imported_function=True,
                      scalar_converter=True if symbol in {"builtins.str", "os.fspath", "pathlib.Path", "pathlib.PurePath"} else None))

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        value = self.value(node.value)
        if isinstance(node.value, ast.Call) and self.value(node.value.func).symbol in {"WorkspaceStore", "case_intelligence.workspace_store.WorkspaceStore"}:
            value = _Value(symbol=f"workspace@{node.lineno}", workspace=True)
        for target in node.targets:
            self.bind(target, value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
            self.bind(node.target, self.value(node.value))
        if self.eager_variable_annotations and not self.postponed_annotations:
            self.visit(node.annotation)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        value = self.value(node.value)
        self.bind(node.target, value)
        if self.eager_walrus is not None and isinstance(node.target, ast.Name):
            self.eager_walrus.setdefault(node.target.id, []).append(value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.target)
        left = self.value(node.target)
        self.visit(node.value)
        if any(self.contains_namespace(value) for value in (left, self.value(node.value))):
            self.flag(node, "dynamic execution namespace mutation is unsupported; use pure dictionary expressions", structural=True)
        result = self.binary_value(left, self.value(node.value), node.op)
        if self.unresolved_ownership(result):
            self.flag(node, "augmented fixture container operations are unsupported; use named handles and fixture_write()", structural=True)
        self.bind(node.target, result)

    def check_factory_deletion(self, target: ast.Attribute) -> None:
        if target.attr not in {"row_factory", "text_factory"}:
            return
        receiver = self.value(target.value)
        if receiver.connection is not None and not receiver.connection.startswith("sqlite@"):
            self.flag(target, "shared factory deletion is unsupported by fixture_write")

    def check_transaction_assignment(self, target: ast.AST) -> None:
        if isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self.check_transaction_assignment(item)
            return
        if isinstance(target, ast.Starred):
            self.check_transaction_assignment(target.value)
            return
        if isinstance(target, ast.Attribute) and target.attr == "text_factory":
            receiver = self.value(target.value)
            if receiver.connection is not None and not receiver.connection.startswith("sqlite@"):
                if receiver.connection not in self.protected | self.held_locks:
                    self.flag(target, "text_factory change requires the owning lock or fixture_write")
            return
        if isinstance(target, ast.Attribute) and target.attr == "row_factory":
            receiver = self.value(target.value)
            if receiver.connection is None or receiver.connection.startswith("sqlite@"):
                return
            if not receiver.fixture_handle or receiver.connection not in self.protected:
                self.flag(target, "row_factory change requires the yielded fixture_write handle")
            return
        if isinstance(target, ast.Attribute) and target.attr in {"autocommit", "isolation_level"}:
            receiver = self.value(target.value)
            if receiver.connection is not None and receiver.connection.startswith("sqlite@"):
                return
            if receiver.connection not in self.protected:
                self.flag(target, "transaction mode change outside fixture_write")
            elif not receiver.fixture_handle:
                self.flag(target, "transaction control requires the yielded fixture_write handle")

    def visit_With(self, node: ast.With) -> None:
        previous = self.protected.copy()
        previous_nested = self.nested_protected.copy()
        previous_media = self.media_protected.copy()
        previous_locks = self.held_locks.copy()
        for item in node.items:
            expression = item.context_expr
            self.visit(expression)
            value = self.value(expression)
            is_helper = isinstance(expression, ast.Call) and self.value(expression.func).symbol in {"fixture_write", "tests.fixture_write.fixture_write"}
            if value.lock_owner is not None:
                self.held_locks.add(value.lock_owner)
            workspace_argument = (expression.args[0] if expression.args else next(
                (kw.value for kw in expression.keywords if kw.arg == "workspace"), None)) if is_helper else None
            if is_helper and workspace_argument is not None:
                owner = self.value(workspace_argument).symbol
                if not owner and isinstance(workspace_argument, ast.Name):
                    # A tuple-unpacked fixture can have unknown provenance; its
                    # explicit boundary still names one owner, not every other
                    # unknown value (whose abstract symbol is also empty).
                    owner = workspace_argument.id
                    self.bind(workspace_argument, replace(self.value(workspace_argument), symbol=owner, workspace=True))
                media_mode = any(keyword.arg == "media" and isinstance(keyword.value, ast.Constant)
                                 and keyword.value.value is True for keyword in expression.keywords)
                if owner in self.protected:
                    self.nested_protected.add(owner)
                    if media_mode:
                        self.flag(expression, "media fixture_write cannot nest inside the same workspace boundary")
                if media_mode:
                    self.media_protected.add(owner)
                self.protected.add(owner)
                value = _Value(connection=owner, symbol=owner + ".connection", fixture_handle=True)
            elif value.connection is not None and not value.connection.startswith("sqlite@"):
                if value.connection not in self.protected:
                    self.flag(expression, "naked connection context")
                elif not value.fixture_handle:
                    self.flag(expression, "connection context requires the yielded fixture_write handle")
            if item.optional_vars is not None:
                self.bind(item.optional_vars, value)
        for statement in node.body:
            self.visit(statement)
        self.protected = previous
        self.nested_protected = previous_nested
        self.media_protected = previous_media
        self.held_locks = previous_locks

    visit_AsyncWith = visit_With

    def raw_cursor_result_escapes(self, node: ast.Call) -> bool:
        # This is an acquisition rule, not cursor lifetime emulation. A raw
        # result may be discarded or consumed by an immediate fetch/close;
        # retained, returned or passed cursors must come from the scoped proxy.
        current = node
        parent = self.parents.get(current)
        while isinstance(parent, ast.Attribute):
            if parent.attr in {"rowcount", "lastrowid", "description", "arraysize"}:
                return False
            call = self.parents.get(parent)
            if not isinstance(call, ast.Call) or call.func is not parent:
                break
            if parent.attr in {"fetchone", "fetchmany", "fetchall", "close"}:
                return False
            if parent.attr not in {"execute", "executemany", "cursor"}:
                break
            current = call
            parent = self.parents.get(current)
        return not isinstance(parent, ast.Expr)

    def visit_Call(self, node: ast.Call) -> None:
        # Evaluate nested calls first, so an explicitly returned receiver or
        # bound method retains ownership at its immediate invocation.
        self.visit(node.func)
        function = self.value(node.func)
        self.reject_dynamic_builtin(node.func, function)
        if self.unresolved_ownership(function):
            self.flag(node, "shared container accessors are unsupported; use explicit named handles", structural=True)
            self.evaluated_values[node] = function
        arguments_before = [self.value(argument.value if isinstance(argument, ast.Starred) else argument)
                            for argument in node.args]
        arguments_before.extend(self.value(keyword.value) for keyword in node.keywords)
        if any(self.unresolved_ownership(value) for value in arguments_before):
            self.flag(node, "heterogeneous shared fixture containers are unsupported; use explicit named handles", structural=True)
        actual_values, opaque_values = [], []
        for argument in node.args:
            operand = argument.value if isinstance(argument, ast.Starred) else argument
            self.visit(operand)
            value = self.value(operand)
            if isinstance(argument, ast.Starred):
                value = self.iteration_value(value)
                if value.elements is None:
                    opaque_values.append(value)
                else:
                    actual_values.extend(value.elements)
            else:
                actual_values.append(value)
        keyword_values = []
        for keyword in node.keywords:
            self.visit(keyword.value)
            keyword_values.append((keyword.arg, self.value(keyword.value)))
        if any(self.unresolved_ownership(value) for value in [*actual_values, *opaque_values, *(v for _, v in keyword_values)]):
            self.flag(node, "heterogeneous shared fixture containers are unsupported; use explicit named handles", structural=True)
        operands = [*actual_values, *opaque_values, *(v for _, v in keyword_values)]
        shared_tuples = [v for v in operands if v.elements is not None and self.known_ownership(v, shared_only=True)]
        thread_arguments = (function.symbol == "threading.Thread"
                            and not any(v.elements is not None and self.known_ownership(v, shared_only=True)
                                        for v in [*actual_values, *opaque_values])
                            and all(key == "args" for key, v in keyword_values
                                    if v.elements is not None and self.known_ownership(v, shared_only=True)))
        if shared_tuples and function.symbol not in self.local_functions and not thread_arguments:
            self.flag(node, "opaque shared tuple consumers are unsupported; use fixed helper parameters and fixture_write()", structural=True)
        method_name = function.symbol.rsplit(".", 1)[-1]
        if method_name in _NAMESPACE_SEQUENCE_STORES and any(self.contains_namespace(value) for value in operands):
            self.flag(node, "dynamic execution namespace stores are unsupported; use pure container expressions", structural=True)
        if (function.receiver is not None or function.dictionary_accessor) and method_name in {"append", "extend", "insert", "add", "update", "setdefault", "__setitem__", "__ior__", "__iadd__"}:
            if any(self.known_ownership(v, shared_only=True) or self.has_active_path(v) for v in operands):
                self.flag(node, "shared or active-path container mutation is unsupported; use named handles and fixture_write()", structural=True)
        if function.symbol in {"tuple", "list", "set", "frozenset", "dict", "builtins.tuple", "builtins.list", "builtins.set", "builtins.frozenset", "builtins.dict"}:
            materializes_rows = (function.symbol in {"tuple", "list", "builtins.tuple", "builtins.list"}
                                 and len(actual_values) == 1 and not opaque_values and not keyword_values
                                 and actual_values[0].sqlite_cursor)
            transports_path = (function.symbol not in {"dict", "builtins.dict"}
                               and any(self.has_active_path(v) for v in operands))
            if not materializes_rows and (transports_path or any(self.known_ownership(v, shared_only=True) for v in operands)):
                self.flag(node, "shared fixture container constructors are unsupported; use literal tuples and fixture_write()", structural=True)
        explicit_receiver = actual_values[0] if actual_values and not opaque_values else None
        owner, _, method = function.symbol.rpartition('.')
        dictionary_method = function.dictionary_method or (method if owner in {"dict", "builtins.dict"} else "*")
        dictionary_receiver = explicit_receiver if function.dictionary_accessor else function.receiver
        dictionary_arguments = actual_values[1:] if function.dictionary_accessor else actual_values
        namespace_inputs = [*dictionary_arguments, *opaque_values, *(value for _, value in keyword_values)]
        if dictionary_receiver is not None:
            namespace_inputs.append(dictionary_receiver)
        if ((function.dictionary_accessor or function.dictionary_method or method_name == "__setitem__") and any(
                self.contains_namespace(value) for value in namespace_inputs)
                or dictionary_method in {"copy", "__or__", "__ror__"} and dictionary_receiver is not None
                and self.is_dictionary(dictionary_receiver)):
            if dictionary_method in _NAMESPACE_MUTATIONS or method_name == "__setitem__":
                self.flag(node, "dynamic execution namespace mutation is unsupported; use pure dictionary expressions", structural=True)
            elif dictionary_method in _NAMESPACE_ENUMERATIONS:
                self.flag(node, "dynamic execution namespace enumeration is unsupported; use explicit literal key lookups", structural=True)
            elif dictionary_method in {"get", "pop"} and any(self.contains_namespace(value) for value in dictionary_arguments[1:]):
                self.flag(node, "dynamic execution namespace defaults are unsupported; use explicit named values", structural=True)
            elif dictionary_method == "copy" and dictionary_receiver is not None and not dictionary_arguments:
                self.evaluated_values[node] = dictionary_receiver
            elif dictionary_method in {"__or__", "__ror__"}:
                if dictionary_receiver is not None and len(dictionary_arguments) == 1 and not opaque_values and not keyword_values:
                    pair = [dictionary_receiver, dictionary_arguments[0]]
                    if dictionary_method == "__ror__":
                        pair.reverse()
                    self.evaluated_values[node] = self.dictionary_value(pair, [])
                else:
                    self.evaluated_values[node] = self.opaque_dictionary_value(namespace_inputs)
        if function.dictionary_accessor or function.dictionary_method:
            accessor = self.container_accessor_value(dictionary_receiver, dictionary_method) if dictionary_receiver is not None else None
            defaults = dictionary_arguments[1:] if dictionary_method in {"get", "pop", "setdefault", "*"} else []
            marker = self.payload_value([*([] if accessor is None else [accessor]), *defaults, *opaque_values])
            if dictionary_method == "update":
                marker = self.payload_value([*dictionary_arguments, *opaque_values, *(v for _, v in keyword_values)])
                if dictionary_receiver is not None and self.unresolved_ownership(marker):
                    self.flag(node, "shared or active-path container updates are unsupported; use explicit named handles", structural=True)
                self.evaluated_values[node] = _Value()
            elif self.unresolved_ownership(marker):
                self.evaluated_values[node] = marker
                self.flag(node, "container accessors with shared handles or active workspace paths are unsupported", structural=True)
        if function.dictionary_fromkeys and explicit_receiver is not None:
            if any(self.contains_namespace(value) for value in actual_values[1:]):
                self.flag(node, "dynamic execution namespace defaults are unsupported; use explicit named values", structural=True)
            result = self.fromkeys_value(explicit_receiver, actual_values[1] if len(actual_values) > 1 else _Value())
            self.evaluated_values[node] = result
            if self.unresolved_ownership(result):
                self.flag(node, "dictionary keys/defaults with shared handles or active workspace paths are unsupported", structural=True)
        if function.symbol in {"dict", "builtins.dict"}:
            result = self.dictionary_value([*actual_values, *opaque_values], keyword_values)
            self.evaluated_values[node] = result
            if self.unresolved_ownership(result):
                self.flag(node, "opaque dictionary construction with fixture provenance is unsupported", structural=True)
        argument_nodes = [*node.args, *(keyword.value for keyword in node.keywords)]
        if (any(isinstance(item, ast.NamedExpr) for argument in argument_nodes for item in ast.walk(argument))
                and any(self.known_ownership(value, shared_only=True)
                        for value in [function, *arguments_before, *(self.value(argument) for argument in argument_nodes)])):
            self.flag(node, "assignment expressions in shared fixture call arguments are unsupported; bind values before the call", structural=True)
        if function.setter_attribute is not None and function.symbol.endswith(".__delete__") and node.args:
            target = ast.copy_location(ast.Attribute(value=node.args[0], attr=function.setter_attribute, ctx=ast.Del()), node)
            self.parents[target] = node
            self.check_factory_deletion(target)
        if function.symbol in {"delattr", "builtins.delattr"} and len(node.args) >= 2:
            attribute = self.value(node.args[1])
            if attribute.complete_text:
                target = ast.copy_location(ast.Attribute(value=node.args[0], attr=attribute.text, ctx=ast.Del()), node)
                self.parents[target] = node
                self.check_factory_deletion(target)
        if function.setter_attribute is not None and function.symbol.endswith(".__set__") and node.args:
            target = ast.copy_location(ast.Attribute(value=node.args[0], attr=function.setter_attribute, ctx=ast.Store()), node)
            self.parents[target] = node
            self.check_transaction_assignment(target)
        if function.symbol in {"setattr", "builtins.setattr"} and len(node.args) >= 3:
            attribute = self.value(node.args[1])
            if attribute.complete_text:
                target = ast.copy_location(ast.Attribute(value=node.args[0], attr=attribute.text, ctx=ast.Store()), node)
                self.parents[target] = node
                self.check_transaction_assignment(target)
        if function.symbol == "sqlite3.connect":
            argument = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg == "database"), None)
            path = self.value(argument)
            if path.active_path or "workbench.sqlite" in path.text:
                self.flag(node, "direct SQLite connection to workspace database")
        method = function.symbol.rsplit(".", 1)[-1]
        methods = function.methods | {method}
        receiver = function.receiver or (self.value(node.func.value) if isinstance(node.func, ast.Attribute) else function)
        if (function.connection is not None and not function.methods
                and function.receiver is None and not function.sqlite_cursor):
            # Calling a Connection prepares a raw Statement; preparation alone
            # can mutate PRAGMAs. There is no scoped callable-handle contract.
            methods = methods | {"__call__"}
            receiver = function
        arguments = node.args
        if function.descriptor and arguments:
            receiver, arguments = self.value(arguments[0]), arguments[1:]
        if (methods & {"execute", "executemany", "cursor"} and receiver.connection in self.protected
                and not receiver.fixture_handle and self.raw_cursor_result_escapes(node)):
            self.flag(node, "retained raw cursor requires the yielded fixture_write handle")
        imported_function = isinstance(node.func, ast.Name) and function.imported_function and function.connection is None
        independent_connection = receiver.connection is not None and receiver.connection.startswith("sqlite@")
        if not independent_connection and not imported_function:
            if "close" in methods and receiver.connection is not None and not receiver.sqlite_cursor:
                self.flag(node, "shared connection close bypasses workspace lifetime")
            if methods & _CALLBACK_METHODS and (receiver.connection is not None or receiver.unannotated_parameter) and receiver.connection not in self.protected | self.held_locks:
                self.flag(node, "connection callback change requires the owning lock or fixture_write")
        if receiver.connection is not None and not independent_connection:
            if methods & _UNSUPPORTED_SQLITE_METHODS:
                # Unsupported database/configuration APIs have no scoped
                # equivalent; an owning lock does not license them.
                self.flag(node, "shared non-SQL SQLite operation is unsupported by fixture_write")
            if methods & {"setlimit", "interrupt"} and receiver.connection not in self.protected | self.held_locks:
                self.flag(node, "connection configuration/cancellation requires the owning lock or fixture_write")
        if "backup" in methods:
            destination = self.value(arguments[0] if arguments else next(
                (keyword.value for keyword in node.keywords if keyword.arg == "target"), None))
            if destination.connection is not None and not destination.connection.startswith("sqlite@"):
                self.flag(node, "backup into a shared connection is unsupported by fixture_write")
            if receiver.fixture_handle or receiver.connection in self.protected:
                self.flag(node, "backup inside the owning fixture_write boundary is unsupported")
            elif (receiver.connection is not None and not independent_connection
                  and receiver.connection not in self.protected | self.held_locks):
                self.flag(node, "backup from a shared connection requires the owning lock or fixture_write")
        if methods & _SQL_METHODS and not imported_function:
            argument = arguments[0] if arguments else next(
                (item.value for item in node.keywords if item.arg in {"sql", "sql_script"}), None)
            sql_value = self.value(argument)
            sql = sql_value.text
            # Strip quoted values/comments so SELECT 'update' is still a read.
            commands = re.sub(r"'(?:''|[^'])*'|--[^\n]*|/\*[\s\S]*?\*/", " ", sql)
            writes = _WRITE.search(commands) or re.search(r"^\s*PRAGMA\b[^;]*=", commands, re.I)
            pragma_source = re.sub(r"--[^\n]*|/\*[\s\S]*?\*/", " ", sql).lstrip(";\ufeff \t\r\n\v\f")
            # EXPLAIN can change connection PRAGMAs during statement preparation.
            # Only unwrap it for PRAGMA classification; EXPLAIN DML is a read.
            pragma_source = re.sub(r"^EXPLAIN(?:\s+QUERY\s+PLAN)?\s+", "", pragma_source, flags=re.I)
            pragma = _PRAGMA.match(pragma_source)
            if pragma and pragma_source[pragma.end():].lstrip().startswith("="):
                writes = True
            pragma_name = pragma[1].strip("\"'`[]").lower() if pragma else ""
            if pragma and (pragma_name in _ACTION_PRAGMAS or (pragma[2] and pragma_name not in _READ_ARGUMENT_PRAGMAS)):
                # Parentheses also set values: user_version(12) is a write.
                # Keep argument-taking schema/integrity inspection read-only.
                writes = True
            if re.match(r"\s*WITH\b", commands, re.I):
                writes = writes or re.search(r"\b(?:INSERT|UPDATE|DELETE|REPLACE)\b", commands, re.I)
            independent_connection = receiver.connection is not None and receiver.connection.startswith("sqlite@")
            known_read = re.match(r"\s*SELECT\b", commands, re.I) or (sql_value.complete_text and re.match(r"\s*(?:PRAGMA|EXPLAIN)\b", commands, re.I))
            # Callback parameters may receive a workspace connection even when
            # this module cannot prove its origin. Imported workflow functions
            # are checked at their definitions, not mistaken for SQL methods.
            # Unknown SQL therefore needs an explicit boundary or a precise audit
            # annotation; losing receiver provenance must not silently permit it.
            unknown_write = (not known_read and not writes) or "executescript" in methods
            # A literal DELETE/UPDATE is still a write when the callback's
            # parameter is unannotated or its table has no workbench_ prefix.
            if (unknown_write or (writes and (not independent_connection or _TABLE.search(sql)))) and receiver.connection not in self.protected:
                self.flag(node, "SQL fixture write outside fixture_write")
            nonimplicit_write = writes and not re.match(r"\s*(?:INSERT|UPDATE|DELETE|REPLACE)\b", commands, re.I)
            needs_handle = nonimplicit_write or re.match(r"(?:CREATE|DROP|ALTER|REINDEX|ATTACH|DETACH|VACUUM)\b", commands.lstrip(";\ufeff \t\r\n\v\f"), re.I) or (pragma and writes)
            if (unknown_write or needs_handle or _TRANSACTION.search(commands.lstrip(";\ufeff \t\r\n\v\f")) or _ROLLBACK_CONFLICT.search(commands)) and receiver.connection in self.protected and not receiver.fixture_handle:
                self.flag(node, "transaction control requires the yielded fixture_write handle")
        if (methods & _CONTEXT_METHODS and receiver.connection is not None
                and not independent_connection):
            if receiver.connection not in self.protected or not receiver.fixture_handle:
                self.flag(node, "explicit connection context requires the yielded fixture_write handle")
        if (receiver.symbol in self.protected and not methods & _MEDIA_SEED
                and (receiver.symbol not in self.media_protected or receiver.symbol in self.nested_protected)
                and (receiver.workspace or receiver.live_workspace or function.receiver is not None)):
            self.flag(node, "workspace method inside SQL fixture_write may terminate its transaction")
        if methods & _TRANSACTION_METHODS and not imported_function:
            # Product rollback APIs take domain arguments; a connection's
            # commit/rollback accepts none. Retain bound-method provenance too.
            connection_call = receiver.connection is not None or (not node.args and not node.keywords)
            independent_connection = receiver.connection is not None and receiver.connection.startswith("sqlite@")
            if connection_call and not independent_connection:
                if receiver.connection not in self.protected:
                    self.flag(node, "transaction termination outside fixture_write")
                elif not receiver.fixture_handle:
                    self.flag(node, "transaction control requires the yielded fixture_write handle")
        if methods & _MEDIA_SEED and (receiver.live_workspace or receiver.symbol in self.protected):
            if receiver.symbol in self.nested_protected:
                self.flag(node, "commit-capable media fixture seed inside nested fixture_write")
            elif receiver.symbol not in self.media_protected:
                self.flag(node, "manual media fixture seed requires fixture_write(workspace, media=True)")
        result = self.check_local_call(node, function)
        if result is not None:
            self.evaluated_values[node] = result
        self.reject_dynamic_builtin(node)

    def check_local_call(self, call, function):
        # Specialize direct local helpers with known fixture or namespace
        # provenance using the existing fixed-parameter body analysis.
        if function.symbol not in self.local_functions:
            return
        kind, node, defaults, annotations, lexical, history, local_names = self.local_functions[function.symbol]
        positional = [*node.args.posonlyargs, *node.args.args]
        actual = []
        unresolved_actual = []
        for index, argument in enumerate(call.args):
            if isinstance(argument, ast.Starred):
                elements = self.iteration_value(self.value(argument.value)).elements
                if elements is None:
                    # Dynamic iterable cardinality is outside this bounded
                    # literal/alias expansion; do not guess later positions.
                    unresolved_actual = [self.iteration_value(self.value(item.value)) if isinstance(item, ast.Starred) else self.value(item)
                                         for item in call.args[index:]]
                    break
                actual.extend(elements)
            else:
                actual.append(self.value(argument))
        supplied = {argument.arg: value for argument, value in zip(positional, actual)}
        positional_only = {argument.arg for argument in node.args.posonlyargs}
        fixed_names = {argument.arg for argument in [*node.args.args, *node.args.kwonlyargs]}
        variadic_values = [*actual[len(positional):], *unresolved_actual] if node.args.vararg is not None else []
        for keyword in call.keywords:
            if keyword.arg is not None:
                if keyword.arg not in positional_only:
                    supplied[keyword.arg] = self.value(keyword.value)
                if node.args.kwarg is not None and keyword.arg not in fixed_names:
                    variadic_values.append(self.value(keyword.value))
            else:
                mapping = self.value(keyword.value).mapping
                if mapping is not None:
                    supplied.update((name, value) for name, value in mapping if name not in positional_only)
                    if node.args.kwarg is not None:
                        variadic_values.extend(value for name, value in mapping if name not in fixed_names)
        bindings = {**defaults, **supplied}
        late = {name: self.merge_aliases(*({name: value} for value in values))[name]
                for name, values in history.items() if name not in local_names}
        environment = {**lexical, **late}
        captures = self.captured_values(node, environment)
        # Surplus shared inputs are unsupported rather than silently dropped.
        # Fixed workspace parameters and ordinary keyword options keep the
        # existing direct-helper analysis, even if the signature is variadic.
        values = [*bindings.values(), *captures, *variadic_values]
        # Bare lexical providers are ordinary body references, not transported
        # namespace state. Containers/defaults/actual arguments retain carriers.
        namespace_captures = [value for value in captures
                              if value.symbol not in _NAMESPACE_PROVIDERS | {"builtins"}]
        namespace = any(self.contains_namespace(value) for value in
                        [*bindings.values(), *namespace_captures, *variadic_values])
        known = any(self.contains_namespace(value) or self.known_ownership(value)
                    or self.has_active_path(value) for value in values)
        if not known and (call.args or call.keywords):
            return
        if namespace and (any(self.contains_namespace(value) for value in variadic_values)
                          or isinstance(node, ast.AsyncFunctionDef) or self.has_yield(node)
                          or function.symbol in self.active_calls):
            self.flag(call, "dynamic execution namespace transport requires a nonrecursive synchronous helper with fixed parameters", structural=True)
            return self.payload_value(values)
        shared = any(self.known_ownership(value, shared_only=True) for value in values)
        if any(self.known_ownership(value, shared_only=True) for value in variadic_values):
            self.flag(call, "variadic helpers with shared fixture ownership are unsupported; use explicit named parameters", structural=True)
            return None
        if shared and (isinstance(node, ast.AsyncFunctionDef) or self.has_yield(node)):
            self.flag(call, "async/generator helper results with shared ownership are unsupported by fixture_write", structural=True)
            return None
        if function.symbol in self.active_calls:
            if shared and not isinstance(self.parents.get(call), ast.Expr):
                self.flag(call, "recursive helper results with shared ownership are unsupported by fixture_write", structural=True)
            return None
        previous = self.aliases
        self.aliases = environment
        self.active_calls.add(function.symbol)
        if kind == "function":
            result = self.analyze_function(node, bindings, annotations, invoked=True)
        else:
            result = self.analyze_lambda(node, bindings, invoked=True)
        self.active_calls.remove(function.symbol)
        self.aliases = previous
        return result

    @staticmethod
    def has_active_path(value):
        return value.active_path or value.unresolved_active_path or any(_Check.has_active_path(item) for item in (
            *(value.elements or ()), *(item for _, item in value.mapping or ()),
        ))

    @staticmethod
    def known_ownership(value, *, shared_only=False):
        if value.unresolved_shared_container:
            return True
        if value.workspace or value.live_workspace:
            return True
        if value.connection is not None and (not shared_only or not value.connection.startswith("sqlite@")):
            return True
        # Existing literal containers and bound methods retain their ownership.
        return any(_Check.known_ownership(item, shared_only=shared_only) for item in (
            *([value.receiver] if value.receiver is not None else []),
            *(value.elements or ()), *(item for _, item in value.mapping or ()),
        ))

    def captured_values(self, node, environment):
        # Only referenced lexical values count, not every ambient workspace.
        # Collect this function's local binders before looking at references;
        # Python shadows a closure/global for the whole function, not just after
        # the assignment. Nested bodies are analyzed separately by the worklist.
        parameters = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                      *([node.args.vararg] if node.args.vararg else []),
                      *([node.args.kwarg] if node.args.kwarg else [])]
        local = {argument.arg for argument in parameters}
        declared = set()
        references = []
        pending = list(node.body) if isinstance(node.body, list) else [node.body]
        while pending:
            item = pending.pop()
            if isinstance(item, (ast.Global, ast.Nonlocal)):
                declared.update(item.names)
            elif isinstance(item, ast.Name) and isinstance(item.ctx, (ast.Store, ast.Del)):
                local.add(item.id)
            elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                if not isinstance(item, ast.Lambda):
                    local.add(item.name)
                # Definitions bind here; their bodies have independent scope.
                continue
            elif isinstance(item, (ast.Import, ast.ImportFrom)):
                local.update(alias.asname or alias.name.split(".")[0] for alias in item.names)
            elif isinstance(item, ast.ExceptHandler) and item.name:
                local.add(item.name)
            elif isinstance(item, (ast.MatchAs, ast.MatchStar)) and item.name:
                local.add(item.name)
            elif isinstance(item, ast.MatchMapping) and item.rest:
                local.add(item.rest)
            if isinstance(item, ast.comprehension):
                # Iteration targets belong to the comprehension; walrus targets
                # in its expressions still bind in the containing function.
                pending.extend([item.iter, *item.ifs])
            else:
                pending.extend(ast.iter_child_nodes(item))
        local -= declared
        previous = self.aliases
        self.aliases = environment

        def collect(item, shadowed):
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                return
            if isinstance(item, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                shadowed = shadowed.copy()
                for generator in item.generators:
                    collect(generator.iter, shadowed)
                    shadowed.update(target.id for target in ast.walk(generator.target) if isinstance(target, ast.Name))
                    for condition in generator.ifs:
                        collect(condition, shadowed)
                for expression in ([item.key, item.value] if isinstance(item, ast.DictComp) else [item.elt]):
                    collect(expression, shadowed)
                return
            if isinstance(item, (ast.Name, ast.Attribute)) and isinstance(item.ctx, ast.Load):
                root = item
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name) and root.id not in shadowed:
                    references.append(self.value(item))
            for child in ast.iter_child_nodes(item):
                collect(child, shadowed)

        for item in node.body if isinstance(node.body, list) else [node.body]:
            collect(item, local)
        self.aliases = previous
        return references

    @staticmethod
    def has_yield(node) -> bool:
        pending = list(node.body) if isinstance(node.body, list) else [node.body]
        while pending:
            item = pending.pop()
            if isinstance(item, (ast.Yield, ast.YieldFrom)):
                return True
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                pending.extend(ast.iter_child_nodes(item))
        return False

    def visit_Return(self, node: ast.Return) -> None:
        if node.value is not None:
            self.visit(node.value)
        value = self.value(node.value)
        self.return_values.append(value)
        enclosing = self.parents.get(node)
        while enclosing is not None and not isinstance(enclosing, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            enclosing = self.parents.get(enclosing)
        if enclosing is not None and (isinstance(enclosing, ast.AsyncFunctionDef) or self.has_yield(enclosing)):
            self.reject_delayed_namespace_result(node, value)

    def reject_delayed_namespace_result(self, node, value):
        if self.contains_namespace(value):
            self.flag(node, "dynamic execution namespace results from async/generator helpers are unsupported; use synchronous fixed-parameter helpers", structural=True)

    def visit_Yield(self, node: ast.Yield) -> None:
        if node.value is not None:
            self.visit(node.value)
        self.reject_delayed_namespace_result(node, self.value(node.value))

    def visit_YieldFrom(self, node: ast.YieldFrom) -> None:
        self.visit(node.value)
        self.reject_delayed_namespace_result(node, self.iteration_value(self.value(node.value)))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Closures retain aliases, but can run after the defining lock is gone.
        for decorator in node.decorator_list:
            self.visit(decorator)
        positional = [*node.args.posonlyargs, *node.args.args]
        defaults = dict(zip((argument.arg for argument in positional[-len(node.args.defaults):]), node.args.defaults))
        defaults.update((argument.arg, default) for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults) if default is not None)
        for default in defaults.values():
            # Defaults execute now, outside the deferred function's scope.
            self.visit(default)
        default_values = {name: self.value(default) for name, default in defaults.items()}
        if not self.postponed_annotations:
            for argument in [*positional, *node.args.kwonlyargs, *([node.args.vararg] if node.args.vararg else []), *([node.args.kwarg] if node.args.kwarg else [])]:
                if argument.annotation is not None:
                    self.visit(argument.annotation)
            if node.returns is not None:
                self.visit(node.returns)
        annotated = {argument.arg: self.value(argument.annotation).symbol for argument in [*positional, *node.args.kwonlyargs]}
        # Definitions can also occur inside a with/loop/branch body, where no
        # enclosing module/function declaration pass prebound their names.
        self.bind(ast.Name(id=node.name), _Value(symbol=f"function@{node.lineno}:{node.name}"))
        self.defer("function", node, default_values, annotated)

    def analyze_function(self, node, default_values, annotated, *, invoked=False):
        outer_write_names, self.outer_write_names = self.outer_write_names, set()
        outer_returns, outer_calls = self.return_values, self.evaluated_values
        self.return_values, self.evaluated_values = [], {}
        positional = [*node.args.posonlyargs, *node.args.args]
        outer_deferred, outer_bindings, collecting = self.deferred, self.late_bindings, self.collect_bindings
        self.deferred, self.late_bindings, self.collect_bindings = [], {}, True
        aliases, protected, class_outer = self.aliases, self.protected, self.class_outer_aliases
        held_locks, eager_walrus = self.held_locks, self.eager_walrus
        self.held_locks, self.eager_walrus = held_locks.copy() if invoked else set(), None
        nested = self.nested_protected
        media = self.media_protected
        self.nested_protected = nested.copy() if invoked else set()
        self.media_protected = media.copy() if invoked else set()
        eager_annotations = self.eager_variable_annotations
        self.eager_variable_annotations = False
        self.aliases = (class_outer if class_outer is not None else aliases).copy()
        self.protected, self.class_outer_aliases = protected.copy() if invoked else set(), None
        for argument in [*positional, *node.args.kwonlyargs, *([node.args.vararg] if node.args.vararg else []), *([node.args.kwarg] if node.args.kwarg else [])]:
            self.aliases.pop(argument.arg, None)
            if argument.arg in default_values:
                self.aliases[argument.arg] = default_values[argument.arg]
            elif annotated.get(argument.arg) == "sqlite3.Connection":
                self.aliases[argument.arg] = _Value(symbol=argument.arg, connection=argument.arg)
            elif annotated.get(argument.arg) in {"WorkspaceStore", "case_intelligence.workspace_store.WorkspaceStore"}:
                self.aliases[argument.arg] = _Value(symbol=argument.arg, workspace=True)
            if argument.annotation is None and argument.arg.strip("_") in _CONNECTION_PARAMETERS:
                existing = self.aliases.get(argument.arg)
                if existing is None or existing.connection is None:
                    self.aliases[argument.arg] = _Value(symbol=argument.arg, connection=argument.arg)
            elif argument.annotation is None and argument.arg not in default_values:
                self.aliases[argument.arg] = _Value(symbol=argument.arg, unannotated_parameter=True)
        self.declare_functions(node.body)
        for statement in node.body:
            self.visit(statement)
        result = self.merge_aliases(*({"return": value} for value in self.return_values)).get("return", _Value())
        self.flush_deferred()
        self.aliases, self.protected, self.class_outer_aliases = aliases, protected, class_outer
        self.held_locks, self.eager_walrus = held_locks, eager_walrus
        self.nested_protected = nested
        self.media_protected = media
        self.eager_variable_annotations = eager_annotations
        self.deferred, self.late_bindings, self.collect_bindings = outer_deferred, outer_bindings, collecting
        self.return_values, self.evaluated_values = outer_returns, outer_calls
        self.outer_write_names = outer_write_names
        return result

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in [*node.decorator_list, *node.bases, *(keyword.value for keyword in node.keywords)]:
            self.visit(expression)
        aliases, class_outer = self.aliases, self.class_outer_aliases
        outer_names, self.class_outer_names = self.class_outer_names, set()
        eager_annotations = self.eager_variable_annotations
        self.eager_variable_annotations = True
        lexical = class_outer if class_outer is not None else aliases
        outer_write_names, self.outer_write_names = self.outer_write_names, set()
        self.aliases, self.class_outer_aliases = lexical.copy(), lexical
        self.declare_functions(node.body)
        for statement in node.body:
            self.visit(statement)
        for name in self.class_outer_names:
            lexical[name] = self.aliases.get(name, _Value(symbol=name))
        self.class_outer_names = outer_names
        self.aliases, self.class_outer_aliases = aliases, class_outer
        self.eager_variable_annotations = eager_annotations
        self.aliases[node.name] = _Value(symbol=f"class@{node.lineno}:{node.name}")
        self.outer_write_names = outer_write_names

    def visit_Lambda(self, node: ast.Lambda) -> None:
        positional = [*node.args.posonlyargs, *node.args.args]
        defaults = dict(zip((argument.arg for argument in positional[-len(node.args.defaults):]), node.args.defaults))
        defaults.update((argument.arg, default) for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults) if default is not None)
        for default in defaults.values():
            self.visit(default)
        default_values = {name: self.value(default) for name, default in defaults.items()}
        self.defer("lambda", node, default_values)

    def analyze_lambda(self, node, default_values, *, invoked=False):
        outer_write_names, self.outer_write_names = self.outer_write_names, None
        outer_returns, outer_calls = self.return_values, self.evaluated_values
        self.return_values, self.evaluated_values = [], {}
        positional = [*node.args.posonlyargs, *node.args.args]
        outer_deferred, outer_bindings, collecting = self.deferred, self.late_bindings, self.collect_bindings
        self.deferred, self.late_bindings, self.collect_bindings = [], {}, True
        aliases, protected, class_outer = self.aliases, self.protected, self.class_outer_aliases
        held_locks, eager_walrus = self.held_locks, self.eager_walrus
        self.held_locks, self.eager_walrus = held_locks.copy() if invoked else set(), None
        nested = self.nested_protected
        media = self.media_protected
        self.nested_protected = nested.copy() if invoked else set()
        self.media_protected = media.copy() if invoked else set()
        self.aliases = (class_outer if class_outer is not None else aliases).copy()
        self.protected, self.class_outer_aliases = protected.copy() if invoked else set(), None
        for argument in [*positional, *node.args.kwonlyargs, *([node.args.vararg] if node.args.vararg else []), *([node.args.kwarg] if node.args.kwarg else [])]:
            self.aliases[argument.arg] = default_values.get(argument.arg, _Value())
            if argument.arg not in default_values:
                self.aliases[argument.arg] = _Value(symbol=argument.arg, unannotated_parameter=True)
            if argument.arg.strip("_") in _CONNECTION_PARAMETERS and self.aliases[argument.arg].connection is None:
                self.aliases[argument.arg] = _Value(symbol=argument.arg, connection=argument.arg)
        self.visit(node.body)
        result = self.value(node.body)
        self.flush_deferred()
        self.aliases, self.protected, self.class_outer_aliases = aliases, protected, class_outer
        self.held_locks, self.eager_walrus = held_locks, eager_walrus
        self.nested_protected = nested
        self.media_protected = media
        self.deferred, self.late_bindings, self.collect_bindings = outer_deferred, outer_bindings, collecting
        self.return_values, self.evaluated_values = outer_returns, outer_calls
        self.outer_write_names = outer_write_names
        return result

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self.visit(node.generators[0].iter)
        self.defer("generator", node, self.value(node.generators[0].iter))

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self.visit_comprehension_scope(node, deferred=False)

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp

    def visit_comprehension_scope(self, node, *, deferred, analyzed=False, first_iterable=None):
        # Python evaluates only the first iterable at generator creation. The
        # remaining clauses and result expression execute when it is consumed.
        # Eager comprehensions keep protection but bind their own loop names.
        if not analyzed:
            self.visit(node.generators[0].iter)
            first_iterable = self.value(node.generators[0].iter)
        collecting, self.collect_bindings = self.collect_bindings, False
        outer_locals = self.comprehension_locals
        self.comprehension_locals = outer_locals.copy()
        aliases, protected, class_outer = self.aliases, self.protected, self.class_outer_aliases
        held_locks, eager_walrus = self.held_locks, self.eager_walrus
        self.held_locks = set() if deferred else held_locks.copy()
        nested = self.nested_protected
        media = self.media_protected
        self.nested_protected = set() if deferred else nested.copy()
        self.media_protected = set() if deferred else media.copy()
        self.eager_walrus = None if deferred else {}
        self.aliases = (class_outer if class_outer is not None else aliases).copy()
        self.protected, self.class_outer_aliases = set() if deferred else protected.copy(), None
        for index, generator in enumerate(node.generators):
            if index:
                self.visit(generator.iter)
            iterable = self.value(generator.iter) if index else first_iterable
            if iterable is not None:
                iterable = self.iteration_value(iterable)
            if iterable is not None and self.unresolved_ownership(iterable):
                self.flag(generator.iter, "heterogeneous shared fixture containers are unsupported; use explicit named handles", structural=True)
            elements = iterable.elements if iterable is not None else None
            value = self.merge_aliases(*({"item": element} for element in elements))["item"] if elements else _Value()
            self.comprehension_locals.update(item.id for item in ast.walk(generator.target) if isinstance(item, ast.Name))
            self.bind(generator.target, value)
            for condition in generator.ifs:
                self.visit(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            key = self.value(node.key)
            self.visit(node.value)
            shared = any(self.known_ownership(value, shared_only=True) for value in (key, self.value(node.value)))
            result = self.opaque_dictionary_value([key, self.value(node.value)])
            self.evaluated_values[node] = result
            self.reject_dynamic_builtin(node, result)
            if shared:
                self.flag(node, "shared dictionary comprehension results are unsupported; use explicit named handles", structural=True)
            if result.unresolved_active_path:
                self.flag(node, "active-path dictionary comprehension results are unsupported; use named paths and fixture_write()", structural=True)
        else:
            self.visit(node.elt)
            element = self.value(node.elt)
            if deferred and (self.known_ownership(element, shared_only=True) or self.has_active_path(element)):
                self.flag(node, "generator transport of fixture values is unsupported; use fixed named inputs and fixture_write()", structural=True)
            result = self.payload_value([element])
            self.evaluated_values[node] = result
            self.reject_dynamic_builtin(node, result)
            if not deferred:
                self.reject_mutable_carrier(node, element)
                self.evaluated_values[node] = (_Value(receiver=element)
                    if element.fixture_handle and element.connection in self.protected
                    else result)
        assignments = self.eager_walrus or {}
        self.aliases, self.protected, self.class_outer_aliases = aliases, protected, class_outer
        self.held_locks, self.eager_walrus = held_locks, eager_walrus
        self.collect_bindings = collecting
        self.nested_protected = nested
        self.media_protected = media
        self.comprehension_locals = outer_locals
        for name, values in assignments.items():
            # Assignment expressions bind in the containing non-comprehension
            # scope. Include the old value for zero iterations/filtered rows,
            # and every assignment because a later filter can skip a rewrite.
            merged = self.merge_aliases({name: aliases.get(name, _Value(symbol=name))},
                                        *({name: value} for value in values))[name]
            target = ast.copy_location(ast.Name(id=name), node)
            self.parents[target] = node
            # The actual walrus target was already checked at its own location.
            # This is propagation of zero-iteration/branch outcomes, not a new
            # outer-scope assignment of the prior binding.
            self.bind(target, merged, check_outer_write=False)
            if self.eager_walrus is not None:
                self.eager_walrus.setdefault(name, []).extend(values)


def check_fixture_writes(source: str, filename: str = "<fixture>") -> list[Violation]:
    """Return actionable boundary violations without importing the test module."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as error:
        return [Violation(filename, error.lineno or 1, "unsupported syntax; use fixture_write() and supported Python syntax", True)]
    checker = _Check(source, filename, tree)
    for node in ast.walk(tree):
        fields = _AST_FIELDS.get(type(node))
        if fields is None or tuple(node._fields) != fields or getattr(node, "type_params", ()):
            checker.flag(node, "unsupported syntax requires an explicit fixture_write() boundary and reviewed AST support", structural=True)
    if not checker.violations:
        checker.visit(tree)
    return checker.violations
