"""T9: explicit live GPU run; informational only, never a model gate."""

import pytest

from tests.model_canary_live import preflight, run_canary


@pytest.mark.timeout(93600)
def test_t9_model_canary_live(tmp_path):
    receipt = run_canary(preflight(), tmp_path)
    assert receipt["status"] == "passed", receipt
