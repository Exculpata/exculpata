#!/usr/bin/env python3
"""Reject operational legacy identifiers absent from the rename contract.

This additive working-tree check does not replace publication, secret, legacy
name, or documentation gates. Tests retain independent historical expectations.
"""
from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path
import re
from string import Formatter
import sys
from typing import NamedTuple
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from case_intelligence.legacy_names import (  # noqa: E402
    LEGACY_STEM, NAME_CONTRACTS, NameContract, contract_errors,
)
from case_intelligence import legacy_names as naming  # noqa: E402

KNOWN_VALUES = {key: item for key, item in vars(naming).items()
                if key.startswith(("LEGACY_", "CURRENT_")) and isinstance(item, str)}
STRING_METHODS = {"format", "format_map", "join", "replace", "lower", "upper", "casefold",
                  "capitalize", "title", "swapcase", "strip", "lstrip", "rstrip", "removeprefix", "removesuffix"}

SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", "artifacts", ".tmp", "tests",
}
# The existing independently reviewed guard contains literal matching rules,
# not operational names. Neither its implementation nor its policy is changed.
SKIP_FILES = {"CHANGELOG.md", "scripts/legacy_name_check.py"}
HISTORICAL_ASSET = f"{LEGACY_STEM}-banner.svg"

_STEM = re.escape(LEGACY_STEM)
_TOKENS = re.compile(
    rf"(?P<path>/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]*{_STEM}[A-Za-z0-9_.@/-]*)"
    rf"|(?P<version>\d[\w.+-]*{_STEM}[\w.+-]*)"
    rf"|(?P<browser>{_STEM}:(?:[A-Za-z][A-Za-z0-9_:-]*)?)"
    rf"|(?P<name>(?:[A-Za-z0-9_][A-Za-z0-9_-]*|\.+)?{_STEM}[A-Za-z0-9_.-]*)",
    re.IGNORECASE,
)


class Finding(NamedTuple):
    location: str
    identifier: str


def _patterns(contracts: Mapping[str, NameContract]) -> list[tuple[str, re.Pattern[str]]]:
    result = []
    for key, pair in contracts.items():
        parts = []
        for literal, field, _format, _conversion in Formatter().parse(pair.legacy):
            parts.append(re.escape(literal))
            if field is not None:
                if key == "environment":
                    parts.append(r"[A-Z0-9_]*")
                elif key == "header":
                    parts.append(r"[A-Za-z0-9-]*")
                elif key == "git_config":
                    parts.append(r"[A-Za-z0-9]*")
                else:
                    parts.append(r"[A-Za-z0-9_.:-]+")
        result.append((pair.legacy, re.compile("".join(parts))))
    return result


def _registered(token: str, patterns: list[tuple[str, re.Pattern[str]]]) -> bool:
    for template, pattern in patterns:
        if pattern.fullmatch(token):
            return True
        # A declared directory also owns its descendants, not sibling names.
        if template.startswith("/") and "{" not in template:
            if token.startswith(template.rstrip("/") + "/"):
                return True
        # Generated/template examples may show a known family's fixed prefix.
        if "{" in template and token == template.split("{", 1)[0]:
            return True
        # File references may be relative to a configuration/release directory;
        # the filename itself still has to be explicitly present in the table.
        if token.startswith("/") and not template.startswith("/"):
            leaf = token.rstrip("/").rsplit("/", 1)[-1].split("@", 1)[0]
            if ("." in template or "-" in template) and pattern.fullmatch(leaf):
                return True
    return False


def scan_text(location: str, text: str, contracts: Mapping[str, NameContract] = NAME_CONTRACTS) -> list[Finding]:
    patterns = _patterns(contracts)
    found = []
    for match in _TOKENS.finditer(text):
        start, end = match.span()
        # Python's ASCII-oriented operational patterns must not discard a
        # neighboring Unicode letter, number or combining mark. Keep those
        # characters in the token so a registered suffix cannot hide them.
        def identifier_character(character: str) -> bool:
            return character in "_-" or unicodedata.category(character)[0] in "LMN"

        if match.lastgroup != "path" and not match.group().startswith(".") and start and ord(text[start - 1]) > 127:
            while start and identifier_character(text[start - 1]):
                start -= 1
        while end < len(text) and identifier_character(text[end]):
            end += 1
        token = text[start:end]
        if token.rsplit("/", 1)[-1] == HISTORICAL_ASSET:
            continue
        if not any(_registered(candidate, patterns) for candidate in (token, token.rstrip("."), token.lstrip("."))):
            line = text.count("\n", 0, start) + 1
            finding = Finding(f"{location}:{line}", token)
            if finding not in found:
                found.append(finding)
    return found


def scan_component(location: str, component: str, contracts: Mapping[str, NameContract]) -> list[Finding]:
    if LEGACY_STEM.casefold() not in component.casefold() or component == HISTORICAL_ASSET:
        return []
    if _registered(component, _patterns(contracts)):
        return []
    return [Finding(f"{location}:1", component)]


def scan_python(location: str, text: str, contracts: Mapping[str, NameContract]) -> list[Finding]:
    """Inspect constant string compositions without importing or executing source."""
    try:
        tree = ast.parse(text.removeprefix("\ufeff"))
    except SyntaxError:
        return []  # Compilation is enforced separately.
    env = dict(KNOWN_VALUES)
    unresolved_stems: set[str] = set()
    binding_events: list[tuple[str, str | None]] = []
    found = []

    def joined(parts: list[str]) -> str | None:
        return "".join(parts) if sum(map(len, parts)) <= 65536 else None

    def unbind(name: str) -> None:
        for key in list(env):
            if key == name or key.startswith(name + "."):
                env.pop(key)
        unresolved_stems.discard(name)

    def value(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return env.get(node.id)
        if isinstance(node, ast.Attribute):
            return env.get(ast.unparse(node))
        if isinstance(node, ast.IfExp):
            choices = [value(node.body), value(node.orelse)]
            if choices[0] == choices[1]:
                return choices[0]
            legacy = [item for item in choices if item is not None and LEGACY_STEM in item.casefold()]
            if len(legacy) == 1:
                return legacy[0]  # Check the legacy branch of a generation choice.
        if isinstance(node, ast.BoolOp):
            operands = boolean_operands(node)
            choices = [value(item) for item in operands]
            legacy = {item for item in choices if item is not None and LEGACY_STEM in item.casefold()}
            if len(legacy) == 1 and not any(
                item is None and contains_stem(operand) for operand, item in zip(operands, choices)
            ):
                return legacy.pop()
        if isinstance(node, ast.JoinedStr):
            parts = []
            for part in node.values:
                if isinstance(part, ast.FormattedValue):
                    if part.conversion not in (-1, ord("s")) or part.format_spec is not None:
                        return None
                    # Unknown runtime values remain symbolic. Known literal
                    # fragments must still fit a declared contract family.
                    resolved = value(part.value)
                    if resolved is None:
                        if contains_stem(part.value):
                            return None
                        resolved = "CONTRACTDYNAMIC"
                else:
                    resolved = value(part)
                    if resolved is None:
                        return None
                parts.append(resolved)
            return joined(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = value(node.left), value(node.right)
            if left is not None and LEGACY_STEM in left.casefold() and right is None and not contains_stem(node.right):
                right = "CONTRACTDYNAMIC"
            if right is not None and LEGACY_STEM in right.casefold() and left is None and not contains_stem(node.left):
                left = "CONTRACTDYNAMIC"
            if left is not None and right is not None:
                return joined([left, right])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            template = value(node.left)
            args = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
            if template is not None:
                if re.search(r"%(?![%s])", template):
                    return None  # Do not interpret widths or arbitrary formatting.
                try:
                    values = [value(arg) for arg in args]
                    if any(resolved is None and contains_stem(arg) for arg, resolved in zip(args, values)):
                        return None
                    values = [arg if arg is not None else "CONTRACTDYNAMIC" for arg in values]
                    if joined([template, *values]) is None:
                        return None
                    return template % tuple(values)
                except (TypeError, ValueError):
                    return None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            template = value(node.func.value)
            if template is not None:
                args = [value(arg) for arg in node.args]
                kwargs = {arg.arg: value(arg.value) for arg in node.keywords if arg.arg}
                if any(resolved is None and contains_stem(arg) for arg, resolved in zip(node.args, args)):
                    return None
                if any(kwargs.get(arg.arg) is None and contains_stem(arg.value) for arg in node.keywords):
                    return None
                parts = []
                automatic = 0
                try:
                    for literal, field, spec, conversion in Formatter().parse(template):
                        parts.append(literal)
                        if field is None:
                            continue
                        if spec or conversion not in (None, "s"):
                            return None
                        if not field:
                            field = str(automatic)
                            automatic += 1
                        if field.isdecimal():
                            resolved = args[int(field)] if int(field) < len(args) else None
                        elif field.isidentifier():
                            resolved = kwargs.get(field)
                        else:
                            return None  # Never resolve attributes or indexing.
                        parts.append(resolved if resolved is not None else "CONTRACTDYNAMIC")
                except ValueError:
                    return None
                return joined(parts)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "str" and len(node.args) == 1 and not node.keywords:
                return value(node.args[0])
            if node.func.id == "name_value" and node.args:
                key = value(node.args[0])
                if key in contracts:
                    keywords = {arg.arg: value(arg.value) for arg in node.keywords if arg.arg}
                    names = value(node.args[1]) if len(node.args) > 1 else keywords.get("names", naming.CURRENT_STEM)
                    pair = contracts[key]
                    template = pair.current if names == naming.CURRENT_STEM else pair.legacy
                    parts = {field: (keywords[field] if keywords.get(field) is not None else "CONTRACTDYNAMIC")
                             for _, field, _, _ in Formatter().parse(template) if field}
                    return joined([template.format(**parts)])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            receiver = value(node.func.value)
            arguments = [value(arg) for arg in node.args]
            if receiver is not None and node.func.attr in STRING_METHODS - {"format", "format_map", "join"}:
                if not node.keywords and all(arg is not None for arg in arguments):
                    try:
                        if node.func.attr == "replace" and len(arguments) >= 2:
                            if len(receiver) + (len(receiver) + 1) * len(arguments[1]) > 65536:
                                return None
                        return joined([getattr(str, node.func.attr)(receiver, *arguments)])
                    except TypeError:
                        return None
            if ast.unparse(node.func) == "re.escape" and len(arguments) == 1 and arguments[0] is not None:
                return re.escape(arguments[0])
        return None

    def boolean_operands(node: ast.BoolOp) -> list[ast.AST]:
        possible = []
        for operand in node.values[:-1]:
            resolved = value(operand)
            if resolved is not None:
                stops = not resolved if isinstance(node.op, ast.And) else bool(resolved)
                if stops:
                    return [*possible, operand]
            else:
                possible.append(operand)
        return [*possible, node.values[-1]]

    def call_inputs(node: ast.Call) -> list[ast.AST]:
        """Only arguments contributing output text carry string provenance.

        Search patterns and characters removed by stripping do not contribute
        their contents to an unknown receiver. Reader/parser objects likewise
        do not become strings just because their inputs mention a name.
        """
        if isinstance(node.func, ast.Attribute):
            method = node.func.attr
            receiver = [node.func.value]
            if method == "join" and (separator := value(node.func.value)) and separator.isspace():
                return []  # A whitespace separator preserves identifier boundaries.
            if method in {"format", "format_map", "join"}:
                return [*receiver, *node.args, *(arg.value for arg in node.keywords)]
            if method == "get":
                return [*receiver, *node.args[1:2]]
            if method == "replace":
                return [*receiver, *node.args[1:2]]
            if method in STRING_METHODS:
                return receiver
        if isinstance(node.func, ast.Name) and node.func.id in {"str", "repr", "ascii", "name_value", "dict", "list", "tuple", "set"}:
            return [*node.args, *(arg.value for arg in node.keywords)]
        return []

    def contains_stem(piece: ast.AST) -> bool:
        if isinstance(piece, ast.Name) and piece.id in unresolved_stems:
            return True
        if isinstance(piece, ast.Dict):
            return any(contains_stem(item) for item in piece.values)
        if isinstance(piece, (ast.List, ast.Tuple, ast.Set)):
            return any(contains_stem(item) for item in piece.elts)
        if LEGACY_STEM in (value(piece) or "").casefold():
            return True
        if isinstance(piece, ast.Attribute):
            return contains_stem(piece.value)
        if isinstance(piece, ast.Subscript):
            return contains_stem(piece.value)
        if isinstance(piece, ast.Call):
            return any(contains_stem(item) for item in call_inputs(piece))
        if isinstance(piece, ast.IfExp):
            return contains_stem(piece.body) or contains_stem(piece.orelse)
        if isinstance(piece, ast.BoolOp):
            return any(contains_stem(item) for item in boolean_operands(piece))
        return False

    def bind(target: ast.AST, expression: ast.AST | None) -> None:
        # Resolve the complete right-hand side before updating any target,
        # matching Python's simultaneous tuple/list assignment semantics.
        pending = []

        def collect(item: ast.AST, source: ast.AST | None) -> None:
            if isinstance(item, ast.Name):
                resolved = value(source) if source is not None else None
                unresolved = source is not None and resolved is None and contains_stem(source)
                pending.append((item.id, resolved, unresolved))
            elif isinstance(item, (ast.Tuple, ast.List)):
                if isinstance(source, (ast.Tuple, ast.List)) and len(item.elts) == len(source.elts):
                    for child, child_source in zip(item.elts, source.elts):
                        collect(child, child_source)
                else:
                    for child in item.elts:
                        collect(child, source)

        collect(target, expression)
        for name, resolved, unresolved in pending:
            unbind(name)
            if resolved is not None:
                env[name] = resolved
                if LEGACY_STEM in resolved.casefold():
                    binding_events.append((name, resolved))
            elif unresolved:
                unresolved_stems.add(name)
                binding_events.append((name, None))

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.deferred = []

        def visit_Constant(self, node: ast.Constant) -> None:
            if isinstance(node.value, str):
                for item in scan_text(location, node.value, contracts):
                    _, line = item.location.rsplit(":", 1)
                    found.append(Finding(f"{location}:{node.lineno + int(line) - 1}", item.identifier))

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                if alias.name == "case_intelligence.legacy_names":
                    env.update({f"{alias.asname or alias.name}.{key}": item for key, item in KNOWN_VALUES.items()})
                    for key, item in KNOWN_VALUES.items():
                        if LEGACY_STEM in item.casefold():
                            binding_events.append((f"{alias.asname or alias.name}.{key}", item))

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.module and node.module.rsplit(".", 1)[-1] == "legacy_names":
                for alias in node.names:
                    if alias.name in KNOWN_VALUES:
                        bind(ast.Name(id=alias.asname or alias.name), ast.Constant(value=KNOWN_VALUES[alias.name]))
            elif node.module == "case_intelligence":
                for alias in node.names:
                    if alias.name == "legacy_names":
                        env.update({f"{alias.asname or alias.name}.{key}": item for key, item in KNOWN_VALUES.items()})
                        for key, item in KNOWN_VALUES.items():
                            if LEGACY_STEM in item.casefold():
                                binding_events.append((f"{alias.asname or alias.name}.{key}", item))

        def visit_Assign(self, node: ast.Assign) -> None:
            self.generic_visit(node)
            for target in node.targets:
                bind(target, node.value)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            self.generic_visit(node)
            bind(node.target, node.value)

        def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
            self.visit(node.iter)
            bind(node.target, None)
            for statement in (*node.body, *node.orelse):
                self.visit(statement)

        visit_AsyncFor = visit_For

        def visit_AugAssign(self, node: ast.AugAssign) -> None:
            expression = ast.copy_location(ast.BinOp(left=node.target, op=node.op, right=node.value), node)
            self.visit(expression)
            bind(node.target, expression)

        def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
            self.visit(node.value)
            bind(node.target, node.value)

        def visit_If(self, node: ast.If) -> None:
            nonlocal env, unresolved_stems
            self.visit(node.test)
            outer, outer_unresolved = dict(env), set(unresolved_stems)
            branches = []
            for statements in (node.body, node.orelse):
                env, unresolved_stems = dict(outer), set(outer_unresolved)
                for statement in statements:
                    self.visit(statement)
                branches.append((env, unresolved_stems))
            left, right = branches
            env = {key: item for key, item in left[0].items() if item == right[0].get(key)}
            unresolved_stems = left[1] | right[1]
            for key in left[0].keys() | right[0].keys():
                legacy = {bindings[key] for bindings, _ in branches
                          if key in bindings and LEGACY_STEM in bindings[key].casefold()}
                if key not in env and key not in unresolved_stems and len(legacy) == 1:
                    env[key] = legacy.pop()
                    unresolved_stems.discard(key)
                elif key not in env and legacy:
                    unresolved_stems.add(key)

        def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            for expression in (*node.decorator_list, *node.args.defaults, *node.args.kw_defaults):
                if expression is not None:
                    self.visit(expression)
            self.deferred.append((node, dict(env), set(unresolved_stems), len(binding_events)))

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, node: ast.Lambda) -> None:
            for expression in (*node.args.defaults, *node.args.kw_defaults):
                if expression is not None:
                    self.visit(expression)
            self.deferred.append((node, dict(env), set(unresolved_stems), len(binding_events)))

        def finish(self) -> None:
            nonlocal env, unresolved_stems, binding_events
            # A body can see bindings at its definition or later, including
            # values replaced after an earlier call. Bindings superseded before
            # the definition cannot reach it unless assigned again afterward.
            for node, definition_env, definition_unresolved, offset in self.deferred:
                outer, outer_unresolved, outer_events = env, unresolved_stems, binding_events
                env, unresolved_stems = dict(env), set(unresolved_stems)
                possible_bindings = {name: {item} for name, item in definition_env.items()
                                     if LEGACY_STEM in item.casefold()}
                possible_unresolved = set(definition_unresolved)
                for name, item in binding_events[offset:]:
                    if item is None:
                        possible_unresolved.add(name)
                    else:
                        possible_bindings.setdefault(name, set()).add(item)
                for name in possible_bindings.keys() | possible_unresolved:
                    possible = possible_bindings.get(name, set())
                    if len(possible) == 1 and name not in possible_unresolved:
                        env[name] = next(iter(possible))
                    else:
                        env.pop(name, None)
                        unresolved_stems.add(name)
                binding_events = []
                for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                    unbind(arg.arg)
                for arg in (node.args.vararg, node.args.kwarg):
                    if arg is not None:
                        unbind(arg.arg)
                visitor = Visitor()
                for statement in [node.body] if isinstance(node, ast.Lambda) else node.body:
                    visitor.visit(statement)
                visitor.finish()
                env = outer
                unresolved_stems = outer_unresolved
                binding_events = outer_events

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            nonlocal env, unresolved_stems, binding_events
            outer = env
            outer_unresolved = unresolved_stems
            outer_events = binding_events
            env = dict(env)
            unresolved_stems = set(unresolved_stems)
            binding_events = []
            self.generic_visit(node)
            env = outer
            unresolved_stems = outer_unresolved
            binding_events = outer_events

        def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
            self.composition(node)

        def visit_BinOp(self, node: ast.BinOp) -> None:
            self.composition(node)

        def visit_Call(self, node: ast.Call) -> None:
            self.composition(node)

        def composition(self, node: ast.AST) -> None:
            resolved = value(node)
            if resolved is not None:
                scan = scan_component if re.fullmatch(r"[A-Za-z0-9_.-]+", resolved) else scan_text
                for item in scan(location, resolved, contracts):
                    _, line = item.location.rsplit(":", 1)
                    found.append(Finding(f"{location}:{node.lineno + int(line) - 1}", item.identifier))
            else:
                pieces = []
                if isinstance(node, ast.JoinedStr):
                    pieces = [part.value if isinstance(part, ast.FormattedValue) else part for part in node.values]
                elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
                    # Whitespace around a serialized value cannot create a new
                    # identifier; its inner expressions are still inspected.
                    whitespace = isinstance(node.op, ast.Add) and any(
                        isinstance(part, ast.Constant) and isinstance(part.value, str) and not part.value.strip()
                        for part in (node.left, node.right)
                    )
                    if not whitespace:
                        pieces = [node.left, node.right]
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in STRING_METHODS:
                        pieces = call_inputs(node)
                if any(contains_stem(piece) for piece in pieces):
                    found.append(Finding(f"{location}:{node.lineno}", "unsupported legacy composition; use a contract key"))
                    return
                self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(tree)
    visitor.finish()
    return found


def findings(root: Path = ROOT, contracts: Mapping[str, NameContract] = NAME_CONTRACTS) -> list[Finding]:
    errors = contract_errors(contracts)
    if errors:
        return [Finding("src/case_intelligence/legacy_names.py", error) for error in errors]
    found = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in relative.parts):
            continue
        if relative.as_posix() in SKIP_FILES or relative.as_posix().startswith("docs/assets/"):
            continue
        if relative.as_posix() == "src/case_intelligence/legacy_names.py":
            continue  # The authority is validated above, rather than scanning itself.
        # rglob visits each directory and file, so every relative-path component
        # is checked once, independently of its descendants or content type.
        found.extend(scan_component(relative.as_posix(), path.name, contracts))
        if path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        found.extend(scan_text(relative.as_posix(), text, contracts))
        shebang = text.partition("\n")[0]
        if path.suffix == ".py" or not path.suffix or (shebang.startswith("#!") and re.search(r"\bpython(?:[23](?:\.\d+)?)?\b", shebang)):
            found.extend(scan_python(relative.as_posix(), text, contracts))
    return list(dict.fromkeys(found))


def main() -> int:
    found = findings()
    if found:
        print("RENAME CONTRACT GATE: BLOCKED", file=sys.stderr)
        for item in found:
            print(f"  {item.location}: {item.identifier}", file=sys.stderr)
        return 1
    print("RENAME CONTRACT GATE: CLEAN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
