#!/usr/bin/env python3
"""Fail when a backticked doc identifier is neither code nor the legacy list.

Operator docs name environment variables, paths, roles, units, markers,
headers, versions, schemas, and grammar ids in code spans. Each of those
tokens has to be a string the tree reads or writes, or a retained name in
the compatibility section of docs/BRANDING.md. A renamed spelling that the
code never uses fails here, including one that only shares a suffix with a
real id.
"""
from __future__ import annotations

import ast
import html
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from case_intelligence.legacy_names import (  # noqa: E402
    CURRENT_ENV_PREFIX,
    CURRENT_GIT_PREFIX,
    CURRENT_HEADER_PREFIX,
    CURRENT_PRODUCT,
    CURRENT_STEM,
    LEGACY_ENV_PREFIX,
    LEGACY_GIT_PREFIX,
    LEGACY_HEADER_PREFIX,
    LEGACY_PRODUCT,
    LEGACY_STEM,
)


SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", "artifacts", ".tmp",
}
CODE_SUFFIXES = {
    ".py", ".yml", ".yaml", ".toml", ".sh", ".js", ".cjs", ".mjs", ".html",
    ".json", ".conf", ".template", ".in", ".service", ".timer", ".example",
    ".cfg", ".ini", ".sql", ".env",
}
# The contract test deliberately names identifiers the code must reject.
GROUND_SKIP = {"tests/test_compat_contracts.py", "scripts/doc_identifier_check.py"}
LEGACY_HEADING = "## Compatibility and historical assets"
SEPARATORS = "._-/"

_STEMS = (LEGACY_STEM, CURRENT_STEM)
_ENV_PREFIXES = (LEGACY_ENV_PREFIX, CURRENT_ENV_PREFIX)
_HEADER_PREFIXES = (LEGACY_HEADER_PREFIX, CURRENT_HEADER_PREFIX)
_BASE_ENV = {
    "LEGACY_STEM": {LEGACY_STEM},
    "CURRENT_STEM": {CURRENT_STEM},
    "LEGACY_PRODUCT": {LEGACY_PRODUCT},
    "CURRENT_PRODUCT": {CURRENT_PRODUCT},
    "LEGACY_ENV_PREFIX": {LEGACY_ENV_PREFIX},
    "CURRENT_ENV_PREFIX": {CURRENT_ENV_PREFIX},
    "LEGACY_HEADER_PREFIX": {LEGACY_HEADER_PREFIX},
    "CURRENT_HEADER_PREFIX": {CURRENT_HEADER_PREFIX},
    "LEGACY_GIT_PREFIX": {LEGACY_GIT_PREFIX},
    "CURRENT_GIT_PREFIX": {CURRENT_GIT_PREFIX},
    "LEGACY_ENV_NAME": {LEGACY_ENV_PREFIX[:-1]},
}


def _pattern() -> re.Pattern[str]:
    stems = "|".join(re.escape(stem) for stem in _STEMS)
    env = "|".join(re.escape(prefix) for prefix in _ENV_PREFIXES)
    headers = "|".join(re.escape(prefix) for prefix in _HEADER_PREFIXES)
    return re.compile(
        rf"(?P<version>\d+\.\d+\.\d+\+(?:{stems})\.\d+)"
        rf"|(?P<env>(?:{env})[A-Z0-9_]+)"
        rf"|(?P<header>(?:{headers})[A-Za-z0-9*-]+)"
        rf"|(?P<path>/(?:srv|var/lib|run|opt|usr/share|tmp)/(?:{stems})[A-Za-z0-9_./-]*)"
        rf"|(?P<role>-[Ud]\s+(?:{stems})\b)"
        rf"|(?P<name>\.?(?:{stems})(?:[._-](?:[A-Za-z0-9.+-]+|<[^>\s`]+>))+)"
    )


IDENTIFIER = _pattern()
FENCE = re.compile(
    r"^[ \t]{0,3}(?P<mark>`{3,}|~{3,})[^\n]*\n(?P<body>.*?)"
    r"^[ \t]{0,3}(?P=mark)[ \t]*$",
    re.M | re.S,
)
INLINE = re.compile(r"`([^`\n]+)`")
POSTGRES = re.compile(r"^\s*POSTGRES_(USER|DB):\s*['\"]?([A-Za-z0-9_.-]+)", re.M)
SCHEMATIC_ENV = {prefix + "X" for prefix in _ENV_PREFIXES}


class Finding:
    def __init__(self, location: str, token: str) -> None:
        self.location = location
        self.token = token

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Finding) and (self.location, self.token) == (other.location, other.token)

    def __hash__(self) -> int:
        return hash((self.location, self.token))

    def __str__(self) -> str:
        return f"{self.location}: {self.token}"


class Corpus:
    def __init__(self) -> None:
        self.exact: set[str] = set()
        self.prefixes: set[str] = set()
        self.functions: dict[str, tuple[list[str], dict[str, set[str]]]] = {}
        self._module = ""

    def add(self, value: str) -> None:
        if _interesting(value):
            self.exact.add(value)

    def add_many(self, values: set[str] | None) -> None:
        if not values:
            return
        for value in values:
            self.add(value)


def _interesting(value: str) -> bool:
    if not value or len(value) > 400:
        return False
    needles = (
        LEGACY_STEM, CURRENT_STEM, LEGACY_PRODUCT, CURRENT_PRODUCT,
        LEGACY_ENV_PREFIX, CURRENT_ENV_PREFIX,
        LEGACY_HEADER_PREFIX, CURRENT_HEADER_PREFIX,
    )
    return any(needle in value for needle in needles)


def _good_prefix(value: str) -> bool:
    return bool(value) and value[-1] in SEPARATORS and _interesting(value)


def _eval(node: ast.AST, env: dict[str, set[str]]) -> set[str] | None:
    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        if node.id == "names":
            return {LEGACY_STEM, CURRENT_STEM}
        return env.get(node.id)
    if isinstance(node, ast.JoinedStr):
        parts: list[set[str]] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append({value.value})
            elif isinstance(value, ast.FormattedValue) and value.conversion == -1 and value.format_spec is None:
                got = _eval(value.value, env)
                if got is None:
                    return None
                parts.append(got)
            else:
                return None
        return _fold(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _eval(node.left, env), _eval(node.right, env)
        if left is None or right is None:
            return None
        return _fold([left, right])
    return None


def _fold(parts: list[set[str]]) -> set[str] | None:
    acc = {""}
    for part in parts:
        acc = {left + right for left in acc for right in part}
        if len(acc) > 64:
            return None
    return acc


def _joined_prefixes(node: ast.JoinedStr, env: dict[str, set[str]], params: set[str]) -> tuple[set[str], dict[str, set[str]]]:
    """Known text before the first unknown piece, used for <placeholder> ids."""
    acc = {""}
    bound: dict[str, set[str]] = {}
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            got: set[str] | None = {value.value}
        elif isinstance(value, ast.FormattedValue) and value.conversion == -1 and value.format_spec is None:
            got = _eval(value.value, env)
        else:
            got = None
        if got is None:
            prefixes = {item for item in acc if _good_prefix(item)}
            if (isinstance(value, ast.FormattedValue) and isinstance(value.value, ast.Name)
                    and value.value.id in params):
                bound[value.value.id] = set(prefixes)
            return prefixes, bound
        folded = _fold([acc, got])
        if folded is None:
            return set(), {}
        acc = folded
    return set(), {}


ENV_CALLS = {"env", "required_env", "_env", "_required_env"}
QA_CALL = re.compile(r"""qa\(\s*['"]([A-Z0-9_]+)['"]\s*\)""")


def _both_env(value: str) -> set[str]:
    suffix = value
    for prefix in _ENV_PREFIXES:
        if value.startswith(prefix):
            suffix = value[len(prefix):]
            break
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", suffix) is None:
        return set()
    return {prefix + suffix for prefix in _ENV_PREFIXES}


def _rows(node: ast.AST, env: dict[str, set[str]]) -> list[list[set[str]]] | None:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    rows: list[list[set[str]]] = []
    for elt in node.elts:
        if isinstance(elt, (ast.List, ast.Tuple)):
            cells: list[set[str]] = []
            for cell in elt.elts:
                got = _eval(cell, env)
                if got is None and not isinstance(cell, ast.Constant):
                    return None
                cells.append(got or set())
            rows.append(cells)
            continue
        got = _eval(elt, env)
        if got is None and not isinstance(elt, ast.Constant):
            return None
        rows.append([got or set()])
    return rows


def _bind_loop(node: ast.For, env: dict[str, set[str]]) -> None:
    target = node.target
    if isinstance(target, ast.Name):
        names = [target.id]
    elif isinstance(target, ast.Tuple) and all(isinstance(elt, ast.Name) for elt in target.elts):
        names = [elt.id for elt in target.elts]
    else:
        return
    rows = _rows(node.iter, env)
    if not rows or any(len(row) != len(names) for row in rows):
        return
    for index, name in enumerate(names):
        values: set[str] = set()
        for row in rows:
            values |= row[index]
        if values:
            env[name] = values


def _parametrize(node: ast.FunctionDef) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or _call_name(decorator) != "parametrize":
            continue
        if len(decorator.args) < 2:
            continue
        label, values = decorator.args[0], decorator.args[1]
        if not isinstance(label, ast.Constant) or not isinstance(label.value, str):
            continue
        names = [part.strip() for part in label.value.split(",") if part.strip()]
        rows = _rows(values, _BASE_ENV)
        if not rows or any(len(row) != len(names) for row in rows):
            continue
        for index, name in enumerate(names):
            acc: set[str] = set()
            for row in rows:
                acc |= row[index]
            if acc:
                found[name] = acc
    return found


def _apply_prefix(param: str, arg: ast.AST, prefixes: dict[str, set[str]], env: dict[str, set[str]], corpus: Corpus) -> None:
    if param not in prefixes:
        return
    for prefix in prefixes[param]:
        for value in _eval(arg, env) or set():
            corpus.add(prefix + value)


def _bind_one_call(node: ast.Call, env: dict[str, set[str]], functions: dict[str, tuple[list[str], dict[str, set[str]]]], corpus: Corpus) -> None:
    name = _call_name(node)
    if name in ENV_CALLS and node.args:
        for value in _eval(node.args[0], env) or set():
            corpus.add_many(_both_env(value))
    recorded = functions.get(name or "")
    if recorded is None:
        return
    params, prefixes = recorded
    for index, arg in enumerate(node.args):
        if index < len(params):
            _apply_prefix(params[index], arg, prefixes, env, corpus)
    for keyword in node.keywords:
        if keyword.arg:
            _apply_prefix(keyword.arg, keyword.value, prefixes, env, corpus)


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _walk_python(path: Path, corpus: Corpus) -> dict[str, set[str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return {}
    module_env = dict(_BASE_ENV)
    functions: dict[str, tuple[list[str], dict[str, set[str]]]] = {}

    def walk(body: list[ast.stmt], env: dict[str, set[str]], params: list[str], func_name: str) -> None:
        local = dict(env)
        param_names = set(params)
        param_prefixes: dict[str, set[str]] = {name: set() for name in params}

        class Visitor(ast.NodeVisitor):
            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                walk(node.body, local, [], "")

            def visit_Assign(self, node: ast.Assign) -> None:
                values = _eval(node.value, local)
                corpus.add_many(values)
                if values is not None and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    local[node.targets[0].id] = values
                self.generic_visit(node)

            def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
                if node.value is not None:
                    values = _eval(node.value, local)
                    corpus.add_many(values)
                    if values is not None and isinstance(node.target, ast.Name):
                        local[node.target.id] = values
                self.generic_visit(node)

            def visit_For(self, node: ast.For) -> None:
                _bind_loop(node, local)
                self.generic_visit(node)

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                names = [arg.arg for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)]
                bound = _parametrize(node)
                saved = {name: local.get(name) for name in bound}
                local.update(bound)
                walk(node.body, local, names, node.name)
                for name, previous in saved.items():
                    if previous is None:
                        local.pop(name, None)
                    else:
                        local[name] = previous
                for decorator in node.decorator_list:
                    self.visit(decorator)

            visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

            def visit_Call(self, node: ast.Call) -> None:
                _bind_one_call(node, local, functions, corpus)
                self.generic_visit(node)

            def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
                corpus.add_many(_eval(node, local))
                prefixes, bound = _joined_prefixes(node, local, param_names)
                corpus.prefixes.update(prefixes)
                for name, values in bound.items():
                    param_prefixes.setdefault(name, set()).update(values)
                self.generic_visit(node)

            def visit_BinOp(self, node: ast.BinOp) -> None:
                if isinstance(node.op, ast.Add):
                    left, right = _eval(node.left, local), _eval(node.right, local)
                    if left is not None and right is not None:
                        folded = _fold([left, right])
                        corpus.add_many(folded)
                    elif left is not None and isinstance(node.right, ast.Name) and node.right.id in param_names:
                        for value in left:
                            if value and value[-1] in SEPARATORS:
                                param_prefixes.setdefault(node.right.id, set()).add(value)
                                if _good_prefix(value):
                                    corpus.prefixes.add(value)
                    elif right is not None and isinstance(node.left, ast.Name) and node.left.id in param_names:
                        for value in right:
                            if value[:1] in SEPARATORS:
                                param_prefixes.setdefault(node.left.id, set()).add(value)
                self.generic_visit(node)

            def visit_Constant(self, node: ast.Constant) -> None:
                if isinstance(node.value, str):
                    corpus.add(node.value)

        Visitor().visit(ast.Module(body=body, type_ignores=[]))
        if func_name:
            current = functions.get(func_name)
            if current is None:
                functions[func_name] = (params, param_prefixes)
            else:
                _, existing = current
                for name, values in param_prefixes.items():
                    existing.setdefault(name, set()).update(values)

    walk(tree.body, module_env, [], "")
    corpus.functions = functions
    _bind_calls(tree, functions, corpus)
    return module_env


def _bind_calls(tree: ast.AST, functions: dict[str, tuple[list[str], dict[str, set[str]]]], corpus: Corpus) -> None:
    env = dict(_BASE_ENV)

    def apply(param: str, arg: ast.AST, prefixes: dict[str, set[str]]) -> None:
        if param not in prefixes:
            return
        for prefix in prefixes[param]:
            for value in _eval(arg, env) or set():
                corpus.add(prefix + value)

    class Binder(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            name = _call_name(node)
            if name in ENV_CALLS and node.args:
                for value in _eval(node.args[0], env) or set():
                    corpus.add_many(_both_env(value))
            recorded = functions.get(name or "")
            if recorded is not None:
                params, prefixes = recorded
                for index, arg in enumerate(node.args):
                    if index < len(params):
                        apply(params[index], arg, prefixes)
                for keyword in node.keywords:
                    if keyword.arg:
                        apply(keyword.arg, keyword.value, prefixes)
            self.generic_visit(node)

    Binder().visit(tree)


def _skipped(relative: Path) -> bool:
    return any(part in SKIP_DIRS or part.endswith(".egg-info") for part in relative.parts)


def _read(path: Path) -> str | None:
    data = path.read_bytes()
    if b"\0" in data[:1024]:
        return None
    return data.decode("utf-8", errors="ignore")


def _code_corpus(root: Path) -> Corpus:
    corpus = Corpus()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if _skipped(relative):
            continue
        posix = relative.as_posix()
        if any(stem in path.name for stem in _STEMS):
            corpus.add(path.name)
        if posix in GROUND_SKIP:
            continue
        if path.name == "Dockerfile" or path.suffix in CODE_SUFFIXES:
            text = _read(path)
            if text is None:
                continue
            if path.suffix == ".py":
                _walk_python(path, corpus)
            else:
                for match in IDENTIFIER.finditer(text):
                    # Database roles come from the Compose service, not a CI probe.
                    if match.lastgroup == "role":
                        continue
                    corpus.add(match.group(0))
                if "QA_${" in text:
                    for suffix in QA_CALL.findall(text):
                        for prefix in _ENV_PREFIXES:
                            corpus.add(prefix + "QA_" + suffix)
                if path.name.startswith("compose") and path.suffix in {".yaml", ".yml"} and relative.parent == Path("."):
                    for kind, value in POSTGRES.findall(text):
                        corpus.add(("-U " if kind == "USER" else "-d ") + value)
    return corpus


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def code_spans(text: str) -> list[tuple[int, str]]:
    spans: list[tuple[int, str]] = []
    occupied: list[tuple[int, int]] = []
    for match in FENCE.finditer(text):
        occupied.append((match.start(), match.end()))
        spans.append((_line(text, match.start("body")), html.unescape(match.group("body"))))
    for match in INLINE.finditer(text):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        spans.append((_line(text, match.start()), html.unescape(match.group(1))))
    return spans


def identifiers(text: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for line, span in code_spans(text):
        if re.fullmatch(r"[A-Za-z0-9.-]+\.(?:com|org|net|io)", span.strip()):
            continue
        for match in IDENTIFIER.finditer(span):
            token = match.group(0).rstrip(".,;:")
            if token:
                found.append((line, token))
    return found


def _legacy_tokens(root: Path) -> set[str]:
    text = (root / "docs" / "BRANDING.md").read_text(encoding="utf-8")
    if LEGACY_HEADING not in text:
        return set()
    section = text.split(LEGACY_HEADING, 1)[1]
    return {token for _, token in identifiers(section)}


def _swap(token: str) -> str | None:
    """The other generation of one renamed token, prefix before stem."""
    for old, new in (
        (LEGACY_ENV_PREFIX, CURRENT_ENV_PREFIX),
        (CURRENT_ENV_PREFIX, LEGACY_ENV_PREFIX),
        (LEGACY_HEADER_PREFIX, CURRENT_HEADER_PREFIX),
        (CURRENT_HEADER_PREFIX, LEGACY_HEADER_PREFIX),
        (LEGACY_STEM, CURRENT_STEM),
        (CURRENT_STEM, LEGACY_STEM),
    ):
        if old in token:
            return token.replace(old, new, 1)
    return None


def _contract(token: str) -> bool:
    """Names the running code stores or matches. Operator-chosen examples are not."""
    if token.startswith(("-U ", "-d ")) or token.startswith(_ENV_PREFIXES) or token.startswith(_HEADER_PREFIXES):
        return True
    if re.match(r"\d+\.\d+\.\d+\+", token):
        return True
    if token.endswith((".env", ".keytab")) or "managed-storage" in token:
        return True
    if re.search(r"-v\d+$", token) or "question-draft" in token:
        return True
    stems = "|".join(re.escape(stem) for stem in _STEMS)
    return re.match(rf"^(?:{stems})\.[a-z]+[A-Z]", token) is not None


def _path_supported(token: str, corpus: Corpus, legacy: set[str]) -> bool:
    for item in corpus.exact | legacy:
        if not item.startswith("/"):
            continue
        directory = item.rstrip("/")
        if token == item or token.startswith(directory + "/"):
            return True
    return False


def _allows(token: str, corpus: Corpus, legacy: set[str]) -> bool:
    if token in SCHEMATIC_ENV or token in corpus.exact or token in legacy:
        return True
    pool = corpus.exact | legacy
    if token.endswith("*"):
        base = token[:-1]
        if _good_prefix(base) and any(item.startswith(base) for item in pool):
            return True
    if "<" in token:
        base = token.split("<", 1)[0]
        if base in corpus.prefixes or base in legacy:
            return True
        if any(item.startswith(base) for item in pool) and len(base) > len(CURRENT_STEM) + 1:
            return True
    if token.startswith("/"):
        if _path_supported(token, corpus, legacy):
            return True
        swapped = _swap(token)
        return not bool(swapped and _path_supported(swapped, corpus, legacy))
    if _contract(token):
        return False
    swapped = _swap(token)
    return not bool(swapped and swapped in pool)


def findings_in_text(name: str, text: str, corpus: Corpus, legacy: set[str]) -> list[Finding]:
    seen: set[str] = set()
    found: list[Finding] = []
    for line, token in identifiers(text):
        if token in seen or _allows(token, corpus, legacy):
            continue
        seen.add(token)
        found.append(Finding(f"{name}:{line}", token))
    return found


_CACHE: dict[str, tuple[Corpus, set[str]]] = {}


def load(root: Path | None = None) -> tuple[Corpus, set[str]]:
    root = (root or ROOT).resolve()
    key = str(root)
    if key not in _CACHE:
        if LEGACY_HEADING not in (root / "docs" / "BRANDING.md").read_text(encoding="utf-8"):
            raise RuntimeError("docs/BRANDING.md is missing its compatibility section.")
        _CACHE[key] = (_code_corpus(root), _legacy_tokens(root))
    return _CACHE[key]


def findings(root: Path | None = None) -> list[Finding]:
    root = (root or ROOT).resolve()
    corpus, legacy = load(root)
    if not legacy:
        return [Finding("docs/BRANDING.md", LEGACY_HEADING)]
    found: list[Finding] = []
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root)
        if _skipped(relative) or path.name == "CHANGELOG.md":
            continue
        text = _read(path)
        if text is None:
            continue
        found.extend(findings_in_text(relative.as_posix(), text, corpus, legacy))
    return found


def main() -> int:
    found = findings()
    if found:
        print("DOC IDENTIFIER GATE: BLOCKED", file=sys.stderr)
        for item in found:
            print(f"  {item}", file=sys.stderr)
        return 1
    print("DOC IDENTIFIER GATE: CLEAN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
