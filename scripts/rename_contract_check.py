#!/usr/bin/env python3
"""Reject operational legacy identifiers absent from the rename contract.

This additive working-tree check does not replace publication, secret, legacy
name, or documentation gates. Tests retain independent historical expectations.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
import re
from string import Formatter
import sys
import textwrap
from typing import NamedTuple
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from case_intelligence.legacy_names import (  # noqa: E402
    LEGACY_STEM, NAME_CONTRACTS, NameContract, contract_errors,
)
from case_intelligence import legacy_names as naming  # noqa: E402

MAX_ANALYSIS_STEPS = 500000


class AnalysisBudgetExceeded(Exception):
    pass


KNOWN_VALUES = {key: item for key, item in vars(naming).items()
                if key.startswith(("LEGACY_", "CURRENT_")) and isinstance(item, str)}
# Reviewed selector interface: environment keys select values; they are not
# returned as data. The complete implementation is pinned so an edited helper
# cannot silently inherit these input/output roles.
SELECTOR_MODULE_SHA256 = "90e216265b2f17b2175b03ac6d4fa7cafeed2c2d2a2cf3b6fe1fde9e436cc8a1"
SELECTOR_CALLS = {"case_intelligence.compat_env.env", "case_intelligence.compat_env.required_env"}

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
# and any source change requires a new review rather than inheriting an exemption.
REVIEWED_ANALYSIS_FUNCTIONS = {
    ("scripts/doc_identifier_check.py", "_swap"): "3ece11ba458657b335b958d6e75561208da21988a39e9df5c331eba5a38d433e",
}

_STEM = re.escape(LEGACY_STEM)
_TOKENS = re.compile(
    rf"(?P<uri>(?={_STEM}:)[A-Za-z][A-Za-z0-9+.-]*://[A-Za-z0-9_.:/%&=+~!$,*@;-]*(?:[?#][A-Za-z0-9_.:/?%#&=+~!$,*@;()'-]*)?)"
    rf"|(?P<path>/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]*{_STEM}[A-Za-z0-9_.@/+:%-]*)"
    rf"|(?P<version>\d[\w.+-]*{_STEM}[\w.+-]*)"
    rf"|(?P<browser>{_STEM}:(?:[A-Za-z][A-Za-z0-9_:-]*)?)"
    rf"|(?P<name>(?:[A-Za-z0-9_][A-Za-z0-9_-]*|\.+)?{_STEM}[A-Za-z0-9_.@+%/-]*)",
    re.IGNORECASE,
)


class UnknownLegacy(ast.expr):
    """Analysis-only unknown value with known legacy provenance."""
    _fields = ()


class FrozenDomainValue(ast.expr):
    """Keep a verified immutable domain value across analysis-only bindings."""
    _fields = ()

    def __init__(self, item):
        self.item = item


class ContainerValue(NamedTuple):
    kind: str
    items: tuple


def legacy_value(item: str | ContainerValue | None) -> bool:
    if isinstance(item, str):
        return LEGACY_STEM in item.casefold()
    if isinstance(item, ContainerValue):
        if item.kind in {"module", "callable"}:
            return False
        if item.kind == "unknown":
            return True
        return any(legacy_value(value) for value in item.items) if item.kind != "dict" else any(legacy_value(key) or legacy_value(value) for key, value in item.items)
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


def scan_text(location: str, text: str, contracts: Mapping[str, NameContract] = NAME_CONTRACTS, *, literal: bool = False) -> list[Finding]:
    # Inspect the complete generated name for ordinary deployment, Python and
    # JavaScript placeholders; an unfilled fixed prefix alone is not a name.
    found = []
    placeholder_pattern = r"@[A-Z][A-Z0-9_]*@|\$?\{[A-Za-z_]\w*\}"
    shell_source = Path(location).suffix == ".sh" or Path(location).name.endswith(".env") or bool(re.match(r"#![^\n]*\b(?:ba|da|k|z)?sh\b", text))
    if shell_source:
        placeholder_pattern += r"|\$[A-Za-z_]\w*"
    removal_pattern = placeholder_pattern + r"|[\"']" if shell_source else placeholder_pattern
    cuts, removed = set(), 0
    for placeholder in re.finditer(removal_pattern, text):
        cuts.add(placeholder.start() - removed)
        removed += len(placeholder.group())
    empty = re.sub(removal_pattern, "", text)
    if any(offset in cuts for stem in re.finditer(re.escape(LEGACY_STEM), empty, re.IGNORECASE)
           for offset in range(stem.start() + 1, stem.end())):
        found.extend(scan_text(location, empty, contracts, literal=literal))
    def mask_placeholder(match):
        identifier = match.group().strip("@$ {}")
        if LEGACY_STEM in identifier.casefold():
            offset = text.count("\n", 0, match.start())
            for item in scan_text(location, identifier, contracts, literal=True):
                found.append(Finding(f"{location}:{offset + 1}", item.identifier))
        return "CONTRACTDYNAMIC"
    text = re.sub(placeholder_pattern, mask_placeholder, text)
    patterns = _patterns(contracts)
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
        if Path(location).name == ".gitignore" and not literal:
            token = token.rstrip("/")
        documented_prefix = Path(location).suffix.lower() == ".md"
        line_start = text.rfind("\n", 0, start) + 1
        prose = documented_prefix or (not literal and text[line_start:start].lstrip().startswith(("#", "//")))
        if not literal and token.startswith(".") and start and (text[start - 1].isalnum() or text[start - 1] in "_?])"):
            token = token[1:]  # Source member-access syntax; decoded literals stay exact.
        if (not literal and Path(location).suffix in {".yaml", ".yml"}
                and token.startswith("/") and token.endswith((":ro", ":rw"))
                and (text[line_start:start].rstrip().endswith(("}:", "CONTRACTDYNAMIC:")) or
                     (text[line_start:start].strip() == "-" and text[end:].startswith(",noexec,nosuid,nodev,")))):
            token = token.rsplit(":", 1)[0]  # Explicit interpolated-host Compose mount.
        if not any(_registered(candidate, patterns, template_prefix=documented_prefix)
                   for candidate in ((token, token.rstrip("."), token.lstrip(".")) if prose else (token,))):
            line = text.count("\n", 0, start) + 1
            finding = Finding(f"{location}:{line}", token)
            if finding not in found:
                found.append(finding)
    return found


def scan_javascript(location: str, text: str, contracts) -> list[Finding]:
    """Decode bounded quoted/template string escapes without executing source."""
    literals = re.compile(r"//[^\n]*|/\*[\s\S]*?\*/|(?P<quote>['\"`])(?P<body>(?:\\[\s\S]|(?!(?P=quote))[\s\S])*)(?P=quote)")
    escapes = re.compile(r"\\(?:[0-7]{1,3}|x[0-9a-fA-F]{2}|u\{[0-9a-fA-F]{1,6}\}|u[0-9a-fA-F]{4}|\r\n|[\s\S])")
    def decode(match):
        token = match.group()[1:]
        if token.isdigit() and token != "0" and token[0] in "01234567":
            raise ValueError("unsupported legacy octal escape")
        if token.startswith("x") and len(token) == 3:
            return chr(int(token[1:], 16))
        if token.startswith("u") and len(token) > 1:
            number = int(token[2:-1] if token.startswith("u{") else token[1:], 16)
            if number > 0x10ffff:
                raise ValueError("invalid Unicode code point")
            return chr(number)
        if token in {"x", "u"}:
            raise ValueError("incomplete hexadecimal escape")
        return {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0", "\n": "", "\r\n": ""}.get(token, token)
    # Also inspect decoded text conservatively: regex literals can resemble
    # comment/string boundaries, so lexical ambiguity must not hide escapes.
    found = scan_text(location, text, contracts)
    gap = r"(?:\s|[()]|/\*[\s\S]*?\*/|//[^\n]*(?:\n|$))*"
    if re.search(r"['\"`]" + gap + r"\+" + gap + r"['\"`]", text):
        found.append(Finding(f"{location}:1", "unsupported JavaScript literal composition"))
    try:
        encoded = [decode(match) for match in escapes.finditer(text) if match.group()[1:2] in {"x", "u", "\r", "\n"}]
        if any(not value or any(unicodedata.category(character) not in {"Cc", "Cs", "Cn"} for character in value)
               for value in encoded):
            found.append(Finding(f"{location}:1", "unsupported encoded JavaScript composition"))
        found.extend(scan_text(location, escapes.sub(decode, text), contracts))
    except ValueError:
        found.append(Finding(f"{location}:1", "unsupported ambiguous JavaScript escape"))
    previous_literal = None
    for literal_match in literals.finditer(text):
        body = literal_match.group("body")
        if body is None:
            continue
        if previous_literal is not None:
            gap = text[previous_literal.end():literal_match.start()]
            gap = re.sub(r"/\*[\s\S]*?\*/|//[^\n]*", "", gap)
            if re.fullmatch(r"[\s()+]*\+[\s()+]*", gap):
                found.append(Finding(f"{location}:1", "unsupported JavaScript literal composition"))
        previous_literal = literal_match
        if len(body) > 65536:
            found.append(Finding(f"{location}:1", "unsupported oversized JavaScript literal"))
            continue
        try:
            decoded = escapes.sub(decode, body)
        except ValueError:
            found.append(Finding(f"{location}:1", "unsupported undecodable JavaScript literal"))
            continue
        if any(match.group()[1:] not in {"n", "r", "t", "b", "f", "v", "0", "\\", "'", '"', "`", "/"}
               for match in escapes.finditer(body)):
            found.append(Finding(f"{location}:1", "unsupported encoded JavaScript composition"))
        offset = text.count("\n", 0, literal_match.start())
        for item in scan_text(location, decoded, contracts, literal=True):
            _, line = item.location.rsplit(":", 1)
            found.append(Finding(f"{location}:{offset + int(line)}", item.identifier))
    return found


def scan_json(location: str, text: str, contracts) -> list[Finding]:
    try:
        pending = [json.loads(text.removeprefix("\ufeff"))]
    except (ValueError, RecursionError):
        return [Finding(f"{location}:1", "unsupported undecodable JSON source")]
    found = []
    visited = 0
    while pending:
        item = pending.pop()
        visited += 1
        if visited > 100000:
            return [*found, Finding(f"{location}:1", "unsupported oversized JSON structure")]
        if isinstance(item, str):
            found.extend(scan_text(location, item, contracts, literal=True))
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
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
    env = {name: ContainerValue("callable", ("builtins." + name,)) for name in ("str", "repr", "ascii", "len", "bool", "int", "float", "dict", "list", "tuple", "set")}
    unresolved_stems: set[str] = set()
    binding_events: list[tuple[str, str | ContainerValue | None]] = []
    found = []
    class_scopes = []
    function_locals = []
    function_declarations = []
    module_scope = None
    active_visitors = []
    expansion_budget = 4096
    analysis_steps = MAX_ANALYSIS_STEPS

    def consume_analysis_step():
        nonlocal analysis_steps
        analysis_steps -= 1
        if analysis_steps < 0:
            raise AnalysisBudgetExceeded

    shadowed_qualified = set()
    loop_carried_names = []
    loop_written_names = []
    unsupported_path_builders = {"os.path.join", "posixpath.join", "ntpath.join", "pathlib.PurePath", "pathlib.PurePosixPath", "pathlib.PureWindowsPath", "pathlib.PosixPath", "pathlib.WindowsPath"}

    def unsupported(node: ast.AST, form: str) -> None:
        if active_visitors and active_visitors[-1].reviewed_analysis:
            return
        found.append(Finding(f"{location}:{getattr(node, 'lineno', 1)}", f"unsupported legacy syntax ({form}); use immutable contract-based construction"))

    def index_value(node: ast.AST):
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, bytes)):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)) and isinstance(node.operand, ast.Constant) and type(node.operand.value) is int:
            return node.operand.value if isinstance(node.op, ast.UAdd) else -node.operand.value
        return value(node)

    def joined(parts: list[str]) -> str | None:
        return "".join(parts) if sum(map(len, parts)) <= 65536 else None

    def unbind(name: str) -> None:
        for key in list(env):
            if key == name or key.startswith((name + ".", name + "[")):
                env.pop(key)
        for key in list(unresolved_stems):
            if key == name or key.startswith((name + ".", name + "[")):
                unresolved_stems.discard(key)

    def qualified(node):
        stored = env.get(ast.unparse(node))
        if isinstance(stored, ContainerValue) and stored.kind in {"module", "callable"}:
            return stored.items[0]
        if ast.unparse(node) in shadowed_qualified:
            return None
        if isinstance(node, ast.Attribute):
            parent = qualified(node.value)
            return parent + "." + node.attr if parent else None
        return None

    def selector_inputs(node):
        reference = qualified(node.func)
        if reference not in SELECTOR_CALLS | {"os.getenv", "os.environ.get"}:
            return None
        if reference in SELECTOR_CALLS:
            implementation = ROOT / "src/case_intelligence/compat_env.py"
            digest = hashlib.sha256(implementation.read_text().replace("\r\n", "\n").encode()).hexdigest()
            if digest != SELECTOR_MODULE_SHA256:
                unsupported(node, "changed environment selector implementation")
                return [*node.args, *(keyword.value for keyword in node.keywords)]
        inputs = list(node.args[1:2])
        for keyword in node.keywords:
            if keyword.arg == "default":
                inputs.append(keyword.value)
            elif keyword.arg == "environ":
                mapping = structure(keyword.value)
                if mapping is not None and mapping.kind == "dict":
                    inputs.extend(literal(item) for _, item in mapping.items)
                else:
                    inputs.append(keyword.value)
            elif keyword.arg is None:
                inputs.append(keyword.value)
        return inputs

    def frozen(node: ast.AST) -> str | ContainerValue | None:
        consume_analysis_step()
        if isinstance(node, FrozenDomainValue):
            return node.item
        if (isinstance(node, ast.Call) and qualified(node.func) == "pathlib.Path") or (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
            path = structure(node)
            if path is not None and path.kind == "path":
                return path
        if isinstance(node, (ast.Name, ast.Attribute)):
            stored = env.get(ast.unparse(node))
            if isinstance(stored, ContainerValue) and stored.kind == "path":
                return stored
        if isinstance(node, (ast.IfExp, ast.BoolOp)):
            selected = structure(node)
            if selected is not None:
                return selected
        resolved = value(node)
        if resolved is not None:
            return resolved
        structured = structure(node)
        if structured is not None:
            return structured
        return ContainerValue("unknown", ()) if contains_stem(node) else None

    def comprehension_bindings(node):
        nonlocal env, expansion_budget
        if len(node.generators) != 1 or node.generators[0].is_async:
            return None
        generator = node.generators[0]
        if any(isinstance(part, (ast.NamedExpr, ast.Await, ast.Yield, ast.YieldFrom)) for condition in generator.ifs for part in ast.walk(condition)):
            return None
        iterable = structure(generator.iter)
        if iterable is None or iterable.kind not in {"list", "tuple", "set", "dict"}:
            return None
        items = tuple(key for key, _ in iterable.items) if iterable.kind == "dict" else iterable.items
        if len(items) > 256 or len(items) > expansion_budget:
            return None
        expansion_budget -= len(items)
        results = []
        def targets(target, item, bindings):
            if isinstance(target, ast.Name):
                bindings[target.id] = item
                return True
            if isinstance(target, (ast.Tuple, ast.List)) and isinstance(item, ContainerValue) and item.kind in {"list", "tuple"} and len(target.elts) == len(item.items):
                return all(targets(child, part, bindings) for child, part in zip(target.elts, item.items))
            return False
        for item in items:
            bindings = {}
            if not targets(generator.target, item, bindings):
                return None
            scope = dict(class_scopes[-1][0] if class_scopes else env)
            for name, item in bindings.items():
                for key in list(scope):
                    if key == name or key.startswith((name + ".", name + "[")):
                        scope.pop(key)
                if item is not None:
                    scope[name] = item
            results.append((scope, set(bindings)))
        return results

    def callable_value(item, *, verified_only=False) -> bool:
        if not isinstance(item, ContainerValue):
            return False
        if item.kind == "module":
            return not verified_only
        if item.kind == "callable":
            return not verified_only or item.items[0].startswith("builtins.") or item.items[0] in unsupported_path_builders | {"pathlib.Path"}
        if item.kind in {"local_callable", "ambiguous_callable"}:
            return True
        return any(callable_value(value, verified_only=verified_only) for value in item.items) if item.kind != "dict" else any(
            callable_value(value, verified_only=verified_only) for _, value in item.items)

    def may_carry_callable(node):
        pending = [node]
        while pending:
            piece = pending.pop()
            consume_analysis_step()
            if isinstance(piece, FrozenDomainValue) and callable_value(piece.item, verified_only=True):
                return True
            if isinstance(piece, ast.Lambda):
                return True
            if isinstance(piece, (ast.Name, ast.Attribute, ast.Subscript)):
                if callable_value(env.get(ast.unparse(piece)), verified_only=True):
                    return True
                if isinstance(piece, ast.Attribute) and qualified(piece) in unsupported_path_builders | {"pathlib.Path"}:
                    return True
            if isinstance(piece, ast.Call):
                stored = env.get(ast.unparse(piece.func))
                if isinstance(stored, ContainerValue) and stored.kind == "ambiguous_callable":
                    return True
                pending.extend([*piece.args, *(item.value for item in piece.keywords)])
                if isinstance(piece.func, ast.Attribute) and not qualified(piece.func.value):
                    pending.append(piece.func.value)
            else:
                pending.extend(ast.iter_child_nodes(piece))
        return False

    def callable_choice(items):
        if any(callable_value(item) for item in items):
            if all(item == items[0] for item in items):
                return items[0]
            return ContainerValue("ambiguous_callable", tuple(items))
        return None

    def structure(node: ast.AST) -> ContainerValue | None:
        nonlocal env, unresolved_stems
        consume_analysis_step()
        if isinstance(node, FrozenDomainValue):
            return node.item
        if isinstance(node, ast.Lambda):
            return ContainerValue("local_callable", ())
        if isinstance(node, (ast.IfExp, ast.BoolOp)):
            choices = [node.body, node.orelse] if isinstance(node, ast.IfExp) else boolean_operands(node)
            selected = callable_choice([frozen(choice) for choice in choices])
            if selected is not None:
                return selected
        if isinstance(node, ast.Attribute) and may_carry_callable(node.value):
            parent = structure(node.value)
            if parent is not None and callable_value(parent, verified_only=True):
                return ContainerValue("ambiguous_callable", (parent,))
        if isinstance(node, (ast.Name, ast.Attribute)) and qualified(node) in unsupported_path_builders | {"pathlib.Path"}:
            return ContainerValue("callable", (qualified(node),))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            left, right = structure(node.left), structure(node.right)
            if left is not None and right is not None and left.kind == right.kind == "set":
                return ContainerValue("set", tuple(dict.fromkeys((*left.items, *right.items))))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left = structure(node.left)
            if left is not None and left.kind == "path":
                return ContainerValue("path", (*left.items, frozen(node.right)))
        if isinstance(node, ast.Call) and qualified(node.func) == "builtins.str" and len(node.args) == 1 and not node.keywords:
            return ContainerValue("string", (frozen(node.args[0]),))
        if isinstance(node, ast.Call) and qualified(node.func) == "builtins.dict":
            if len(node.args) > 1:
                return None
            original = structure(node.args[0]) if node.args else ContainerValue("dict", ())
            if original is not None and original.kind != "dict":
                return None
            items = list(original.items) if original is not None else []
            for keyword in node.keywords:
                if keyword.arg is not None:
                    items.append((keyword.arg, frozen(keyword.value)))
                else:
                    added = structure(keyword.value)
                    if added is None or added.kind != "dict":
                        return None
                    items.extend(added.items)
            if original is None and node.args and contains_stem(node.args[0]):
                return ContainerValue("unknown", ())
            return ContainerValue("dict", tuple(items))
        if isinstance(node, ast.Call) and qualified(node.func) in {"builtins.list", "builtins.tuple", "builtins.set"} and not node.args and not node.keywords:
            return ContainerValue(qualified(node.func).rsplit(".", 1)[1], ())
        if isinstance(node, ast.Call) and qualified(node.func) in {"builtins.list", "builtins.tuple", "builtins.set"} and len(node.args) == 1 and not node.keywords:
            original = structure(node.args[0])
            if original is not None and original.kind in {"list", "tuple", "set", "dict"}:
                return ContainerValue(qualified(node.func).rsplit(".", 1)[1], tuple(key for key, _ in original.items) if original.kind == "dict" else original.items)
        if isinstance(node, ast.Call) and qualified(node.func) in {"pathlib.Path", "pathlib.Path.home", "pathlib.Path.cwd"}:
            parts = []
            for arg in node.args:
                part = frozen(arg)
                parts.extend(part.items if isinstance(part, ContainerValue) and part.kind == "path" else (part,))
            return ContainerValue("path", tuple(parts) or (None,))
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            parent = structure(node.value)
            if parent is not None and parent.kind == "path":
                return ContainerValue("path", parent.items[:-1] or (None,))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            parent = structure(node.func.value)
            if node.func.attr == "get" and parent is not None and 1 <= len(node.args) <= 2 and not node.keywords:
                if parent.kind == "ambiguous_callable":
                    return parent
                if parent.kind == "dict":
                    key = index_value(node.args[0])
                    default = frozen(node.args[1]) if len(node.args) == 2 else None
                    if key is None:
                        return callable_choice([*(item for _, item in parent.items), default])
                    selected = next((item for name, item in reversed(parent.items) if name == key), default)
                    if isinstance(selected, ContainerValue):
                        return selected
            if parent is not None and parent.kind == "path" and node.func.attr == "with_name" and len(node.args) == 1 and not node.keywords:
                return ContainerValue("path", (*parent.items[:-1], frozen(node.args[0])))
            if parent is not None and parent.kind == "path" and node.func.attr in {"expanduser", "absolute", "resolve"}:
                return parent
        if isinstance(node, ast.Call) and qualified(node.func) == "argparse.ArgumentParser":
            return ContainerValue("object", tuple(frozen(arg) for arg in [*node.args, *(keyword.value for keyword in node.keywords)]))
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            possibilities = comprehension_bindings(node)
            if possibilities is None:
                return None
            outer, outer_unknown = env, unresolved_stems
            items = []
            for scope, targets in possibilities:
                env, unresolved_stems = scope, (class_scopes[-1][1] if class_scopes else outer_unknown) - targets
                items.append((index_value(node.key), frozen(node.value)) if isinstance(node, ast.DictComp) else frozen(node.elt))
            env, unresolved_stems = outer, outer_unknown
            if node.generators[0].ifs:
                return ContainerValue("selection", tuple(items))
            return ContainerValue("dict" if isinstance(node, ast.DictComp) else "list", tuple(items))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and not node.args and not node.keywords:
            container = structure(node.func.value)
            if container is not None and container.kind == "dict":
                if node.func.attr == "keys":
                    return ContainerValue("list", tuple(key for key, _ in container.items))
                if node.func.attr == "values":
                    return ContainerValue("list", tuple(item for _, item in container.items))
                if node.func.attr == "items":
                    return ContainerValue("list", tuple(ContainerValue("tuple", pair) for pair in container.items))
        if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)):
            stored = env.get(ast.unparse(node))
            if isinstance(stored, ContainerValue):
                return stored
            if isinstance(node, ast.Subscript):
                stored = member(node)
                return stored if isinstance(stored, ContainerValue) else None
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            if any(isinstance(item, ast.Starred) and contains_stem(item.value) for item in node.elts):
                unsupported(node, "starred collection display")
                return ContainerValue("unknown", ())
            return ContainerValue(type(node).__name__.lower(), tuple(frozen(item) for item in node.elts))
        if isinstance(node, ast.Dict):
            if any(key is None and contains_stem(item) for key, item in zip(node.keys, node.values)):
                unsupported(node, "mapping unpacking")
                return ContainerValue("unknown", ())
            return ContainerValue("dict", tuple((index_value(key) if key is not None else None, frozen(item))
                                                for key, item in zip(node.keys, node.values)))
        if isinstance(node, ast.Call):
            callee = structure(node.func) if may_carry_callable(node.func) else None
            raw_inputs = [*node.args, *(item.value for item in node.keywords)]
            if isinstance(node.func, ast.Attribute) and not qualified(node.func.value):
                raw_inputs.append(node.func.value)
            captured = (callee,) if callee is not None and callee.kind == "ambiguous_callable" else ()
            if captured or any(may_carry_callable(item) for item in raw_inputs):
                inputs = tuple(frozen(item) for item in call_inputs(node)
                               if not (isinstance(node.func, ast.Attribute) and item is node.func.value and qualified(item)))
                if captured or any(callable_value(item, verified_only=True) for item in inputs):
                    if any(legacy_value(item) for item in (*captured, *inputs)):
                        unsupported(node, "unmodeled callable consumer with legacy data")
                    return ContainerValue("ambiguous_callable", (*captured, *inputs))
        return None

    def member(node: ast.Subscript) -> str | ContainerValue | None:
        source = structure(node.value)
        if source is None:
            return None
        key = index_value(node.slice)
        if source.kind == "ambiguous_callable":
            return source
        if source.kind == "selection":
            return (ContainerValue("ambiguous_callable", source.items) if callable_value(source)
                    else ContainerValue("unknown", ()) if legacy_value(source) else None)
        if key is None and callable_value(source):
            return ContainerValue("ambiguous_callable", (source,))
        if source.kind == "dict":
            return next((item for name, item in reversed(source.items) if name == key), None)
        if source.kind in {"list", "tuple"} and isinstance(key, int) and -len(source.items) <= key < len(source.items):
            return source.items[key]
        return None

    def literal(item: str | ContainerValue | None) -> ast.AST:
        if isinstance(item, ContainerValue):
            if item.kind in {"path", "string", "module", "callable", "local_callable", "ambiguous_callable", "object"}:
                return FrozenDomainValue(item)
            if item.kind == "dict":
                return ast.Dict(keys=[ast.Constant(value=key) for key, _ in item.items], values=[literal(value) for _, value in item.items])
            if item.kind == "unknown":
                return UnknownLegacy()
            return ast.List(elts=[literal(value) for value in item.items], ctx=ast.Load())
        return ast.Constant(value=item)

    def value(node: ast.AST) -> str | None:
        consume_analysis_step()
        if isinstance(node, ast.Attribute) and node.attr == "name":
            parent = structure(node.value)
            if parent is not None and parent.kind == "path" and parent.items and isinstance(parent.items[-1], str):
                return parent.items[-1].rsplit("/", 1)[-1]
        if isinstance(node, FrozenDomainValue) and node.item.kind == "path" and all(part is None or isinstance(part, str) for part in node.item.items):
            return "/".join(part if part is not None else "CONTRACTDYNAMIC" for part in node.item.items)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                return node.value
            if isinstance(node.value, bytes):
                try:
                    return node.value.decode("utf-8")
                except UnicodeDecodeError:
                    return None
        if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)):
            if isinstance(node, ast.Attribute) and node.attr == "hex" and isinstance(node.value, ast.Call) and qualified(node.value.func) in {"uuid.uuid4", "uuid.uuid5", "uuid.uuid3"}:
                return "CONTRACTDYNAMIC"
            stored = env.get(ast.unparse(node))
            if stored is None and isinstance(node, ast.Subscript):
                stored = member(node)
            if isinstance(stored, ContainerValue) and stored.kind == "path" and all(isinstance(part, str) or part is None for part in stored.items):
                return "/".join(part if part is not None else "CONTRACTDYNAMIC" for part in stored.items)
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
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left, right = value(node.left), value(node.right)
            if right is None and not contains_stem(node.right):
                right = "CONTRACTDYNAMIC"
            if right is not None and (left is not None or not contains_stem(node.left)):
                return joined([left if left is not None else "CONTRACTDYNAMIC", "/", right])
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
        if isinstance(node, ast.Call):
            inputs = selector_inputs(node)
            if inputs is not None:
                candidates = {value(item) for item in inputs if value(item) is not None and contains_stem(item)}
                if len(candidates) == 1:
                    return next(iter(candidates))
            reference = qualified(node.func)
            if reference == "builtins.str" and len(node.args) == 1 and not node.keywords:
                return value(node.args[0])
            if reference == "json.dumps" and len(node.args) == 1:
                def data(item):
                    if isinstance(item, str):
                        return item
                    if isinstance(item, ContainerValue) and item.kind in {"list", "tuple"}:
                        return [data(part) for part in item.items]
                    if isinstance(item, ContainerValue) and item.kind == "dict":
                        return {key: data(part) for key, part in item.items}
                    raise ValueError("unresolved JSON input")
                try:
                    options = {keyword.arg: ast.literal_eval(keyword.value) for keyword in node.keywords}
                    if None not in options and options.keys() <= {"ensure_ascii", "indent", "sort_keys", "separators"}:
                        return joined([json.dumps(data(frozen(node.args[0])), **options)])
                except (ValueError, TypeError, RecursionError):
                    pass
            if reference == "case_intelligence.legacy_names.name_value" and node.args:
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
            if receiver is not None and node.func.attr == "join" and len(node.args) == 1 and not node.keywords:
                parts = structure(node.args[0])
                if parts is not None and parts.kind in {"list", "tuple"} and all(isinstance(part, str) for part in parts.items):
                    return joined([receiver.join(parts.items)])
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
            if qualified(node.func) == "re.escape" and len(arguments) == 1 and arguments[0] is not None:
                return re.escape(arguments[0])
            if node.func.attr == "hexdigest" and isinstance(node.func.value, ast.Call) and qualified(node.func.value.func) in {"hashlib.sha256", "hashlib.sha512", "hashlib.sha1", "hashlib.md5"}:
                return "CONTRACTDYNAMIC"
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

        Input roles require a verified receiver or callable. An arbitrary
        method named get, replace or startswith has no assumed semantics.
        Parser and regex objects can expose their legacy-bearing inputs again.
        """
        consume_analysis_step()
        inputs = selector_inputs(node)
        if inputs is not None:
            return inputs
        if isinstance(node.func, ast.Attribute):
            method = node.func.attr
            receiver = [node.func.value]
            if method in {"format", "format_map", "join"}:
                return [*receiver, *node.args, *(arg.value for arg in node.keywords)]
            structured = structure(node.func.value)
            if method == "get" and structured is not None and structured.kind == "dict":
                key = index_value(node.args[0]) if node.args else None
                selected = [item for name, item in structured.items if key is None or name == key]
                return [*(literal(item) for item in selected), *node.args[1:2]]
            if value(node.func.value) is not None or (structured is not None and structured.kind == "string"):
                if method == "replace":
                    return [*receiver, *node.args[1:2]]
                if method in STRING_METHODS:
                    return receiver
        if qualified(node.func) in {"builtins.len", "builtins.bool", "builtins.int", "builtins.float"}:
            return []  # Verified constructors/consumers do not return identifier text.
        if isinstance(node.func, ast.Attribute):
            receiver = structure(node.func.value)
            if receiver is not None and receiver.kind == "path" and node.func.attr in {"read_text", "read_bytes", "exists", "is_file", "is_dir", "is_symlink", "stat", "lstat"}:
                return []  # A verified Path selector is not returned file content.
            if node.func.attr in {"startswith", "endswith"} and value(node.func.value) is not None:
                return []
        return [*([node.func.value] if isinstance(node.func, ast.Attribute) else []), *node.args, *(arg.value for arg in node.keywords)]

    def contains_stem(piece: ast.AST) -> bool:
        consume_analysis_step()
        if isinstance(piece, FrozenDomainValue):
            return legacy_value(piece.item)
        if isinstance(piece, UnknownLegacy):
            return True
        if isinstance(piece, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            resolved = structure(piece)
            if resolved is not None:
                return legacy_value(resolved)
        if isinstance(piece, (ast.Name, ast.Attribute, ast.Subscript)):
            if ast.unparse(piece) in unresolved_stems or legacy_value(env.get(ast.unparse(piece))):
                return True
            if isinstance(piece, ast.Subscript):
                selected = member(piece)
                if selected is not None:
                    return legacy_value(selected)
        if isinstance(piece, ast.Dict):
            return any(contains_stem(item) for item in [*piece.values, *(key for key in piece.keys if key is not None)])
        if isinstance(piece, ast.Starred):
            return contains_stem(piece.value)
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
        if isinstance(piece, (ast.Compare, ast.Lambda)) or (isinstance(piece, ast.UnaryOp) and isinstance(piece.op, ast.Not)):
            return False
        return any(contains_stem(child) for child in ast.iter_child_nodes(piece) if isinstance(child, ast.expr))

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
            if isinstance(target, (ast.Attribute, ast.Subscript)):
                prior = scope.get(ast.unparse(target.value))
                if legacy_value(resolved) or unresolved or legacy_value(prior):
                    unsupported(target, "mutable attribute or container state")
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
            if isinstance(target, ast.Attribute):
                shadowed_qualified.add(name)
            root = re.split(r"[.\[]", name, maxsplit=1)[0]
            if any(root in names for names in loop_carried_names) and (legacy_value(resolved) or unresolved):
                accumulator = False
                prior_state_names = {root}.union(*loop_written_names)
                if isinstance(target, ast.Name) and isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.BitOr):
                    own = [part for part in (expression.left, expression.right) if isinstance(part, ast.Name) and part.id == target.id]
                    other = expression.right if own and own[0] is expression.left else expression.left
                    accumulator = bool(own) and isinstance(resolved, ContainerValue) and resolved.kind == "set" and not any(isinstance(part, ast.Name) and part.id in prior_state_names for part in ast.walk(other))
                if isinstance(target, ast.Name) and isinstance(expression, ast.Call) and qualified(expression.func) == "builtins.dict":
                    accumulator = len(expression.args) == 1 and isinstance(expression.args[0], ast.Name) and expression.args[0].id == target.id and not any(isinstance(part, ast.Name) and part.id in prior_state_names for keyword in expression.keywords for part in ast.walk(keyword.value))
                if not accumulator:
                    unsupported(target, "loop-carried legacy binding")
            if not class_scopes and function_declarations and root in function_declarations[-1] and (legacy_value(resolved) or unresolved):
                unsupported(target, "global or nonlocal write")
            if class_scopes:
                outer_env, outer_unresolved, outer_events, local_names, declared_outer = class_scopes[-1]
                root = re.split(r"[.\[]", name, maxsplit=1)[0]
                if root in declared_outer and (legacy_value(resolved) or unresolved):
                    unsupported(target, "global or nonlocal write")
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
            self.comprehension_depth = 0
            self.try_depth = 0
            self.finally_depth = 0

        def visit(self, node):
            active_visitors.append(self)
            try:
                return super().visit(node)
            finally:
                active_visitors.pop()

        def generic_visit(self, node):
            supported = (UnknownLegacy, FrozenDomainValue, ast.Constant, ast.Name, ast.Attribute, ast.Subscript, ast.List, ast.Tuple, ast.Set, ast.Dict,
                         ast.JoinedStr, ast.FormattedValue, ast.Call, ast.BinOp, ast.IfExp, ast.BoolOp, ast.NamedExpr,
                         ast.Compare, ast.UnaryOp, ast.Lambda, ast.Starred, ast.Yield, ast.YieldFrom, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
            if isinstance(node, ast.expr) and not isinstance(node, supported) and contains_stem(node):
                unsupported(node, type(node).__name__)
            super().generic_visit(node)

        def visit_Constant(self, node: ast.Constant) -> None:
            decoded = node.value
            if isinstance(decoded, bytes):
                try:
                    decoded = decoded.decode("utf-8")
                except UnicodeDecodeError:
                    return
            if isinstance(decoded, str):
                for item in scan_text(location, decoded, contracts, literal=True):
                    _, line = item.location.rsplit(":", 1)
                    found.append(Finding(f"{location}:{node.lineno + int(line) - 1}", item.identifier))

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                root = alias.asname or alias.name.split(".")[0]
                bind(ast.Name(id=root), None)
                env[root] = ContainerValue("module", (alias.name if alias.asname else alias.name.split(".")[0],))
                if alias.name == "case_intelligence.legacy_names":
                    env.update({f"{alias.asname or alias.name}.{key}": item for key, item in KNOWN_VALUES.items()})
                    for key, item in KNOWN_VALUES.items():
                        if legacy_value(item):
                            binding_events.append((f"{alias.asname or alias.name}.{key}", item))

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            module = node.module or ""
            if node.level:
                parts = Path(location).parent.parts
                package = list(parts[1:]) if parts and parts[0] == "src" else []
                if node.level <= len(package):
                    module = ".".join([*package[:len(package) - node.level + 1], *module.split(".")]).rstrip(".")
                else:
                    module = ""
            authority = module == "case_intelligence.legacy_names"
            for alias in node.names:
                name = alias.asname or alias.name
                bind(ast.Name(id=name), None)
                if alias.name != "*" and module:
                    env[name] = ContainerValue("callable", (module + "." + alias.name,))
                if authority:
                    if alias.name == "name_value":
                        env[name] = ContainerValue("callable", ("case_intelligence.legacy_names.name_value",))
                    if alias.name == "*":
                        env["name_value"] = ContainerValue("callable", ("case_intelligence.legacy_names.name_value",))
                    names = KNOWN_VALUES if alias.name == "*" else {name: KNOWN_VALUES[alias.name]} if alias.name in KNOWN_VALUES else {}
                    for bound_name, item in names.items():
                        bind(ast.Name(id=bound_name), ast.Constant(value=item))
                elif module == "case_intelligence" and alias.name == "legacy_names":
                    env[name] = ContainerValue("module", ("case_intelligence.legacy_names",))
                    env.update({f"{name}.{key}": item for key, item in KNOWN_VALUES.items()})
                    for key, item in KNOWN_VALUES.items():
                        if legacy_value(item):
                            binding_events.append((f"{name}.{key}", item))

        def visit_Delete(self, node: ast.Delete) -> None:
            for target in node.targets:
                self.visit(target)
                for part in ast.walk(target):
                    if isinstance(part, ast.Attribute):
                        shadowed_qualified.add(ast.unparse(part))
                    if isinstance(part, (ast.Attribute, ast.Subscript)) and contains_stem(part):
                        unsupported(part, "deletion of legacy-bearing mutable state")
                    if isinstance(part, (ast.Name, ast.Attribute, ast.Subscript)) and isinstance(part.ctx, ast.Del):
                        unbind(ast.unparse(part))

        def visit_Assign(self, node: ast.Assign) -> None:
            self.generic_visit(node)
            if len(node.targets) == 1:
                bind(node.targets[0], node.value)
            else:
                resolved = frozen(node.value)
                if isinstance(resolved, ContainerValue):
                    # Chained mutable aliases are outside the immutable grammar.
                    if legacy_value(resolved):
                        unsupported(node, "chained container assignment")
                bind(ast.Tuple(elts=node.targets, ctx=ast.Store()), ast.Tuple(elts=[node.value] * len(node.targets), ctx=ast.Load()))

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            self.generic_visit(node)
            if node.value is not None:
                bind(node.target, node.value)

        def loop_reads(self, node):
            # Only a direct, unconditional fresh binding can rule out carry
            # between iterations. Nested/conditional assignments stay rejected.
            reset = {part.id for part in ast.walk(node.target) if isinstance(part, ast.Name)} if isinstance(node, (ast.For, ast.AsyncFor)) else set()
            carried = set()
            statements = [*([node.test] if isinstance(node, ast.While) else []), *node.body]
            for statement in statements:
                if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                    reads = self.loop_reads(statement)
                    eager = [statement.iter] if isinstance(statement, (ast.For, ast.AsyncFor)) else [statement.test]
                    reads |= {part.id for expression in [*eager, *statement.orelse] for part in ast.walk(expression)
                              if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Load)}
                else:
                    reads = {part.id for part in ast.walk(statement)
                             if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Load)}
                reads.update(part.target.id for part in ast.walk(statement)
                             if isinstance(part, ast.AugAssign) and isinstance(part.target, ast.Name))
                carried.update(reads - reset)
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target] if isinstance(statement, ast.AnnAssign) and statement.value is not None else []
                for target in targets:
                    reset.update(part.id for part in ast.walk(target)
                                 if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Store))
            return carried

        def loop_writes(self, node):
            return {part.id for statement in node.body for part in ast.walk(statement)
                    if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Store)}

        def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
            self.visit(node.iter)
            event_offset = len(binding_events)
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
                selected = callable_choice(list(candidates))
                if selected is not None:
                    return selected
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
            loop_carried_names.append(self.loop_reads(node))
            loop_written_names.append(self.loop_writes(node))
            self.alternatives([[], [assignment, *node.body]])
            loop_carried_names.pop()
            loop_written_names.pop()
            # A break can bypass the else suite; retain both possibilities.
            self.alternatives([[], node.orelse])
            if binding_events[event_offset:] and any(isinstance(part, (ast.Break, ast.Continue)) for statement in node.body for part in ast.walk(statement)):
                unsupported(node, "abrupt loop with legacy bindings")

        visit_AsyncFor = visit_For

        def visit_While(self, node: ast.While) -> None:
            event_offset = len(binding_events)
            loop_carried_names.append(self.loop_reads(node))
            loop_written_names.append(self.loop_writes(node))
            self.visit(node.test)
            self.alternatives([[], node.body])
            loop_carried_names.pop()
            loop_written_names.pop()
            self.alternatives([[], node.orelse])
            if binding_events[event_offset:] and any(isinstance(part, (ast.Break, ast.Continue)) for statement in node.body for part in ast.walk(statement)):
                unsupported(node, "abrupt loop with legacy bindings")

        def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
            for item in node.items:
                self.visit(item.context_expr)
                if item.optional_vars is not None:
                    bind(item.optional_vars, None)
            for statement in node.body:
                self.visit(statement)

        visit_AsyncWith = visit_With

        def visit_exception_suite(self, statements, owner):
            event_offset = len(binding_events)
            simple = True
            for statement in statements:
                simple &= isinstance(statement, ast.Pass) or (
                    isinstance(statement, ast.Assign) and len(statement.targets) == 1
                    and isinstance(statement.targets[0], ast.Name)
                    and (isinstance(statement.value, ast.Constant) or
                         (isinstance(statement.value, ast.Name) and value(statement.value) is not None)))
                self.visit(statement)
            if self.finally_depth and binding_events[event_offset:] and not simple:
                unsupported(owner, "exceptional handler or else suite with legacy bindings")

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.type is not None:
                self.visit(node.type)
            if node.name:
                bind(ast.Name(id=node.name, ctx=ast.Store()), None)
            self.visit_exception_suite(node.body, node)
            if node.name:
                unbind(node.name)
                if class_scopes:
                    class_scopes[-1][3].discard(node.name)

        def visit_Try(self, node: ast.Try | ast.TryStar) -> None:
            self.finally_depth += bool(node.finalbody)
            # Visit each statement once. An exception can occur before or after
            # any statement; merge those inputs for the handler paths while
            # preserving the precise successful body result for the else path.
            exceptional = [self.snapshot()]
            for statement in node.body:
                offset = len(binding_events)
                self.try_depth += 1
                self.visit(statement)
                self.try_depth -= 1
                if isinstance(statement, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.TryStar, ast.Match)) and binding_events[offset:]:
                    unsupported(statement, "compound exception path with legacy bindings")
                exceptional.append(self.snapshot())
            self.visit_exception_suite(node.orelse, node)
            normal = self.snapshot()
            self.merge_states(exceptional)
            handler_input = self.snapshot()
            if isinstance(node, ast.TryStar):
                # Exception-group handlers can run in order on disjoint
                # subgroups, so each later handler sees earlier possible writes.
                for handler in node.handlers:
                    self.alternatives([[], [handler]])
                self.alternatives([[], []], starts=[normal, self.snapshot()])
            else:
                self.alternatives([[], *[[handler] for handler in node.handlers]],
                                  starts=[normal, *[handler_input for _ in node.handlers]])
            if node.finalbody:
                self.merge_states([self.snapshot(), *exceptional])
            for statement in node.finalbody:
                self.visit(statement)
            self.finally_depth -= bool(node.finalbody)

        visit_TryStar = visit_Try

        def visit_AugAssign(self, node: ast.AugAssign) -> None:
            expression = ast.copy_location(ast.BinOp(left=node.target, op=node.op, right=node.value), node)
            self.visit(expression)
            bind(node.target, expression)

        def visit_conditional_expression(self, node):
            writes = [part for part in ast.walk(node) if isinstance(part, ast.NamedExpr)]
            if any(contains_stem(write.target) or contains_stem(write.value) for write in writes):
                unsupported(node, "conditional legacy assignment expression")
            self.generic_visit(node)

        visit_BoolOp = visit_conditional_expression
        visit_IfExp = visit_conditional_expression

        def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
            if self.try_depth and (contains_stem(node.target) or contains_stem(node.value)):
                unsupported(node, "exception-sensitive legacy assignment expression")
            if self.comprehension_depth and contains_stem(node.value):
                unsupported(node, "legacy assignment expression in comprehension")
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
                if case.guard is not None and any(contains_stem(item.value) for item in bindings):
                    unsupported(case.guard, "guarded legacy capture")
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
                    selected = callable_choice([scope.get(key) for scope, _ in branches])
                    if key not in bindings and selected is not None:
                        bindings[key] = selected
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
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                         *([node.args.vararg] if node.args.vararg else []),
                         *([node.args.kwarg] if node.args.kwarg else [])]
            for annotation in [*(arg.annotation for arg in arguments), node.returns]:
                if annotation is not None:
                    self.visit(annotation)
            for expression in (*node.decorator_list, *node.args.defaults, *node.args.kw_defaults):
                if expression is not None:
                    self.visit(expression)
            bind(ast.Name(id=node.name), None)
            env[node.name] = ContainerValue("local_callable", ())
            scope_env, scope_unresolved, scope_events = self.method_scope or (env, unresolved_stems, binding_events)
            parameters = [*node.args.posonlyargs, *node.args.args]
            defaults = {arg.arg: frozen(default) for arg, default in zip(parameters[len(parameters) - len(node.args.defaults):], node.args.defaults)}
            defaults.update({arg.arg: frozen(default) for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults) if default is not None})
            self.deferred.append((node, dict(scope_env), set(scope_unresolved), len(scope_events), defaults))
            if class_scopes:
                class_scopes[-1][3].add(node.name)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, node: ast.Lambda) -> None:
            for expression in (*node.args.defaults, *node.args.kw_defaults):
                if expression is not None:
                    self.visit(expression)
            scope_env, scope_unresolved, scope_events = self.method_scope or (env, unresolved_stems, binding_events)
            parameters = [*node.args.posonlyargs, *node.args.args]
            defaults = {arg.arg: frozen(default) for arg, default in zip(parameters[len(parameters) - len(node.args.defaults):], node.args.defaults)}
            defaults.update({arg.arg: frozen(default) for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults) if default is not None})
            self.deferred.append((node, dict(scope_env), set(scope_unresolved), len(scope_events), defaults))

        def finish(self) -> None:
            nonlocal env, unresolved_stems, binding_events
            # A body can see bindings at its definition or later, including
            # values replaced after an earlier call. Bindings superseded before
            # the definition cannot reach it unless assigned again afterward.
            for node, definition_env, definition_unresolved, offset, defaults in self.deferred:
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
                for name, item in defaults.items():
                    if item is not None:
                        env[name] = item
                local_names = set()
                declared_names = set()
                def locals_in(statement):
                    if isinstance(statement, (ast.Global, ast.Nonlocal)):
                        declared_names.update(statement.names)
                    elif isinstance(statement, ast.Name) and isinstance(statement.ctx, ast.Store):
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
                function_locals.append(local_names - declared_names)
                function_declarations.append(declared_names)
                name = getattr(node, "name", "<lambda>")
                expected = REVIEWED_ANALYSIS_FUNCTIONS.get((location, name)) if not self.scope else None
                reviewed = expected is not None and hashlib.sha256(textwrap.dedent(ast.get_source_segment(text, node) or "").replace("\r\n", "\n").encode()).hexdigest() == expected
                visitor = Visitor(scope=(*self.scope, name), reviewed_analysis=reviewed)
                for statement in [node.body] if isinstance(node, ast.Lambda) else node.body:
                    visitor.visit(statement)
                visitor.finish()
                function_locals.pop()
                function_declarations.pop()
                env = outer
                unresolved_stems = outer_unresolved
                binding_events = outer_events

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            nonlocal env, unresolved_stems, binding_events
            for expression in (*node.decorator_list, *node.bases, *(item.value for item in node.keywords)):
                self.visit(expression)
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
            for statement in node.body:
                self.visit(statement)
            local_names = class_scopes[-1][3]
            attributes = {name: env[name] for name in env if name.split(".", 1)[0] in local_names}
            unknown_attributes = {name for name in unresolved_stems if name.split(".", 1)[0] in local_names}
            if any(legacy_value(item) for item in attributes.values()) or unknown_attributes:
                unsupported(node, "legacy-bearing class namespace")
            class_scopes.pop()
            env = outer
            unresolved_stems = outer_unresolved
            binding_events = outer_events
            self.method_scope = outer_method_scope
            bind(ast.Name(id=node.name), None)
            for name, item in attributes.items():
                env[node.name + "." + name] = item
                if legacy_value(item):
                    binding_events.append((node.name + "." + name, item))
            for name in unknown_attributes:
                unresolved_stems.add(node.name + "." + name)
                binding_events.append((node.name + "." + name, None))
            if class_scopes:
                class_scopes[-1][3].add(node.name)

        def visit_comprehension_expression(self, node):
            nonlocal env, unresolved_stems
            outer = self.snapshot()
            self.comprehension_depth += 1
            possibilities = comprehension_bindings(node)
            if possibilities is not None:
                for scope, targets in possibilities:
                    env, unresolved_stems = scope, (class_scopes[-1][1] if class_scopes else outer[1]) - targets
                    for condition in node.generators[0].ifs:
                        for call in (part for part in ast.walk(condition) if isinstance(part, ast.Call)):
                            receiver = structure(call.func.value) if isinstance(call.func, ast.Attribute) else None
                            verified = (receiver is not None and receiver.kind == "path" and call.func.attr in {"exists", "is_file", "is_dir", "is_symlink"} and not call.args and not call.keywords)
                            if not verified and (contains_stem(node.generators[0].iter) or contains_stem(condition)):
                                unsupported(condition, "unverified comprehension predicate")
                        self.visit(condition)
                    for expression in [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]:
                        self.visit(expression)
                        if getattr(node, "deferred_generator", False) and contains_stem(expression):
                            unsupported(node, "deferred legacy generator")
                        if contains_stem(expression) and frozen(expression) == ContainerValue("unknown", ()):
                            unsupported(node, "nonconstant comprehension output")
            else:
                first_iterable = frozen(node.generators[0].iter)
                self.visit(node.generators[0].iter)
                if class_scopes:
                    env, unresolved_stems = dict(class_scopes[-1][0]), set(class_scopes[-1][1])
                for index, generator in enumerate(node.generators):
                    if index:
                        self.visit(generator.iter)
                    origin = legacy_value(first_iterable) if not index else contains_stem(generator.iter)
                    bind(generator.target, UnknownLegacy() if origin else None)
                    if origin:
                        unsupported(node, "nonfinite or filtered legacy comprehension")
                    for condition in generator.ifs:
                        self.visit(condition)
                for expression in [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]:
                    self.visit(expression)
                    if getattr(node, "deferred_generator", False) and contains_stem(expression):
                        unsupported(node, "deferred legacy generator")
                    if contains_stem(expression) and frozen(expression) == ContainerValue("unknown", ()):
                        unsupported(node, "nonfinite or filtered legacy comprehension")
            self.restore(outer)
            self.comprehension_depth -= 1

        visit_ListComp = visit_comprehension_expression
        visit_SetComp = visit_comprehension_expression
        visit_DictComp = visit_comprehension_expression
        def visit_GeneratorExp(self, node):
            # The first iterable is eager; the output and guards are delayed.
            self.visit(node.generators[0].iter)
            captured_iterable = frozen(node.generators[0].iter)
            eager = ast.copy_location(ast.ListComp(elt=copy.deepcopy(node.elt), generators=copy.deepcopy(node.generators)), node)
            eager.deferred_generator = True
            eager.generators[0].iter = ast.copy_location(literal(captured_iterable), node.generators[0].iter)
            delayed = ast.copy_location(ast.Lambda(args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]), body=eager), node)
            scope_env, scope_unresolved, scope_events = self.method_scope or (env, unresolved_stems, binding_events)
            self.deferred.append((delayed, dict(scope_env), set(scope_unresolved), len(scope_events), {}))

        def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
            self.composition(node)

        def visit_BinOp(self, node: ast.BinOp) -> None:
            self.composition(node)

        def visit_Call(self, node: ast.Call) -> None:
            callee = structure(node.func)
            if callee is not None and callee.kind == "local_callable" and any(contains_stem(item) for item in call_inputs(node)):
                unsupported(node, "explicit legacy arguments to local callable")
            if callee is not None and callee.kind == "ambiguous_callable" and (legacy_value(callee) or any(contains_stem(item) for item in call_inputs(node))):
                unsupported(node, "explicit legacy arguments to ambiguous callable")
            if qualified(node.func) in unsupported_path_builders and any(contains_stem(item) for item in call_inputs(node)):
                unsupported(node, "legacy path builder output")
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"append", "extend", "insert", "update", "setdefault", "add", "__setitem__"} and any(contains_stem(item) for item in [node.func.value, *node.args, *(arg.value for arg in node.keywords)]):
                unsupported(node, "legacy-bearing mutator call")
            self.composition(node)

        def composition(self, node: ast.AST) -> None:
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr) and structure(node) is not None:
                self.generic_visit(node)
                return
            resolved = value(node)
            path_output = isinstance(node, ast.Call) and qualified(node.func) == "pathlib.Path"
            if resolved is None and isinstance(node, ast.Call) and qualified(node.func) == "pathlib.Path":
                constructed = structure(node)
                if constructed is not None:
                    resolved = value(FrozenDomainValue(constructed))
            if resolved is not None:
                scanned = (scan_component(location, resolved, contracts) if path_output or re.fullmatch(r"[A-Za-z0-9_.-]+", resolved)
                           else scan_text(location, resolved, contracts, literal=True))
                for item in scanned:
                    _, line = item.location.rsplit(":", 1)
                    found.append(Finding(f"{location}:{node.lineno + int(line) - 1}", item.identifier))
            else:
                pieces = []
                if isinstance(node, ast.JoinedStr):
                    pieces = [part.value if isinstance(part, ast.FormattedValue) else part for part in node.values]
                elif isinstance(node, ast.BinOp):
                    # Whitespace around a serialized value cannot create a new
                    # identifier; its inner expressions are still inspected.
                    whitespace = isinstance(node.op, ast.Add) and any(
                        isinstance(part, ast.Constant) and isinstance(part.value, str) and not part.value.strip()
                        for part in (node.left, node.right)
                    )
                    if not whitespace:
                        pieces = [node.left, node.right]
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    transparent = node.func.attr in {"strip", "lstrip", "rstrip"} and not node.args and not node.keywords
                    if node.func.attr in {"encode", "decode"}:
                        encoding = value(node.args[0]) if node.args else "utf-8"
                        transparent = len(node.args) <= 1 and not node.keywords and encoding in {"utf-8", "utf8", "ascii"}
                    namespace = structure(node.func.value)
                    if node.func.attr in STRING_METHODS and not transparent and not (namespace is not None and namespace.kind == "module"):
                        pieces = call_inputs(node)
                if any(contains_stem(piece) for piece in pieces) and not self.reviewed_analysis:
                    found.append(Finding(f"{location}:{node.lineno}", "unsupported legacy composition; use a contract key"))
                    return
                self.generic_visit(node)

    visitor = Visitor()
    try:
        visitor.visit(tree)
        module_scope = env, unresolved_stems, binding_events
        visitor.finish()
    except AnalysisBudgetExceeded:
        found.append(Finding(f"{location}:1", "unsupported Python analysis budget exhausted"))
    except RecursionError:
        found.append(Finding(f"{location}:1", "unsupported Python analysis depth"))
    return found


SOURCE_FORM_CONTRACT_PATH = ROOT / "config/rename-source-forms.json"


def source_form_contracts() -> dict:
    """Explicit source receipts are diagnostic-specific, never file exclusions."""
    if not SOURCE_FORM_CONTRACT_PATH.exists():
        return {}
    document = json.loads(SOURCE_FORM_CONTRACT_PATH.read_text())
    if document.get("version") != 1 or not isinstance(document.get("files"), dict):
        raise ValueError("invalid source-form contract document")
    records = document["files"]
    for path, record in records.items():
        if not isinstance(path, str) or not re.fullmatch(r"[a-f0-9]{64}", record.get("source_sha256", "")):
            raise ValueError("invalid source-form contract identity")
        for item in record.get("diagnostics", []):
            if (type(item.get("line")) is not int or item["line"] < 1
                    or not item.get("reason") or not item.get("validation")
                    or not item.get("identifier", "").startswith(("unsupported legacy syntax (", "unsupported legacy composition;"))):
                raise ValueError("invalid source-form diagnostic contract")
    return records


def apply_source_forms(location: str, text: str, items: list[Finding], records: dict,
                       *, require_current: bool = False) -> list[Finding]:
    record = records.get(location)
    if record is None:
        return items
    digest = hashlib.sha256(text.replace("\r\n", "\n").encode()).hexdigest()
    if digest != record["source_sha256"]:
        return [*items, *([Finding(f"{location}:1", "source-form contract changed; explicit review required")]
                         if require_current else [])]
    allowed = {(f"{location}:{item['line']}", item["identifier"]) for item in record["diagnostics"]}
    remaining = [item for item in items if tuple(item) not in allowed]
    if require_current and not allowed <= {tuple(item) for item in items}:
        remaining.append(Finding(f"{location}:1", "stale source-form contract; remove or review the unused diagnostic"))
    return remaining


def findings(root: Path = ROOT, contracts: Mapping[str, NameContract] = NAME_CONTRACTS) -> list[Finding]:
    errors = contract_errors(contracts)
    if errors:
        return [Finding("src/case_intelligence/legacy_names.py", error) for error in errors]
    found = []
    try:
        source_forms = source_form_contracts()
    except (ValueError, OSError, TypeError, KeyError) as error:
        return [Finding("config/rename-source-forms.json:1", str(error))]
    consumed_source_forms = set()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in relative.parts):
            continue
        if relative.as_posix() in SKIP_FILES or relative.as_posix().startswith("docs/assets/"):
            continue
        # rglob visits each directory and file, so every relative-path component
        # is checked once, independently of its descendants or content type.
        found.extend(scan_component(relative.as_posix(), path.name, contracts))
        if path.is_symlink():
            try:
                target = str(path.readlink())
                target.encode("utf-8")
            except (OSError, UnicodeEncodeError):
                found.append(Finding(f"{relative.as_posix()}:1", "unsupported unreadable symlink target"))
                continue
            found.extend(scan_text(relative.as_posix(), target, contracts, literal=True))
            for part in Path(target).parts:
                found.extend(scan_component(relative.as_posix(), part, contracts))
            continue
        if not path.is_file():
            continue
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".pdf", ".wav", ".mp3", ".mp4", ".webm", ".ogg"}:
                found.append(Finding(f"{relative.as_posix()}:1", "unsupported undecodable operational source"))
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
        if path.suffix.lower() in {".yaml", ".yml", ".toml"}:
            if re.search(r"\\[xuU]", text):
                found.append(Finding(f"{relative.as_posix()}:1", "unsupported encoded configuration string"))
            continued = re.sub(r"\\[ \t]*[\r\n\x85\u2028\u2029]+[ \t\r\n\x85\u2028\u2029]*", "", text)
            if continued != text:
                found.extend(scan_text(relative.as_posix(), continued, contracts))
        if path.suffix.lower() == ".json":
            found.extend(scan_json(relative.as_posix(), text, contracts))
        if path.suffix.lower() in {".js", ".cjs", ".mjs"}:
            found.extend(scan_javascript(relative.as_posix(), text, contracts))
        shebang = text.removeprefix("\ufeff").partition("\n")[0]
        if path.suffix == ".py" or not path.suffix or (shebang.startswith("#!") and re.search(r"\bpython(?:[23](?:\.\d+)?)?\b", shebang)):
            consumed_source_forms.add(relative.as_posix())
            syntactic = scan_python(relative.as_posix(), text, contracts, tree=authority)
            found.extend(apply_source_forms(relative.as_posix(), text, syntactic, source_forms,
                                            require_current=root.resolve() == ROOT.resolve()))
    if root.resolve() == ROOT.resolve():
        for missing in sorted(source_forms.keys() - consumed_source_forms):
            found.append(Finding(f"{missing}:1", "unused source-form contract; source was not scanned"))
    return list(dict.fromkeys(found))


def main() -> int:
    found = findings()
    if found:
        print("RENAME CONTRACT GATE: BLOCKED", file=sys.stderr)
        for item in found:
            print(f"  {item.location}: {item.identifier}", file=sys.stderr)
        return 1
    count = sum(len(record["diagnostics"]) for record in source_form_contracts().values())
    print(f"RENAME CONTRACT GATE: CLEAN ({count} exact source-form contracts; literal/resolved names still checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
