"""Fail-closed integrity checks for the frozen synthetic Review acceptance pack."""
from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Mapping, Sequence


SHA256_RE = re.compile(r"[0-9a-f]{64}")


class PackIntegrityError(ValueError):
    """The frozen acceptance manifest or one of its declared inputs changed."""


def canonical_sha256(value: object) -> str:
    body = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def safe_repo_path(root: Path, relative_path: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise PackIntegrityError("acceptance path is empty")
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise PackIntegrityError(f"acceptance path escapes the repository: {relative_path}")
    repository = root.resolve()
    try:
        resolved = (repository / relative).resolve(strict=True)
    except OSError as exc:
        raise PackIntegrityError(f"acceptance input is missing: {relative_path}") from exc
    if not resolved.is_relative_to(repository) or not resolved.is_file():
        raise PackIntegrityError(f"acceptance path is not a repository file: {relative_path}")
    return resolved


def file_sha256(root: Path, relative_path: str) -> str:
    return hashlib.sha256(safe_repo_path(root, relative_path).read_bytes()).hexdigest()


def _selected_node(tree: ast.AST, selector: str, node_id: str) -> ast.AST:
    body: Sequence[ast.stmt] = getattr(tree, "body", ())
    selected: ast.AST | None = None
    for raw_part in selector.split("::"):
        name = raw_part.split("[", 1)[0]
        matches = [
            item
            for item in body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and item.name == name
        ]
        if len(matches) != 1:
            raise PackIntegrityError(f"acceptance test node is unresolved: {node_id}")
        selected = matches[0]
        body = getattr(selected, "body", ())
    if selected is None:
        raise PackIntegrityError(f"acceptance test node is unresolved: {node_id}")
    return selected


def test_node_bytes(root: Path, node_id: str) -> bytes:
    if not isinstance(node_id, str) or not node_id.strip():
        raise PackIntegrityError("acceptance test node is empty")
    path_text, separator, selector = node_id.partition("::")
    path = safe_repo_path(root, path_text)
    raw = path.read_bytes()
    if not separator:
        return raw
    if not selector:
        raise PackIntegrityError(f"acceptance test node is malformed: {node_id}")
    try:
        source = raw.decode("utf-8")
        tree = ast.parse(source, filename=path_text)
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise PackIntegrityError(f"acceptance test source cannot be parsed: {node_id}") from exc
    selected = _selected_node(tree, selector, node_id)
    start = min(
        [selected.lineno]
        + [decorator.lineno for decorator in getattr(selected, "decorator_list", ())]
    )
    end = getattr(selected, "end_lineno", None)
    if end is None:
        raise PackIntegrityError(f"acceptance test node has no source span: {node_id}")
    lines = source.splitlines(keepends=True)
    return "".join(lines[start - 1 : end]).encode("utf-8")


def test_node_sha256(root: Path, node_id: str) -> str:
    return hashlib.sha256(test_node_bytes(root, node_id)).hexdigest()


def _string_tuple(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PackIntegrityError(f"{label} must be a list of paths")
    return tuple(value)


def referenced_test_nodes(payload: Mapping[str, object]) -> tuple[str, ...]:
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise PackIntegrityError("acceptance cases are missing")
    nodes: list[str] = []
    for case in cases:
        if not isinstance(case, Mapping):
            raise PackIntegrityError("acceptance case is malformed")
        case_nodes = _string_tuple(case.get("node_ids"), label="case node_ids")
        if not case_nodes or len(case_nodes) != len(set(case_nodes)):
            raise PackIntegrityError("acceptance case nodes must be nonempty and unique")
        nodes.extend(case_nodes)
    return tuple(sorted(set(nodes)))


def referenced_test_files(payload: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(sorted({node.partition("::")[0] for node in referenced_test_nodes(payload)}))


def referenced_fixture_files(payload: Mapping[str, object]) -> tuple[str, ...]:
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise PackIntegrityError("acceptance cases are missing")
    paths: set[str] = set()
    for case in cases:
        if not isinstance(case, Mapping):
            raise PackIntegrityError("acceptance case is malformed")
        fixture_files = case.get("fixture_files", [])
        paths.update(_string_tuple(fixture_files, label="case fixture_files"))
        scenario_path = case.get("scenario_path")
        if scenario_path is not None:
            if not isinstance(scenario_path, str) or not scenario_path:
                raise PackIntegrityError("acceptance scenario_path is malformed")
            paths.add(scenario_path)
    return tuple(sorted(paths))


def _digest_mapping(value: object, *, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise PackIntegrityError(f"acceptance {label} map is missing")
    result: dict[str, str] = {}
    for key, digest in value.items():
        if (
            not isinstance(key, str)
            or not isinstance(digest, str)
            or SHA256_RE.fullmatch(digest) is None
        ):
            raise PackIntegrityError(f"acceptance {label} entry is malformed")
        result[key] = digest
    return result


def verify_pack_integrity(root: Path, payload: Mapping[str, object]) -> str:
    """Verify manifest, test-node, helper-file, and fixture bytes before execution."""

    cases = payload.get("cases")
    if payload.get("case_fingerprint") != canonical_sha256(cases):
        raise PackIntegrityError("acceptance case fingerprint does not match")
    integrity = payload.get("integrity")
    if not isinstance(integrity, Mapping) or integrity.get("algorithm") != "sha256":
        raise PackIntegrityError("acceptance integrity declaration is missing")

    node_digests = _digest_mapping(
        integrity.get("test_node_sha256"), label="test-node digest"
    )
    file_digests = _digest_mapping(
        integrity.get("test_file_sha256"), label="test-file digest"
    )
    fixture_digests = _digest_mapping(
        integrity.get("fixture_file_sha256"), label="fixture digest"
    )
    expected_nodes = referenced_test_nodes(payload)
    expected_test_files = referenced_test_files(payload)
    expected_fixtures = referenced_fixture_files(payload)
    if tuple(sorted(node_digests)) != expected_nodes:
        raise PackIntegrityError("acceptance test-node digest set does not match cases")
    if tuple(sorted(file_digests)) != expected_test_files:
        raise PackIntegrityError("acceptance test-file digest set does not match cases")
    if tuple(sorted(fixture_digests)) != expected_fixtures:
        raise PackIntegrityError("acceptance fixture digest set does not match cases")

    for node_id, expected in node_digests.items():
        if test_node_sha256(root, node_id) != expected:
            raise PackIntegrityError(f"acceptance test node changed: {node_id}")
    for relative_path, expected in file_digests.items():
        if file_sha256(root, relative_path) != expected:
            raise PackIntegrityError(f"acceptance test file changed: {relative_path}")
    for relative_path, expected in fixture_digests.items():
        if file_sha256(root, relative_path) != expected:
            raise PackIntegrityError(f"acceptance fixture changed: {relative_path}")

    content = {
        "manifest": {key: value for key, value in payload.items() if key != "integrity"},
        "algorithm": integrity["algorithm"],
        "test_node_sha256": node_digests,
        "test_file_sha256": file_digests,
        "fixture_file_sha256": fixture_digests,
    }
    actual_fingerprint = canonical_sha256(content)
    if integrity.get("content_fingerprint") != actual_fingerprint:
        raise PackIntegrityError("acceptance content fingerprint does not match")
    return actual_fingerprint
