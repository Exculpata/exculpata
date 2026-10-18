#!/usr/bin/env python3
"""Reject operational legacy identifiers absent from the rename contract.

This additive working-tree check does not replace publication, secret, legacy
name, or documentation gates. Tests retain independent historical expectations.
"""
from __future__ import annotations

import ast
import hashlib
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
                  "encode", "decode", "capitalize", "title", "swapcase", "strip", "lstrip", "rstrip", "removeprefix", "removesuffix"}

SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", "artifacts", ".tmp", "tests",
}
# The existing independently reviewed guard contains literal matching rules,
# not operational names. Neither its implementation nor its policy is changed.
SKIP_FILES = {"CHANGELOG.md", "scripts/legacy_name_check.py"}
HISTORICAL_ASSET = f"{LEGACY_STEM}-banner.svg"
# This exact helper translates candidate identifiers for documentation analysis;
# it does not emit operational names. Only unresolved-composition diagnostics
# receive this disposition. Literal and resolved-name scanning still applies,
# and any AST change requires a new review rather than inheriting an exemption.
REVIEWED_ANALYSIS_FUNCTIONS = {
    ("scripts/doc_identifier_check.py", "_swap"): "75ce8138218b66706d732d21f538e1f7f5877331b2f332a63e973b18718d2d3b",
}

_STEM = re.escape(LEGACY_STEM)
_TOKENS = re.compile(
    rf"(?P<uri>(?={_STEM}:)[A-Za-z][A-Za-z0-9+.-]*://[A-Za-z0-9_.:/%&=+~!$,*@;-]*(?:[?#][A-Za-z0-9_.:/?%#&=+~!$,*@;()'-]*)?)"
    rf"|(?P<path>/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]*{_STEM}[A-Za-z0-9_.@/-]*)"
    rf"|(?P<version>\d[\w.+-]*{_STEM}[\w.+-]*)"
    rf"|(?P<browser>{_STEM}:(?:[A-Za-z][A-Za-z0-9_:-]*)?)"
    rf"|(?P<name>(?:[A-Za-z0-9_][A-Za-z0-9_-]*|\.+)?{_STEM}[A-Za-z0-9_.-]*)",
    re.IGNORECASE,
)


class UnknownLegacy(ast.expr):
    """Analysis-only unknown value with known legacy provenance."""
    _fields = ()


class ContainerValue(NamedTuple):
    kind: str
    items: tuple


def legacy_value(item: str | ContainerValue | None) -> bool:
    if isinstance(item, str):
        return LEGACY_STEM in item.casefold()
    if isinstance(item, ContainerValue):
        if item.kind == "unknown":
            return True
        return any(legacy_value(value) for value in item.items) if item.kind != "dict" else any(legacy_value(value) for _, value in item.items)
    return False


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


def _registered(token: str, patterns: list[tuple[str, re.Pattern[str]]], *, template_prefix: bool = False) -> bool:
    def known_descendants(parts: list[str], prefix: str = "") -> bool:
        for part in parts:
            prefix += "/" + part
            component, _, realm = part.partition("@")
            if LEGACY_STEM in part.casefold() and not any(
                pattern.fullmatch(component) or pattern.fullmatch(prefix) for _, pattern in patterns
            ):
                return False
            if LEGACY_STEM in realm.casefold() and not _registered(realm, patterns):
                return False
        return True

    for template, pattern in patterns:
        if pattern.fullmatch(token):
            return True
        # A declared directory also owns its descendants, not sibling names.
        if template.startswith("/") and "{" not in template:
            if token.startswith(template.rstrip("/") + "/"):
                if known_descendants(token[len(template.rstrip("/")) + 1:].split("/"), template.rstrip("/")):
                    return True
        # Generated/template examples may show a known family's fixed prefix.
        if template_prefix and "{" in template and token == template.split("{", 1)[0]:
            return True
        # File references may be relative to a configuration/release directory;
        # the filename itself still has to be explicitly present in the table.
        if token.startswith("/") and not template.startswith("/"):
            leaf = token.rstrip("/").rsplit("/", 1)[-1].split("@", 1)[0]
            if ("." in template or "-" in template) and pattern.fullmatch(leaf):
                if known_descendants(token.strip("/").split("/")):
                    return True
    return False


def scan_text(location: str, text: str, contracts: Mapping[str, NameContract] = NAME_CONTRACTS) -> list[Finding]:
    # Inspect the complete generated name for ordinary deployment, Python and
    # JavaScript placeholders; an unfilled fixed prefix alone is not a name.
    text = re.sub(r"@[A-Z][A-Z0-9_]*@|\$?\{[A-Za-z_]\w*\}", "CONTRACTDYNAMIC", text)
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
        if match.lastgroup == "uri" and start and text[start - 1] == "'" and token.endswith("'"):
            token = token[:-1]  # A source string's closing delimiter is not URI content.
        if token.rsplit("/", 1)[-1] == HISTORICAL_ASSET:
            continue
        documented_prefix = Path(location).suffix.lower() == ".md"
        if not any(_registered(candidate, patterns, template_prefix=documented_prefix)
                   for candidate in (token, token.rstrip("."), token.lstrip("."))):
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


def scan_python(location: str, text: str, contracts: Mapping[str, NameContract], *, tree: ast.Module | None = None) -> list[Finding]:
    """Inspect constant string compositions without importing or executing source."""
    try:
        tree = tree if tree is not None else ast.parse(text.removeprefix("\ufeff"))
    except SyntaxError:
        return []  # Compilation is enforced separately.
    env = dict(KNOWN_VALUES)
    unresolved_stems: set[str] = set()
    binding_events: list[tuple[str, str | ContainerValue | None]] = []
    found = []
    class_scopes = []
    function_locals = []
    module_scope = None

    def joined(parts: list[str]) -> str | None:
        return "".join(parts) if sum(map(len, parts)) <= 65536 else None

    def unbind(name: str) -> None:
        for key in list(env):
            if key == name or key.startswith((name + ".", name + "[")):
                env.pop(key)
        for key in list(unresolved_stems):
            if key == name or key.startswith((name + ".", name + "[")):
                unresolved_stems.discard(key)

    def frozen(node: ast.AST) -> str | ContainerValue | None:
        resolved = value(node)
        if resolved is not None:
            return resolved
        structured = structure(node)
        if structured is not None:
            return structured
        return ContainerValue("unknown", ()) if contains_stem(node) else None

    def structure(node: ast.AST) -> ContainerValue | None:
        if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)):
            stored = env.get(ast.unparse(node))
            if isinstance(stored, ContainerValue):
                return stored
            if isinstance(node, ast.Subscript):
                stored = member(node)
                return stored if isinstance(stored, ContainerValue) else None
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return ContainerValue(type(node).__name__.lower(), tuple(frozen(item) for item in node.elts))
        if isinstance(node, ast.Dict):
            return ContainerValue("dict", tuple((key.value if isinstance(key, ast.Constant) else None, frozen(item))
                                                for key, item in zip(node.keys, node.values)))
        return None

    def member(node: ast.Subscript) -> str | ContainerValue | None:
        source = structure(node.value)
        if source is None or not isinstance(node.slice, ast.Constant):
            return None
        key = node.slice.value
        if source.kind == "dict":
            return next((item for name, item in reversed(source.items) if name == key), None)
        if source.kind in {"list", "tuple"} and isinstance(key, int) and -len(source.items) <= key < len(source.items):
            return source.items[key]
        return None

    def literal(item: str | ContainerValue | None) -> ast.AST:
        if isinstance(item, ContainerValue):
            if item.kind == "dict":
                return ast.Dict(keys=[ast.Constant(value=key) for key, _ in item.items], values=[literal(value) for _, value in item.items])
            if item.kind == "unknown":
                return UnknownLegacy()
            return ast.List(elts=[literal(value) for value in item.items], ctx=ast.Load())
        return ast.Constant(value=item)

    def value(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                return node.value
            if isinstance(node.value, bytes):
                try:
                    return node.value.decode("utf-8")
                except UnicodeDecodeError:
                    return None
        if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)):
            stored = env.get(ast.unparse(node))
            if stored is None and isinstance(node, ast.Subscript):
                stored = member(node)
            return stored if isinstance(stored, str) else None
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
            if receiver is not None and node.func.attr in {"encode", "decode"}:
                encoding = arguments[0] if arguments else "utf-8"
                errors = arguments[1] if len(arguments) > 1 else "strict"
                keywords = {arg.arg: value(arg.value) for arg in node.keywords}
                encoding = keywords.get("encoding", encoding)
                errors = keywords.get("errors", errors)
                if (len(arguments) <= 2 and keywords.keys() <= {"encoding", "errors"}
                        and encoding in {"utf-8", "utf8", "UTF-8", "ascii"} and errors == "strict"):
                    if encoding != "ascii" or receiver.isascii():
                        return receiver
                return None
            if receiver is not None and node.func.attr in STRING_METHODS - {"format", "format_map", "join", "encode", "decode"}:
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
        if isinstance(piece, UnknownLegacy):
            return True
        if isinstance(piece, (ast.Name, ast.Attribute, ast.Subscript)):
            if ast.unparse(piece) in unresolved_stems or legacy_value(env.get(ast.unparse(piece))):
                return True
            if isinstance(piece, ast.Subscript) and legacy_value(member(piece)):
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
            if isinstance(item, (ast.Name, ast.Attribute, ast.Subscript)):
                resolved = frozen(source) if source is not None else None
                unresolved = source is not None and resolved is None and contains_stem(source)
                pending.append((item, resolved, unresolved))
            elif isinstance(item, ast.Starred):
                collect(item.value, source)
            elif isinstance(item, (ast.Tuple, ast.List)):
                structured = structure(source) if source is not None else None
                if structured is not None and structured.kind in {"list", "tuple"}:
                    source = literal(structured)
                if isinstance(source, (ast.Tuple, ast.List)):
                    star = next((index for index, child in enumerate(item.elts) if isinstance(child, ast.Starred)), None)
                    for index, child in enumerate(item.elts):
                        if isinstance(child, ast.Starred):
                            end = len(source.elts) - (len(item.elts) - index - 1)
                            collect(child.value, ast.List(elts=source.elts[index:end], ctx=ast.Load()))
                        else:
                            offset = index if star is None or index < star else index - len(item.elts)
                            collect(child, source.elts[offset] if -len(source.elts) <= offset < len(source.elts) else None)
                else:
                    for child in item.elts:
                        collect(child, source)

        def store(scope, unknown, events, target, resolved, unresolved):
            name = ast.unparse(target)
            if isinstance(target, ast.Subscript):
                parent = ast.unparse(target.value)
                original = scope.get(parent)
                if isinstance(original, ContainerValue):
                    updated = ContainerValue("unknown", ()) if legacy_value(original) or legacy_value(resolved) or unresolved else None
                    if isinstance(target.slice, ast.Constant):
                        key = target.slice.value
                        if original.kind == "dict":
                            updated = ContainerValue("dict", (*((key_, item) for key_, item in original.items if key_ != key), (key, resolved)))
                        elif original.kind == "list" and isinstance(key, int) and -len(original.items) <= key < len(original.items):
                            items = list(original.items)
                            items[key] = resolved
                            updated = ContainerValue("list", tuple(items))
                    aliases = [key for key, item in scope.items() if item is original]
                    for alias in aliases:
                        for key in list(scope):
                            if key.startswith(alias + "["):
                                scope.pop(key)
                        if updated is None:
                            scope.pop(alias, None)
                        else:
                            scope[alias] = updated
                            if legacy_value(updated):
                                events.append((alias, updated))
                elif legacy_value(resolved) or unresolved:
                    unknown.add(parent)
                    events.append((parent, None))
            for key in list(scope):
                if key == name or key.startswith((name + ".", name + "[")):
                    scope.pop(key)
            for key in list(unknown):
                if key == name or key.startswith((name + ".", name + "[")):
                    unknown.discard(key)
            if resolved is not None:
                scope[name] = resolved
                if legacy_value(resolved):
                    events.append((name, resolved))
            elif unresolved:
                unknown.add(name)
                events.append((name, None))

        collect(target, expression)
        for target, resolved, unresolved in pending:
            name = ast.unparse(target)
            if class_scopes:
                outer_env, outer_unresolved, outer_events, local_names, declared_outer = class_scopes[-1]
                root = re.split(r"[.\[]", name, maxsplit=1)[0]
                if isinstance(target, ast.Name) and root not in declared_outer:
                    local_names.add(root)
                elif root not in local_names:
                    if declared_outer.get(root) == "global" and module_scope is not None:
                        store(*module_scope, target, resolved, unresolved)
                        # A method still closes over a same-named function
                        # local; a class global declaration cannot change it.
                        if not function_locals or root not in function_locals[-1]:
                            store(outer_env, outer_unresolved, outer_events, target, resolved, unresolved)
                    else:
                        store(outer_env, outer_unresolved, outer_events, target, resolved, unresolved)
            store(env, unresolved_stems, binding_events, target, resolved, unresolved)

    class Visitor(ast.NodeVisitor):
        def __init__(self, *, scope=(), reviewed_analysis=False) -> None:
            self.deferred = []
            self.method_scope = None
            self.scope = scope
            self.reviewed_analysis = reviewed_analysis

        def visit_Constant(self, node: ast.Constant) -> None:
            decoded = node.value
            if isinstance(decoded, bytes):
                try:
                    decoded = decoded.decode("utf-8")
                except UnicodeDecodeError:
                    return
            if isinstance(decoded, str):
                for item in scan_text(location, decoded, contracts):
                    _, line = item.location.rsplit(":", 1)
                    found.append(Finding(f"{location}:{node.lineno + int(line) - 1}", item.identifier))

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                if class_scopes:
                    class_scopes[-1][3].add(alias.asname or alias.name.split(".")[0])
                if alias.name == "case_intelligence.legacy_names":
                    env.update({f"{alias.asname or alias.name}.{key}": item for key, item in KNOWN_VALUES.items()})
                    for key, item in KNOWN_VALUES.items():
                        if LEGACY_STEM in item.casefold():
                            binding_events.append((f"{alias.asname or alias.name}.{key}", item))

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if class_scopes:
                class_scopes[-1][3].update(alias.asname or alias.name for alias in node.names)
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
            def joined_values(items):
                candidates = set(items)
                if len(candidates) == 1:
                    return next(iter(candidates))
                containers = [item for item in candidates if isinstance(item, ContainerValue)]
                if containers and len(containers) == len(candidates) and all(
                    item.kind in {"list", "tuple"} and len(item.items) == len(containers[0].items) for item in containers
                ):
                    return ContainerValue("tuple", tuple(joined_values([item.items[index] for item in containers])
                                                         for index in range(len(containers[0].items))))
                legacy = {item for item in candidates if legacy_value(item)}
                return next(iter(legacy)) if len(legacy) == 1 else ContainerValue("unknown", ()) if legacy else None

            iterable = structure(node.iter)
            possibilities = [iterable] if iterable is not None else []
            if isinstance(node.iter, ast.Subscript) and not isinstance(node.iter.slice, ast.Constant):
                parent = structure(node.iter.value)
                if parent is not None:
                    possibilities = [item for _, item in parent.items] if parent.kind == "dict" else list(parent.items)
            members = []
            for possible in possibilities:
                if isinstance(possible, ContainerValue) and possible.kind in {"dict", "list", "tuple", "set"}:
                    members.extend(key for key, _ in possible.items) if possible.kind == "dict" else members.extend(possible.items)
                elif legacy_value(possible):
                    members.append(ContainerValue("unknown", ()))
            iteration = joined_values(members) if members else ContainerValue("unknown", ()) if contains_stem(node.iter) else None
            assignment = ast.Assign(targets=[node.target], value=literal(iteration))
            ast.fix_missing_locations(ast.copy_location(assignment, node))
            self.alternatives([[], [assignment, *node.body]])
            # A break can bypass the else suite; retain both possibilities.
            self.alternatives([[], node.orelse])

        visit_AsyncFor = visit_For

        def visit_While(self, node: ast.While) -> None:
            self.visit(node.test)
            self.alternatives([[], node.body])
            self.alternatives([[], node.orelse])

        def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
            for item in node.items:
                self.visit(item.context_expr)
                if item.optional_vars is not None:
                    bind(item.optional_vars, None)
            for statement in node.body:
                self.visit(statement)

        visit_AsyncWith = visit_With

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.type is not None:
                self.visit(node.type)
            if node.name:
                bind(ast.Name(id=node.name, ctx=ast.Store()), None)
            for statement in node.body:
                self.visit(statement)
            if node.name:
                unbind(node.name)
                if class_scopes:
                    class_scopes[-1][3].discard(node.name)

        def visit_Try(self, node: ast.Try | ast.TryStar) -> None:
            # Visit each statement once. An exception can occur before or after
            # any statement; merge those inputs for the handler paths while
            # preserving the precise successful body result for the else path.
            exceptional = [self.snapshot()]
            for statement in node.body:
                self.visit(statement)
                exceptional.append(self.snapshot())
            normal = self.snapshot()
            self.merge_states(exceptional)
            handler_input = self.snapshot()
            if isinstance(node, ast.TryStar):
                # Exception-group handlers can run in order on disjoint
                # subgroups, so each later handler sees earlier possible writes.
                for handler in node.handlers:
                    self.alternatives([[], [handler]])
                self.alternatives([node.orelse, []], starts=[normal, self.snapshot()])
            else:
                self.alternatives([node.orelse, *[[handler] for handler in node.handlers]],
                                  starts=[normal, *[handler_input for _ in node.handlers]])
            for statement in node.finalbody:
                self.visit(statement)

        visit_TryStar = visit_Try

        def visit_AugAssign(self, node: ast.AugAssign) -> None:
            expression = ast.copy_location(ast.BinOp(left=node.target, op=node.op, right=node.value), node)
            self.visit(expression)
            bind(node.target, expression)

        def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
            self.visit(node.value)
            bind(node.target, node.value)

        def visit_If(self, node: ast.If) -> None:
            self.visit(node.test)
            self.alternatives([node.body, node.orelse])

        def visit_Match(self, node: ast.Match) -> None:
            self.visit(node.subject)

            def captures(pattern: ast.pattern, subject: ast.AST) -> list[ast.Assign]:
                structured = structure(subject)
                if structured is not None:
                    subject = literal(structured)
                result = []
                def capture(name: str | None, source: ast.AST) -> None:
                    if name:
                        result.append(ast.Assign(targets=[ast.Name(id=name, ctx=ast.Store())], value=source))
                if isinstance(pattern, ast.MatchAs):
                    capture(pattern.name, subject)
                    if pattern.pattern:
                        result.extend(captures(pattern.pattern, subject))
                elif isinstance(pattern, ast.MatchStar):
                    capture(pattern.name, subject)
                elif isinstance(pattern, ast.MatchMapping):
                    remaining = list(zip(subject.keys, subject.values)) if isinstance(subject, ast.Dict) else []
                    for key, child in zip(pattern.keys, pattern.patterns):
                        source = ast.Subscript(value=subject, slice=key, ctx=ast.Load())
                        for pair in remaining:
                            if pair[0] is not None and ast.dump(pair[0]) == ast.dump(key):
                                source = pair[1]
                                remaining.remove(pair)
                                break
                        result.extend(captures(child, source))
                    rest = ast.Dict(keys=[key for key, _ in remaining], values=[item for _, item in remaining]) if isinstance(subject, ast.Dict) else subject
                    capture(pattern.rest, rest)
                elif isinstance(pattern, ast.MatchSequence):
                    items = subject.elts if isinstance(subject, (ast.List, ast.Tuple)) else None
                    star = next((index for index, child in enumerate(pattern.patterns) if isinstance(child, ast.MatchStar)), None)
                    for index, child in enumerate(pattern.patterns):
                        source = subject
                        if items is not None:
                            if isinstance(child, ast.MatchStar):
                                end = len(items) - (len(pattern.patterns) - index - 1)
                                source = ast.List(elts=items[index:end], ctx=ast.Load())
                            else:
                                offset = index if star is None or index < star else index - len(pattern.patterns)
                                if -len(items) <= offset < len(items):
                                    source = items[offset]
                        result.extend(captures(child, source))
                elif isinstance(pattern, ast.MatchClass):
                    for child in pattern.patterns:
                        result.extend(captures(child, subject))
                    for attribute, child in zip(pattern.kwd_attrs, pattern.kwd_patterns):
                        result.extend(captures(child, ast.Attribute(value=subject, attr=attribute, ctx=ast.Load())))
                elif isinstance(pattern, ast.MatchOr):
                    by_name = {}
                    for option in pattern.patterns:
                        for assignment in captures(option, subject):
                            name = assignment.targets[0].id
                            previous = by_name.get(name)
                            by_name[name] = assignment.value if previous is None else ast.IfExp(test=ast.Constant(value=None), body=previous, orelse=assignment.value)
                    for name, source in by_name.items():
                        capture(name, source)
                return result

            choices = []
            exhaustive = False
            for case in node.cases:
                # Captures bind matched values, including structural members;
                # they are not unknown values that erase existing provenance.
                bindings = captures(case.pattern, node.subject)
                # Evaluate all capture sources before binding any name: one
                # capture may shadow a name used to construct the subject.
                assignment = ast.Assign(
                    targets=[ast.Tuple(elts=[item.targets[0] for item in bindings], ctx=ast.Store())],
                    value=ast.Tuple(elts=[item.value for item in bindings], ctx=ast.Load()),
                )
                ast.fix_missing_locations(ast.copy_location(assignment, node.subject))
                choices.append([case.pattern, assignment, *([case.guard] if case.guard else []), *case.body])
                exhaustive |= isinstance(case.pattern, ast.MatchAs) and case.pattern.pattern is None and case.guard is None
            self.alternatives(choices if exhaustive else [*choices, []])

        def snapshot(self):
            enclosing = None
            if class_scopes:
                scope, unknown, _, local_names, _ = class_scopes[-1]
                enclosing = dict(scope), set(unknown), set(local_names)
            return dict(env), set(unresolved_stems), enclosing

        def restore(self, state) -> None:
            nonlocal env, unresolved_stems
            env, unresolved_stems = dict(state[0]), set(state[1])
            if class_scopes:
                scope, unknown, _, local_names, _ = class_scopes[-1]
                scope.clear()
                scope.update(state[2][0])
                unknown.clear()
                unknown.update(state[2][1])
                local_names.clear()
                local_names.update(state[2][2])

        def merge_states(self, states) -> None:
            def merged(branches):
                bindings = {key: item for key, item in branches[0][0].items()
                            if all(item == scope.get(key) for scope, _ in branches[1:])}
                unresolved = set().union(*(unknown for _, unknown in branches))
                for key in set().union(*(scope.keys() for scope, _ in branches)):
                    legacy = {scope[key] for scope, _ in branches if key in scope and legacy_value(scope[key])}
                    if key not in bindings and key not in unresolved and len(legacy) == 1:
                        bindings[key] = legacy.pop()
                    elif key not in bindings and legacy:
                        unresolved.add(key)
                return bindings, unresolved

            bindings, unknown = merged([(state[0], state[1]) for state in states])
            enclosing = None
            if class_scopes:
                scope, uncertain = merged([(state[2][0], state[2][1]) for state in states])
                # A class root is definitely local only if every path binds it.
                locals_ = set.intersection(*(state[2][2] for state in states))
                enclosing = scope, uncertain, locals_
            self.restore((bindings, unknown, enclosing))

        def alternatives(self, choices: list[list[ast.AST]], *, starts=None) -> None:
            initial = self.snapshot()
            results = []
            for index, statements in enumerate(choices):
                self.restore(starts[index] if starts is not None else initial)
                for statement in statements:
                    self.visit(statement)
                results.append(self.snapshot())
            self.merge_states(results)

        def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            for expression in (*node.decorator_list, *node.args.defaults, *node.args.kw_defaults):
                if expression is not None:
                    self.visit(expression)
            scope_env, scope_unresolved, scope_events = self.method_scope or (env, unresolved_stems, binding_events)
            self.deferred.append((node, dict(scope_env), set(scope_unresolved), len(scope_events)))
            if class_scopes:
                class_scopes[-1][3].add(node.name)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, node: ast.Lambda) -> None:
            for expression in (*node.args.defaults, *node.args.kw_defaults):
                if expression is not None:
                    self.visit(expression)
            scope_env, scope_unresolved, scope_events = self.method_scope or (env, unresolved_stems, binding_events)
            self.deferred.append((node, dict(scope_env), set(scope_unresolved), len(scope_events)))

        def finish(self) -> None:
            nonlocal env, unresolved_stems, binding_events
            # A body can see bindings at its definition or later, including
            # values replaced after an earlier call. Bindings superseded before
            # the definition cannot reach it unless assigned again afterward.
            for node, definition_env, definition_unresolved, offset in self.deferred:
                outer, outer_unresolved, outer_events = env, unresolved_stems, binding_events
                env, unresolved_stems = dict(env), set(unresolved_stems)
                possible_bindings = {name: {item} for name, item in definition_env.items()
                                     if legacy_value(item)}
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
                local_names = set()
                def locals_in(statement):
                    if isinstance(statement, ast.Name) and isinstance(statement.ctx, ast.Store):
                        local_names.add(statement.id)
                    elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        local_names.add(statement.name)
                    elif isinstance(statement, (ast.Import, ast.ImportFrom)):
                        local_names.update(alias.asname or alias.name.split(".")[0] for alias in statement.names)
                    elif not isinstance(statement, ast.Lambda):
                        for child in ast.iter_child_nodes(statement):
                            locals_in(child)
                for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                    local_names.add(arg.arg)
                for statement in [node.body] if isinstance(node, ast.Lambda) else node.body:
                    locals_in(statement)
                function_locals.append(local_names)
                name = getattr(node, "name", "<lambda>")
                expected = REVIEWED_ANALYSIS_FUNCTIONS.get((location, name)) if not self.scope else None
                reviewed = expected is not None and hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest() == expected
                visitor = Visitor(scope=(*self.scope, name), reviewed_analysis=reviewed)
                for statement in [node.body] if isinstance(node, ast.Lambda) else node.body:
                    visitor.visit(statement)
                visitor.finish()
                function_locals.pop()
                env = outer
                unresolved_stems = outer_unresolved
                binding_events = outer_events

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            nonlocal env, unresolved_stems, binding_events
            outer = env
            outer_unresolved = unresolved_stems
            outer_events = binding_events
            outer_method_scope = self.method_scope
            self.method_scope = self.method_scope or (outer, outer_unresolved, outer_events)
            scope_env, scope_unresolved, scope_events = self.method_scope
            env = dict(scope_env)
            unresolved_stems = set(scope_unresolved)
            binding_events = []
            declared_outer = {}
            def declarations(statement):
                if isinstance(statement, (ast.Global, ast.Nonlocal)):
                    declared_outer.update({name: "global" if isinstance(statement, ast.Global) else "nonlocal" for name in statement.names})
                elif not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    for child in ast.iter_child_nodes(statement):
                        declarations(child)
            for statement in node.body:
                declarations(statement)
            class_scopes.append((scope_env, scope_unresolved, scope_events, set(), declared_outer))
            self.generic_visit(node)
            class_scopes.pop()
            env = outer
            unresolved_stems = outer_unresolved
            binding_events = outer_events
            self.method_scope = outer_method_scope
            if class_scopes:
                class_scopes[-1][3].add(node.name)

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
                if any(contains_stem(piece) for piece in pieces) and not self.reviewed_analysis:
                    found.append(Finding(f"{location}:{node.lineno}", "unsupported legacy composition; use a contract key"))
                    return
                self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(tree)
    module_scope = env, unresolved_stems, binding_events
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
        authority = None
        if relative.as_posix() == "src/case_intelligence/legacy_names.py":
            # The table is validated above; every other declaration in its
            # module remains subject to the same operational-name checks.
            text = text.removeprefix("\ufeff")
            lines = [bytearray(line.encode("utf-8")) for line in text.splitlines(keepends=True)]
            try:
                authority = ast.parse(text.removeprefix("\ufeff"))
            except SyntaxError:
                authority = ast.Module(body=[], type_ignores=[])
            remaining = []
            for statement in authority.body:
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target] if isinstance(statement, ast.AnnAssign) else []
                if len(targets) == 1 and isinstance(targets[0], ast.Name) and targets[0].id == "NAME_CONTRACTS":
                    # AST columns count UTF-8 bytes. Preserve other statements
                    # and comments even when they share a table boundary line.
                    for index in range(statement.lineno - 1, statement.end_lineno):
                        start = statement.col_offset if index == statement.lineno - 1 else 0
                        end = statement.end_col_offset if index == statement.end_lineno - 1 else len(lines[index])
                        for column in range(start, end):
                            if lines[index][column] not in (10, 13):
                                lines[index][column] = 32
                else:
                    remaining.append(statement)
            authority.body = remaining
            text = b"".join(lines).decode("utf-8")
        found.extend(scan_text(relative.as_posix(), text, contracts))
        shebang = text.removeprefix("\ufeff").partition("\n")[0]
        if path.suffix == ".py" or not path.suffix or (shebang.startswith("#!") and re.search(r"\bpython(?:[23](?:\.\d+)?)?\b", shebang)):
            found.extend(scan_python(relative.as_posix(), text, contracts, tree=authority))
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
