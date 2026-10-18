"""Static tripwire for fixture mutations that can race live workspace workers.

This deliberately small, local data-flow check is not a Python/SQL interpreter.
It follows ordinary assignment aliases, SQL literals/concatenation/f-strings and
SQLite imports. Keep fixture setup explicit so that this check and reviewers can
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


class _Check(ast.NodeVisitor):
    def __init__(self, source: str, filename: str, tree: ast.AST):
        self.filename = filename
        self.aliases: dict[str, _Value] = {}
        self.class_outer_aliases: dict[str, _Value] | None = None
        self.class_outer_names: set[str] = set()
        self.eager_variable_annotations = True
        self.protected: set[str] = set()
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
        if isinstance(node, ast.Constant):
            return _Value(symbol=repr(node.value), text=node.value if isinstance(node.value, str) else "",
                          active_path=isinstance(node.value, str) and "workbench.sqlite" in node.value,
                          complete_text=isinstance(node.value, str))
        if isinstance(node, ast.Attribute):
            base = self.value(node.value)
            symbol = base.symbol + "." + node.attr
            if node.attr == "connection":
                return _Value(symbol=symbol, connection=base.connection or base.symbol,
                              fixture_handle=base.fixture_handle)
            if node.attr in _SQL_METHODS | _TRANSACTION_METHODS:
                return _Value(symbol=symbol, connection=base.connection, fixture_handle=base.fixture_handle)
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
            self.violations.append(Violation(self.filename, node.lineno, rule))

    def bind(self, target: ast.AST, value: _Value) -> None:
        if isinstance(target, ast.Name):
            self.aliases[target.id] = value
            if self.class_outer_aliases is not None and target.id in self.class_outer_names:
                self.class_outer_aliases[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self.bind(item, _Value())

    def declare_functions(self, body: list[ast.stmt]) -> None:
        # A local workflow helper named execute is inspected at its definition;
        # its callers are not connection methods just because of that spelling.
        for statement in body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.aliases[statement.name] = _Value(symbol=f"function@{statement.lineno}:{statement.name}")

    @staticmethod
    def merge_aliases(*states: dict[str, _Value]) -> dict[str, _Value]:
        result = {}
        for name in set().union(*(state.keys() for state in states)):
            values = [state.get(name, _Value(symbol=name)) for state in states]
            if all(value == values[0] for value in values):
                result[name] = values[0]
            elif all(value.connection is not None and value.connection.startswith("sqlite@") for value in values):
                result[name] = _Value(symbol=f"sqlite@merged:{name}", connection=f"sqlite@merged:{name}")
            else:
                result[name] = _Value(symbol=name, active_path=any(value.active_path for value in values),
                                      connection=f"unknown@merged:{name}" if any(value.connection is not None for value in values) else None)
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

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.aliases[alias.asname or alias.name] = _Value(symbol=alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            symbol = f"{node.module}.{alias.name}" if node.module else alias.name
            self.aliases[alias.asname or alias.name] = _Value(symbol=symbol, imported_function=True)

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
        self.bind(node.target, self.value(node.value))

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        self.check_transaction_assignment(node.target)
        self.bind(node.target, self.value(ast.BinOp(left=node.target, op=node.op, right=node.value)))

    def check_transaction_assignment(self, target: ast.AST) -> None:
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
        for item in node.items:
            expression = item.context_expr
            value = self.value(expression)
            is_helper = isinstance(expression, ast.Call) and self.value(expression.func).symbol in {"fixture_write", "tests.fixture_write.fixture_write"}
            self.visit(expression)
            workspace_argument = (expression.args[0] if expression.args else next(
                (kw.value for kw in expression.keywords if kw.arg == "workspace"), None)) if is_helper else None
            if is_helper and workspace_argument is not None:
                owner = self.value(workspace_argument).symbol
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

    visit_AsyncWith = visit_With

    def visit_Call(self, node: ast.Call) -> None:
        function = self.value(node.func)
        if function.symbol == "sqlite3.connect":
            argument = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg == "database"), None)
            path = self.value(argument)
            if path.active_path or "workbench.sqlite" in path.text:
                self.flag(node, "direct SQLite connection to workspace database")
        method = function.symbol.rsplit(".", 1)[-1]
        receiver = self.value(node.func.value) if isinstance(node.func, ast.Attribute) else function
        imported_function = isinstance(node.func, ast.Name) and function.imported_function and function.connection is None
        if method in _SQL_METHODS and not imported_function:
            argument = node.args[0] if node.args else next(
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
            unknown_write = (not known_read and not writes) or method == "executescript"
            # A literal DELETE/UPDATE is still a write when the callback's
            # parameter is unannotated or its table has no workbench_ prefix.
            if (unknown_write or (writes and (not independent_connection or _TABLE.search(sql)))) and receiver.connection not in self.protected:
                self.flag(node, "SQL fixture write outside fixture_write")
            if (unknown_write or _TRANSACTION.search(commands.lstrip(";\ufeff \t\r\n\v\f")) or _ROLLBACK_CONFLICT.search(commands)) and receiver.connection in self.protected and not receiver.fixture_handle:
                self.flag(node, "transaction control requires the yielded fixture_write handle")
        if method in _TRANSACTION_METHODS and not imported_function:
            # Product rollback APIs take domain arguments; a connection's
            # commit/rollback accepts none. Retain bound-method provenance too.
            connection_call = receiver.connection is not None or (not node.args and not node.keywords)
            independent_connection = receiver.connection is not None and receiver.connection.startswith("sqlite@")
            if connection_call and not independent_connection:
                if receiver.connection not in self.protected:
                    self.flag(node, "transaction termination outside fixture_write")
                elif not receiver.fixture_handle:
                    self.flag(node, "transaction control requires the yielded fixture_write handle")
        if isinstance(node.func, ast.Attribute):
            if node.func.attr in _MEDIA_SEED and receiver.live_workspace and receiver.symbol not in self.protected:
                self.flag(node, "manual media fixture seed outside fixture_write")
        self.generic_visit(node)

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
        aliases, protected, class_outer = self.aliases, self.protected, self.class_outer_aliases
        eager_annotations = self.eager_variable_annotations
        self.eager_variable_annotations = False
        self.aliases = (class_outer if class_outer is not None else aliases).copy()
        self.protected, self.class_outer_aliases = set(), None
        for argument in [*positional, *node.args.kwonlyargs, *([node.args.vararg] if node.args.vararg else []), *([node.args.kwarg] if node.args.kwarg else [])]:
            self.aliases.pop(argument.arg, None)
            if argument.arg in default_values:
                self.aliases[argument.arg] = default_values[argument.arg]
            elif annotated.get(argument.arg) == "sqlite3.Connection":
                self.aliases[argument.arg] = _Value(symbol=argument.arg, connection=argument.arg)
            elif annotated.get(argument.arg) in {"WorkspaceStore", "case_intelligence.workspace_store.WorkspaceStore"}:
                self.aliases[argument.arg] = _Value(symbol=argument.arg, workspace=True)
        self.declare_functions(node.body)
        for statement in node.body:
            self.visit(statement)
        self.aliases, self.protected, self.class_outer_aliases = aliases, protected, class_outer
        self.eager_variable_annotations = eager_annotations

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
        aliases, protected, class_outer = self.aliases, self.protected, self.class_outer_aliases
        self.aliases = (class_outer if class_outer is not None else aliases).copy()
        self.protected, self.class_outer_aliases = set(), None
        for argument in [*positional, *node.args.kwonlyargs, *([node.args.vararg] if node.args.vararg else []), *([node.args.kwarg] if node.args.kwarg else [])]:
            self.aliases[argument.arg] = default_values.get(argument.arg, _Value())
        self.visit(node.body)
        self.aliases, self.protected, self.class_outer_aliases = aliases, protected, class_outer

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self.visit_comprehension_scope(node, deferred=True)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self.visit_comprehension_scope(node, deferred=False)

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp

    def visit_comprehension_scope(self, node, *, deferred):
        # Python evaluates only the first iterable at generator creation. The
        # remaining clauses and result expression execute when it is consumed.
        # Eager comprehensions keep protection but bind their own loop names.
        self.visit(node.generators[0].iter)
        aliases, protected, class_outer = self.aliases, self.protected, self.class_outer_aliases
        self.aliases = (class_outer if class_outer is not None else aliases).copy()
        self.protected, self.class_outer_aliases = set() if deferred else protected.copy(), None
        for index, generator in enumerate(node.generators):
            if index:
                self.visit(generator.iter)
            self.bind(generator.target, _Value())
            for condition in generator.ifs:
                self.visit(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        self.aliases, self.protected, self.class_outer_aliases = aliases, protected, class_outer


def check_fixture_writes(source: str, filename: str = "<fixture>") -> list[Violation]:
    """Return actionable boundary violations without importing the test module."""
    tree = ast.parse(source, filename=filename)
    checker = _Check(source, filename, tree)
    checker.visit(tree)
    return checker.violations
