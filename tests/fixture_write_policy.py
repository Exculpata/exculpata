"""Static tripwire for fixture mutations that can race live workspace workers.

This deliberately small, local data-flow check is not a Python/SQL interpreter.
It follows ordinary assignment aliases, SQL literals/concatenation/f-strings and
SQLite imports, and direct local helper arguments with known SQLite provenance.
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
_CALLBACK_METHODS = {
    "set_trace_callback", "set_authorizer", "set_progress_handler",
    "create_function", "create_aggregate", "create_collation", "create_window_function",
}
_CONNECTION_PARAMETERS = {"connection", "conn", "db", "database"}
_MEDIA_SEED = {"queue_media_job", "import_media_transcript", "finish_media_job"}


@dataclass(frozen=True)
class Violation:
    filename: str
    line: int
    rule: str

    def __str__(self) -> str:
        return f"{self.filename}:{self.line}: {self.rule}; use fixture_write(workspace) or document this exact intentional raw operation"


@dataclass(frozen=True)
class _Value:
    symbol: str = ""
    text: str = ""
    complete_text: bool = False
    connection: str | None = None
    live_workspace: bool = False
    workspace: bool = False
    active_path: bool = False
    imported_function: bool = False
    fixture_handle: bool = False
    elements: tuple[_Value, ...] | None = None
    methods: frozenset[str] = frozenset()
    receiver: _Value | None = None
    sqlite_cursor: bool = False
    lock_owner: str | None = None
    unannotated_parameter: bool = False
    descriptor: bool = False
    setter_attribute: str | None = None


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
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, _Value(symbol=node.id))
        if isinstance(node, ast.Lambda):
            return _Value(symbol=f"lambda@{node.lineno}:{node.col_offset}")
        if isinstance(node, ast.Constant):
            return _Value(symbol=repr(node.value), text=node.value if isinstance(node.value, str) else "",
                          active_path=isinstance(node.value, str) and "workbench.sqlite" in node.value,
                          complete_text=isinstance(node.value, str))
        if isinstance(node, (ast.Tuple, ast.List)):
            return _Value(elements=tuple(self.value(item) for item in node.elts))
        if isinstance(node, ast.Attribute):
            base = self.value(node.value)
            symbol = base.symbol + "." + node.attr
            if node.attr == "__set__" and base.setter_attribute is not None:
                return replace(base, symbol=symbol)
            if base.symbol in {"sqlite3.Connection", "sqlite3.Cursor"} and node.attr in {"row_factory", "isolation_level", "autocommit"}:
                return _Value(symbol=symbol, setter_attribute=node.attr)
            if node.attr == "connection":
                return _Value(symbol=symbol, connection=base.connection or base.symbol,
                              fixture_handle=base.fixture_handle)
            if node.attr == "_lock":
                return _Value(symbol=symbol, lock_owner=base.symbol)
            if node.attr in _SQL_METHODS | _TRANSACTION_METHODS | _CALLBACK_METHODS | _MEDIA_SEED | {"cursor", "close"}:
                return _Value(symbol=symbol, connection=base.connection, fixture_handle=base.fixture_handle,
                              methods=frozenset({node.attr}), receiver=base,
                              descriptor=base.symbol in {"sqlite3.Connection", "sqlite3.Cursor"})
            return _Value(symbol=symbol, live_workspace=node.attr == "workspace", workspace=node.attr == "workspace",
                          active_path=node.attr == "path" and (base.live_workspace or base.symbol in self.workspace_symbols))
        if isinstance(node, ast.JoinedStr):
            parts = [self.value(part.value if isinstance(part, ast.FormattedValue) else part) for part in node.values]
            return _Value(text="".join(part.text or "?" for part in parts), active_path=any(part.active_path for part in parts),
                          complete_text=all(part.complete_text for part in parts))
        if isinstance(node, ast.BinOp):
            left, right = self.value(node.left), self.value(node.right)
            return _Value(symbol=left.symbol + "/" + right.symbol,
                          text=left.text + right.text, active_path=left.active_path or right.active_path,
                          complete_text=isinstance(node.op, ast.Add) and left.complete_text and right.complete_text)
        if isinstance(node, ast.NamedExpr):
            return self.value(node.value)
        if isinstance(node, ast.IfExp):
            return self.merge_aliases({"value": self.value(node.body)}, {"value": self.value(node.orelse)})["value"]
        if isinstance(node, ast.BoolOp):
            return self.merge_aliases(*({"value": self.value(value)} for value in node.values))["value"]
        if isinstance(node, ast.Call):
            function = self.value(node.func)
            if function.descriptor and node.args and function.methods & {"cursor", "execute", "executemany"}:
                return replace(self.value(node.args[0]), sqlite_cursor=True)
            if function.symbol == "getattr" and len(node.args) >= 2:
                name = self.value(node.args[1])
                if name.complete_text:
                    return self.value(ast.Attribute(value=node.args[0], attr=name.text, ctx=ast.Load()))
                receiver = self.value(node.args[0])
                if receiver.connection is not None:
                    # A dynamically selected callable on a known SQL connection
                    # must not discard ownership just because its name is unknown.
                    return _Value(connection=receiver.connection, fixture_handle=receiver.fixture_handle,
                                  methods=frozenset(_SQL_METHODS | _TRANSACTION_METHODS | _CALLBACK_METHODS | {"close"}), receiver=receiver)
            if function.connection is not None and function.symbol.rsplit(".", 1)[-1] in {"cursor", "execute", "executemany"}:
                return replace(function, symbol=function.symbol.rsplit(".", 1)[0], methods=frozenset(), receiver=None,
                               sqlite_cursor=True)
            if function.symbol in {"str", "Path", "pathlib.Path", "pathlib.PurePath", "os.fspath"} and node.args:
                return self.value(node.args[0])
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

    def flag(self, node: ast.AST, rule: str) -> None:
        statement = node
        while not isinstance(statement, ast.stmt) and statement in self.parents:
            if isinstance(statement, (ast.Lambda, ast.GeneratorExp)):
                break
            statement = self.parents[statement]
        if statement not in self.exempt:
            violation = Violation(self.filename, node.lineno, rule)
            if violation not in self.violations:
                self.violations.append(violation)

    def bind(self, target: ast.AST, value: _Value) -> None:
        if isinstance(target, ast.Name):
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
                self.bind(target.elts[index], _Value(elements=values[index:end]))
                for item, element in zip(target.elts[index + 1:], values[end:]):
                    self.bind(item, element)
            else:
                for item, element in zip(target.elts, values):
                    self.bind(item, element)
        elif isinstance(target, ast.Starred):
            self.bind(target.value, value)

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
            self.local_functions[symbol] = (kind, node, defaults, annotations, lexical.copy())

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
            values = [state.get(name, _Value(symbol=name)) for state in states]
            if all(value == values[0] for value in values):
                result[name] = values[0]
            elif all(value.elements is not None for value in values) and len({len(value.elements) for value in values}) == 1:
                result[name] = _Value(elements=tuple(
                    _Check.merge_aliases(*({"item": value.elements[index]} for value in values))["item"]
                    for index in range(len(values[0].elements))))
            elif all(value.connection is not None and value.connection.startswith("sqlite@") for value in values):
                result[name] = _Value(symbol=f"sqlite@merged:{name}", connection=f"sqlite@merged:{name}",
                                      sqlite_cursor=all(value.sqlite_cursor for value in values))
            else:
                receivers = [value.receiver for value in values if value.receiver is not None]
                same_owner = all(value.connection == values[0].connection for value in values)
                result[name] = _Value(symbol=values[0].symbol if all(value.symbol == values[0].symbol for value in values) else name,
                                      active_path=any(value.active_path for value in values),
                                      connection=(values[0].connection if same_owner else
                                                  f"unknown@merged:{name}" if any(value.connection is not None for value in values) else None),
                                      fixture_handle=same_owner and all(value.fixture_handle for value in values),
                                      live_workspace=any(value.live_workspace for value in values),
                                      workspace=any(value.workspace for value in values),
                                      unannotated_parameter=any(value.unannotated_parameter for value in values),
                                      descriptor=any(value.descriptor for value in values),
                                      setter_attribute=values[0].setter_attribute if all(value.setter_attribute == values[0].setter_attribute for value in values) else None,
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
            self.visit(case.pattern)
            for pattern in ast.walk(case.pattern):
                name = getattr(pattern, "name", None) or getattr(pattern, "rest", None)
                if name:
                    previous = self.aliases.get(name, _Value())
                    value = (subject if pattern is case.pattern and isinstance(pattern, ast.MatchAs)
                             else _Value(symbol=name))
                    if value.connection is None and previous.connection is not None:
                        value = replace(value, connection=f"unknown@pattern:{name}")
                    self.bind(ast.Name(id=name), value)
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

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        ends = []
        for expression in node.values:
            self.visit(expression)
            ends.append(self.aliases.copy())
        self.aliases = self.merge_aliases(*ends)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self.visit_loop(node, target=node.target)

    visit_AsyncFor = visit_For

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self.visit_loop(node)

    def visit_loop(self, node, target=None):
        before = self.aliases.copy()
        if target is not None:
            self.bind(target, _Value())
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
            self.bind(ast.Name(id=alias.asname or alias.name), _Value(symbol=alias.name))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            symbol = f"{node.module}.{alias.name}" if node.module else alias.name
            self.bind(ast.Name(id=alias.asname or alias.name), _Value(symbol=symbol, imported_function=True))

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        value = self.value(node.value)
        if isinstance(node.value, ast.Call) and self.value(node.value.func).symbol in {"WorkspaceStore", "case_intelligence.workspace_store.WorkspaceStore"}:
            value = _Value(symbol=f"workspace@{node.lineno}", workspace=True)
        for target in node.targets:
            self.check_transaction_assignment(target)
            self.bind(target, value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
            self.check_transaction_assignment(node.target)
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
        self.visit(node.value)
        self.check_transaction_assignment(node.target)
        self.bind(node.target, self.value(ast.BinOp(left=node.target, op=node.op, right=node.value)))

    def check_transaction_assignment(self, target: ast.AST) -> None:
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
            value = self.value(expression)
            is_helper = isinstance(expression, ast.Call) and self.value(expression.func).symbol in {"fixture_write", "tests.fixture_write.fixture_write"}
            self.visit(expression)
            if value.lock_owner is not None:
                self.held_locks.add(value.lock_owner)
            workspace_argument = (expression.args[0] if expression.args else next(
                (kw.value for kw in expression.keywords if kw.arg == "workspace"), None)) if is_helper else None
            if is_helper and workspace_argument is not None:
                owner = self.value(workspace_argument).symbol
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

    def visit_Call(self, node: ast.Call) -> None:
        function = self.value(node.func)
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
        arguments = node.args
        if function.descriptor and arguments:
            receiver, arguments = self.value(arguments[0]), arguments[1:]
        imported_function = isinstance(node.func, ast.Name) and function.imported_function and function.connection is None
        independent_connection = receiver.connection is not None and receiver.connection.startswith("sqlite@")
        if not independent_connection and not imported_function:
            if "close" in methods and receiver.connection is not None and not receiver.sqlite_cursor:
                self.flag(node, "shared connection close bypasses workspace lifetime")
            if methods & _CALLBACK_METHODS and (receiver.connection is not None or receiver.unannotated_parameter) and receiver.connection not in self.protected | self.held_locks:
                self.flag(node, "connection callback change requires the owning lock or fixture_write")
        if methods & _SQL_METHODS and not imported_function:
            argument = arguments[0] if arguments else next(
                (item.value for item in node.keywords if item.arg in {"sql", "sql_script"}), None)
            sql_value = self.value(argument)
            sql = sql_value.text
            # Strip quoted values/comments so SELECT 'update' is still a read.
            commands = re.sub(r"'(?:''|[^'])*'|--[^\n]*|/\*[\s\S]*?\*/", " ", sql)
            writes = _WRITE.search(commands) or re.search(r"^\s*PRAGMA\b[^;]*=", commands, re.I)
            pragma = _PRAGMA.match(re.sub(r"--[^\n]*|/\*[\s\S]*?\*/", " ", sql))
            pragma_name = pragma[1].strip("\"'`[]").lower() if pragma else ""
            if pragma and (pragma_name in _ACTION_PRAGMAS or (pragma[2] and pragma_name not in _READ_ARGUMENT_PRAGMAS)):
                # Parentheses also set values: user_version(12) is a write.
                # Keep argument-taking schema/integrity inspection read-only.
                writes = True
            if re.match(r"\s*WITH\b", commands, re.I):
                writes = writes or re.search(r"\b(?:INSERT|UPDATE|DELETE|REPLACE)\b", commands, re.I)
            independent_connection = receiver.connection is not None and receiver.connection.startswith("sqlite@")
            known_read = re.match(r"\s*(?:SELECT|EXPLAIN)\b", commands, re.I) or (sql_value.complete_text and re.match(r"\s*PRAGMA\b", commands, re.I))
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
        self.generic_visit(node)
        self.check_local_call(node, function)

    def check_local_call(self, call, function):
        # Specialize direct local helpers only when an actual SQLite handle or
        # bound method is supplied. Generic resource.close remains ordinary
        # cleanup, while aliases and helper chains retain shared ownership.
        if function.symbol not in self.local_functions or function.symbol in self.active_calls:
            return
        kind, node, defaults, annotations, lexical = self.local_functions[function.symbol]
        positional = [*node.args.posonlyargs, *node.args.args]
        supplied = {argument.arg: self.value(value) for argument, value in zip(positional, call.args)}
        supplied.update({keyword.arg: self.value(keyword.value) for keyword in call.keywords if keyword.arg is not None})
        if not any(value.connection is not None for value in supplied.values()):
            return
        bindings = {**defaults, **supplied}
        previous = self.aliases
        self.aliases = lexical.copy()
        self.active_calls.add(function.symbol)
        if kind == "function":
            self.analyze_function(node, bindings, annotations, invoked=True)
        else:
            self.analyze_lambda(node, bindings, invoked=True)
        self.active_calls.remove(function.symbol)
        self.aliases = previous

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
        self.defer("function", node, default_values, annotated)

    def analyze_function(self, node, default_values, annotated, *, invoked=False):
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
        self.flush_deferred()
        self.aliases, self.protected, self.class_outer_aliases = aliases, protected, class_outer
        self.held_locks, self.eager_walrus = held_locks, eager_walrus
        self.nested_protected = nested
        self.media_protected = media
        self.eager_variable_annotations = eager_annotations
        self.deferred, self.late_bindings, self.collect_bindings = outer_deferred, outer_bindings, collecting

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in [*node.decorator_list, *node.bases, *(keyword.value for keyword in node.keywords)]:
            self.visit(expression)
        aliases, class_outer = self.aliases, self.class_outer_aliases
        outer_names, self.class_outer_names = self.class_outer_names, set()
        eager_annotations = self.eager_variable_annotations
        self.eager_variable_annotations = True
        lexical = class_outer if class_outer is not None else aliases
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

    def visit_Lambda(self, node: ast.Lambda) -> None:
        positional = [*node.args.posonlyargs, *node.args.args]
        defaults = dict(zip((argument.arg for argument in positional[-len(node.args.defaults):]), node.args.defaults))
        defaults.update((argument.arg, default) for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults) if default is not None)
        for default in defaults.values():
            self.visit(default)
        default_values = {name: self.value(default) for name, default in defaults.items()}
        self.defer("lambda", node, default_values)

    def analyze_lambda(self, node, default_values, *, invoked=False):
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
        self.flush_deferred()
        self.aliases, self.protected, self.class_outer_aliases = aliases, protected, class_outer
        self.held_locks, self.eager_walrus = held_locks, eager_walrus
        self.nested_protected = nested
        self.media_protected = media
        self.deferred, self.late_bindings, self.collect_bindings = outer_deferred, outer_bindings, collecting

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
            elements = iterable.elements if iterable is not None else None
            value = self.merge_aliases(*({"item": element} for element in elements))["item"] if elements else _Value()
            self.bind(generator.target, value)
            self.comprehension_locals.update(item.id for item in ast.walk(generator.target) if isinstance(item, ast.Name))
            for condition in generator.ifs:
                self.visit(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
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
            self.bind(ast.Name(id=name), merged)
            if self.eager_walrus is not None:
                self.eager_walrus.setdefault(name, []).extend(values)


def check_fixture_writes(source: str, filename: str = "<fixture>") -> list[Violation]:
    """Return actionable boundary violations without importing the test module."""
    tree = ast.parse(source, filename=filename)
    checker = _Check(source, filename, tree)
    checker.visit(tree)
    return checker.violations
