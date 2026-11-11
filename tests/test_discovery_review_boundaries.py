"""Exercise the unchanged layer checker and inspect the new package boundary."""
import ast
import json

import pytest

from tests.test_import_boundaries import (
    LAYER_SNAPSHOT, PACKAGE, _check, _resolve_imports, _synthetic_baseline, _write_module,
)


def test_package_contract_and_isolation():
    layers = json.loads(LAYER_SNAPSHOT.read_text())
    assert layers["case_intelligence.modules"] == "core.contracts"
    assert layers["case_intelligence.modules.registry"] == "app_shell"
    paths = list((PACKAGE / "modules" / "discovery_review").glob("*.py"))
    assert len(paths) >= 3
    contract = PACKAGE / "modules" / "__init__.py"
    assert not _resolve_imports("case_intelligence.modules", contract, set(layers))
    for path in paths:
        name = "case_intelligence.modules.discovery_review"
        if path.stem != "__init__":
            name += "." + path.stem
        assert layers[name] == "modules.discovery_review"
        imports = _resolve_imports(name, path, set(layers))
        assert not imports & {"case_intelligence.workbench", "case_intelligence.workspace_store"}
        tree = ast.parse(path.read_text())
        assert not [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr.startswith("_")]
        constants = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id.isupper()]
        assert not [name for name in constants if "FIELD" in name or "COLUMN" in name]


@pytest.mark.parametrize("target, layer", [
    ("registry", "app_shell"), ("workbench", "app_shell"), ("other_feature", "modules.answers"),
])
def test_feature_import_rejected_by_unchanged_checker(tmp_path, target, layer):
    package = tmp_path / "synthetic"
    _write_module(package, "discovery_review")
    _write_module(package, target)
    layers = {"synthetic.discovery_review": "modules.discovery_review", f"synthetic.{target}": layer}
    baseline = _synthetic_baseline(package, layers)
    _write_module(package, "discovery_review", f"from synthetic import {target}\n")
    with pytest.raises(AssertionError, match="New forbidden import edges"):
        _check(package, layers, baseline)
