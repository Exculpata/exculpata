"""Keep the internal module registry fixed and within its dependency layer."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from case_intelligence.modules import Module
from case_intelligence.modules.registry import REGISTERED, lookup


ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "src" / "case_intelligence"
LAYERS = json.loads(
    (ROOT / "tests" / "snapshots" / "import_layers.json").read_text(encoding="utf-8")
)


def test_registered_modules_are_a_tuple_with_unique_names() -> None:
    assert isinstance(REGISTERED, tuple)
    names = [module.name for module in REGISTERED]
    assert len(names) == len(set(names))


def test_every_registered_module_has_a_module_layer() -> None:
    assert all(isinstance(module, Module) for module in REGISTERED)
    assert all(module.layer.startswith("modules.") for module in REGISTERED)


def test_registry_lookup_uses_the_fixed_registry() -> None:
    assert lookup("not-registered") is None


def test_no_core_module_imports_module_registry_package() -> None:
    for module_name, layer in LAYERS.items():
        if not layer.startswith("core."):
            continue
        relative = module_name.removeprefix("case_intelligence.").replace(".", "/")
        path = PACKAGE / f"{relative}.py"
        if not path.exists():
            path = PACKAGE / relative / "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level == 0:
                imported.add(node.module or "")
                continue
            package_parts = module_name.split(".")[:-1]
            if path.name == "__init__.py":
                package_parts = module_name.split(".")
            prefix = package_parts[: len(package_parts) - (node.level - 1)]
            imported.add(".".join([*prefix, node.module] if node.module else prefix))
        assert not any(
            name == "case_intelligence.modules"
            or name.startswith("case_intelligence.modules.")
            for name in imported
        ), module_name
