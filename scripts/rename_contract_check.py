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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from case_intelligence.legacy_names import (  # noqa: E402
    LEGACY_STEM, NAME_CONTRACTS, NameContract, contract_errors,
)

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
    rf"|(?P<name>(?:[A-Za-z0-9_][A-Za-z0-9_-]*|\.)?{_STEM}[A-Za-z0-9_.-]*)",
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
        token = match.group()
        if token.rsplit("/", 1)[-1] == HISTORICAL_ASSET:
            continue
        if not any(_registered(candidate, patterns) for candidate in (token, token.rstrip("."), token.lstrip("."))):
            line = text.count("\n", 0, match.start()) + 1
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
        tree = ast.parse(text)
    except SyntaxError:
        return []  # Compilation is enforced separately.
    env = {"LEGACY_STEM": LEGACY_STEM}
    unresolved_stems: set[str] = set()
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
            if left is not None and LEGACY_STEM in left and right is None:
                right = "CONTRACTDYNAMIC"
            if right is not None and LEGACY_STEM in right and left is None:
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
        return None

    def contains_stem(piece: ast.AST) -> bool:
        if isinstance(piece, ast.Name) and piece.id in unresolved_stems:
            return True
        if isinstance(piece, ast.Dict):
            return any(contains_stem(item) for item in piece.values)
        if isinstance(piece, (ast.List, ast.Tuple, ast.Set)):
            return any(contains_stem(item) for item in piece.elts)
        return LEGACY_STEM in (value(piece) or "")

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.deferred = []

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                if alias.name == "case_intelligence.legacy_names":
                    env[f"{alias.asname or alias.name}.LEGACY_STEM"] = LEGACY_STEM

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.module and node.module.rsplit(".", 1)[-1] == "legacy_names":
                for alias in node.names:
                    if alias.name == "LEGACY_STEM":
                        env[alias.asname or alias.name] = LEGACY_STEM
            elif node.module == "case_intelligence":
                for alias in node.names:
                    if alias.name == "legacy_names":
                        env[f"{alias.asname or alias.name}.LEGACY_STEM"] = LEGACY_STEM

        def visit_Assign(self, node: ast.Assign) -> None:
            self.generic_visit(node)
            resolved = value(node.value)
            unresolved = resolved is None and contains_stem(node.value)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    unbind(target.id)
                    if resolved is not None:
                        env[target.id] = resolved
                    elif unresolved:
                        unresolved_stems.add(target.id)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            self.generic_visit(node)
            if isinstance(node.target, ast.Name):
                resolved = value(node.value) if node.value is not None else None
                unresolved = node.value is not None and resolved is None and contains_stem(node.value)
                unbind(node.target.id)
                if resolved is not None:
                    env[node.target.id] = resolved
                elif unresolved:
                    unresolved_stems.add(node.target.id)

        def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            for expression in (*node.decorator_list, *node.args.defaults, *node.args.kw_defaults):
                if expression is not None:
                    self.visit(expression)
            self.deferred.append(node)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, node: ast.Lambda) -> None:
            for expression in (*node.args.defaults, *node.args.kw_defaults):
                if expression is not None:
                    self.visit(expression)
            self.deferred.append(node)

        def finish(self) -> None:
            nonlocal env, unresolved_stems
            # Function globals and closure aliases resolve when called, after
            # the containing scope has established its constant bindings.
            for node in self.deferred:
                outer = env
                outer_unresolved = unresolved_stems
                env = dict(env)
                unresolved_stems = set(unresolved_stems)
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

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            nonlocal env, unresolved_stems
            outer = env
            outer_unresolved = unresolved_stems
            env = dict(env)
            unresolved_stems = set(unresolved_stems)
            self.generic_visit(node)
            env = outer
            unresolved_stems = outer_unresolved

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
                    pieces = [node.left, node.right]
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
                    pieces = [node.func.value, *node.args, *(arg.value for arg in node.keywords)]
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
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        found.extend(scan_text(relative.as_posix(), text, contracts))
        if path.suffix == ".py":
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
