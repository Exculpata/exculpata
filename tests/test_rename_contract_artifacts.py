"""Generated compatibility consumers cannot drift from the source contract."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from case_intelligence.legacy_names import LEGACY_STEM, name_value

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "rename_contract_artifacts", ROOT / "scripts/rename_contract_artifacts.py"
)
assert SPEC is not None and SPEC.loader is not None
artifacts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(artifacts)


def test_generated_qa_and_documentation_match_the_contract():
    assert artifacts.findings(ROOT) == []


@pytest.mark.parametrize("relative", ["scripts/qa-env.cjs", "docs/BRANDING.md"])
def test_contract_artifact_drift_is_rejected(tmp_path, relative):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/BRANDING.md").write_text("# Synthetic branding\n")
    for path, text in artifacts.generated_artifacts(tmp_path).items():
        path.write_text(text)
    assert artifacts.findings(tmp_path) == []
    path = tmp_path / relative
    if relative.endswith(".cjs"):
        path.write_text(path.read_text().replace(" || process.env[legacy]", ""))
    else:
        path.write_text(path.read_text().replace("Aliases accept the previous name.", "Aliases discard the previous name."))
    assert artifacts.findings(tmp_path) == [relative]


def test_documentation_regeneration_preserves_surrounding_prose():
    source = "# Synthetic branding\n\n" + artifacts.DOC_START + "\nold\n" + artifacts.DOC_END + "\n\nKeep this paragraph.\n"
    rendered = artifacts.updated_documentation(source)
    assert rendered == "# Synthetic branding\n\n" + artifacts.documentation_block() + "\nKeep this paragraph.\n"
    assert artifacts.updated_documentation(rendered) == rendered


@pytest.mark.parametrize("source", [
    artifacts.DOC_START,
    artifacts.DOC_END,
    artifacts.DOC_END + artifacts.DOC_START,
    artifacts.DOC_START + artifacts.DOC_START + artifacts.DOC_END,
])
def test_documentation_with_ambiguous_markers_is_not_rewritten(source):
    with pytest.raises(ValueError, match="markers are malformed"):
        artifacts.updated_documentation(source)


def test_qa_environment_helper_preserves_both_names_and_nonempty_precedence():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the QA environment contract")
    names = [
        [suffix, name_value("environment", suffix="QA_" + suffix),
         name_value("environment", LEGACY_STEM, suffix="QA_" + suffix)]
        for suffix in ("ORIGIN", "ARTIFACTS", "BROWSER_CHANNEL", "SYNTHETIC_EXTENSION", "__proto__")
    ]
    script = r'''
const assert = require('node:assert/strict');
const qa = require(HELPER);
for (const [suffix, current, legacy] of NAMES) {
  delete process.env[current];
  delete process.env[legacy];
  assert.equal(qa(suffix), undefined);
  process.env[legacy] = 'synthetic-legacy';
  assert.equal(qa(suffix), 'synthetic-legacy');
  process.env[current] = 'synthetic-current';
  assert.equal(qa(suffix), 'synthetic-current');
  process.env[current] = '';
  assert.equal(qa(suffix), 'synthetic-legacy');
  delete process.env[legacy];
  assert.equal(qa(suffix), undefined);
  process.env[current] = 'synthetic-current';
  assert.equal(qa(suffix), 'synthetic-current');
}
'''.replace("HELPER", json.dumps(str(ROOT / "scripts/qa-env.cjs"))).replace("NAMES", json.dumps(names))
    result = subprocess.run([node, "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_both_qa_drivers_use_the_shared_helper():
    for driver in ("qa-people-browser.cjs", "qa-team-groups-browser.cjs"):
        source = (ROOT / "scripts" / driver).read_text()
        assert "const qa = require('./qa-env.cjs');" in source
        assert "qa('ORIGIN')" in source
        assert "qa('ARTIFACTS')" in source
        assert "qa('BROWSER_CHANNEL')" in source
