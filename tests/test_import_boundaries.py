"""Ratchet the package toward the import boundaries in ADR 0001."""

from __future__ import annotations

import ast
import json
import warnings
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "src" / "case_intelligence"
SNAPSHOTS = ROOT / "tests" / "snapshots"
LAYER_SNAPSHOT = SNAPSHOTS / "import_layers.json"
GRAPH_SNAPSHOT = SNAPSHOTS / "import_graph.json"


def _modules(package: Path) -> dict[str, Path]:
    modules = {}
    for path in package.rglob("*.py"):
        parts = list(path.relative_to(package.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        modules[".".join(parts)] = path
    return modules


def _resolve_imports(module: str, path: Path, known: set[str]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    package_parts = module.split(".")[:-1]
    if path.name == "__init__.py":
        package_parts = module.split(".")
    for node in ast.walk(tree):
        candidates: list[str] = []
        if isinstance(node, ast.Import):
            candidates.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                prefix = package_parts[: len(package_parts) - (node.level - 1)]
                base = ".".join([*prefix, node.module] if node.module else prefix)
            else:
                base = node.module or ""
            imported_modules = [
                f"{base}.{alias.name}"
                for alias in node.names
                if alias.name != "*" and f"{base}.{alias.name}" in known
            ]
            candidates.extend(imported_modules or [base])
        for candidate in candidates:
            parts = candidate.split(".")
            while parts:
                target = ".".join(parts)
                if target in known:
                    found.add(target)
                    break
                parts.pop()
    return found


def _graph(package: Path) -> dict[str, set[str]]:
    modules = _modules(package)
    return {
        module: _resolve_imports(module, path, set(modules))
        for module, path in modules.items()
    }


def _components(graph: dict[str, set[str]]) -> list[list[str]]:
    """Return Tarjan strongly connected components in stable order."""
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[list[str]] = []

    def connect(node: str) -> None:
        nonlocal index
        indices[node] = lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(graph[node]):
            if target not in indices:
                connect(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                target = stack.pop()
                on_stack.remove(target)
                component.append(target)
                if target == node:
                    break
            result.append(sorted(component))

    for node in sorted(graph):
        if node not in indices:
            connect(node)
    return sorted(result)


def _forbidden(source: str, target: str, layers: dict[str, str]) -> bool:
    source_layer = layers[source]
    target_layer = layers[target]
    if source_layer in {"app_shell", "evals"}:
        return False
    if source_layer.startswith("core."):
        return not target_layer.startswith("core.")
    if source_layer.startswith("modules."):
        return not (
            target_layer.startswith("core.") or target_layer == source_layer
        )
    raise AssertionError(f"Unknown layer for {source}: {source_layer}")


def _violations(graph: dict[str, set[str]], layers: dict[str, str]) -> set[str]:
    return {
        f"{source} -> {target}"
        for source, targets in graph.items()
        for target in targets
        if _forbidden(source, target, layers)
    }


def _check(
    package: Path, layers: dict[str, str], baseline: dict[str, object]
) -> None:
    graph = _graph(package)
    assert set(graph) == set(layers), "Map every Python module to exactly one layer"

    current = _violations(graph, layers)
    allowed = set(baseline["forbidden_edges"])
    added = sorted(current - allowed)
    assert not added, "New forbidden import edges:\n" + "\n".join(added)
    removed = sorted(allowed - current)
    if removed:
        warnings.warn(
            "Forbidden edges were removed; drop them from the baseline (never add):\n"
            + "\n".join(removed),
            stacklevel=2,
        )

    previous_cycles = [set(component) for component in baseline["cycles"]]
    current_components = _components(graph)
    previous_largest = max(baseline["component_sizes"], default=0)
    current_largest = max(map(len, current_components), default=0)
    assert current_largest <= previous_largest, (
        f"Largest import cycle grew from {previous_largest} to {current_largest}"
    )
    new_cycles = [
        component
        for component in current_components
        if len(component) > 1
        and not any(set(component).issubset(old) for old in previous_cycles)
    ]
    assert not new_cycles, f"New import cycles: {new_cycles}"


def _write_module(package: Path, name: str, source: str = "") -> None:
    path = package.joinpath(*name.split(".")).with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")  # fixture-write: synthetic package


def _synthetic_baseline(package: Path, layers: dict[str, str]) -> dict[str, object]:
    graph = _graph(package)
    components = _components(graph)
    return {
        "forbidden_edges": sorted(_violations(graph, layers)),
        "component_sizes": sorted(map(len, components), reverse=True),
        "cycles": [component for component in components if len(component) > 1],
    }


def test_new_core_to_module_edge_fails(tmp_path: Path) -> None:
    package = tmp_path / "sample"
    _write_module(package, "core")
    _write_module(package, "feature")
    layers = {"sample.core": "core.storage", "sample.feature": "modules.answers"}
    baseline = _synthetic_baseline(package, layers)
    _write_module(package, "core", "from sample import feature\n")

    with pytest.raises(AssertionError, match="New forbidden import edges"):
        _check(package, layers, baseline)


def test_new_cycle_fails(tmp_path: Path) -> None:
    package = tmp_path / "sample"
    _write_module(package, "first")
    _write_module(package, "second", "from sample import first\n")
    layers = {"sample.first": "core.storage", "sample.second": "core.access"}
    baseline = _synthetic_baseline(package, layers)
    _write_module(package, "first", "from sample import second\n")

    with pytest.raises(AssertionError, match="import cycle|import cycles"):
        _check(package, layers, baseline)


def test_removed_forbidden_edge_passes_with_baseline_message(tmp_path: Path) -> None:
    package = tmp_path / "sample"
    _write_module(package, "core", "from sample import feature\n")
    _write_module(package, "feature")
    layers = {"sample.core": "core.storage", "sample.feature": "modules.answers"}
    baseline = _synthetic_baseline(package, layers)
    _write_module(package, "core")

    with pytest.warns(UserWarning, match=r"drop them from the baseline \(never add\)"):
        _check(package, layers, baseline)


def test_application_import_boundaries() -> None:
    layers = json.loads(LAYER_SNAPSHOT.read_text(encoding="utf-8"))
    baseline = json.loads(GRAPH_SNAPSHOT.read_text(encoding="utf-8"))
    _check(PACKAGE, layers, baseline)
