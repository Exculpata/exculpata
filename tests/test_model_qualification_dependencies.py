"""The stdlib scorecard coordinator does not need qualification validation."""
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_defense_eval_runs_without_site_packages():
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", """
import sys
sys.path.insert(0, sys.argv[1])
from case_intelligence import defense_eval
suite = defense_eval.load_suite()
card = defense_eval.run_eval(suite, defense_eval.ReferenceStub())
assert card["aggregates"]["questions"] == len(suite.questions)
assert card["aggregates"]["errors"] == 0
assert "jsonschema" not in sys.modules
""", str(ROOT / "src")],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("operation", [
    "qualification_schema()",
    "load_qualifications()",
    "apply_harness_result({}, model_id='synthetic', harness='synthetic', passed=False)",
])
def test_qualification_validation_requires_jsonschema(operation):
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", """
import sys
sys.path.insert(0, sys.argv[1])
from case_intelligence import model_qualification
try:
    eval("model_qualification." + sys.argv[2])
except ModuleNotFoundError as error:
    assert error.name == "jsonschema"
else:
    raise AssertionError("Qualification validation must require jsonschema")
""", str(ROOT / "src"), operation],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_qualification_validation_still_rejects_invalid_documents(tmp_path):
    from jsonschema import SchemaError, ValidationError
    from case_intelligence.model_qualification import (
        apply_harness_result, load_qualifications, qualification_schema,
    )

    schema = tmp_path / "schema.json"
    # fixture-write: Invalid synthetic schema exercises the real validator.
    schema.write_text('{"type": "synthetic-invalid-type"}')
    with pytest.raises(SchemaError):
        qualification_schema(schema)
    registry = tmp_path / "registry.json"
    # fixture-write: Invalid synthetic registry exercises the real validator.
    registry.write_text('{"schema_version": 1, "qualifications": [{}]}')
    with pytest.raises(ValidationError):
        load_qualifications(registry)
    with pytest.raises(ValidationError):
        apply_harness_result(
            {"qualifications": [{}]}, model_id="synthetic", harness="synthetic", passed=False,
        )
