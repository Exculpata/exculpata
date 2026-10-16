from pathlib import Path
import os
import runpy


def test_synthetic_browser_environment_removes_host_backends(monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    namespace = runpy.run_path(str(Path(__file__).parents[1] / "scripts/synthetic_browser_environment.py"))
    for key in ("CASE_INTELLIGENCE_POSTGRES_DSN", "CASE_INTELLIGENCE_POSTGRES_DSN_FILE",
                "CASE_INTELLIGENCE_MANAGED_STORAGE_ROOT", "CASE_INTELLIGENCE_TRANSCRIPTION_URL",
                "CASE_INTELLIGENCE_CLAMAV_HOST", "CASE_REVIEW_ENABLE_MODELS",
                "CASE_REVIEW_POSTGRES_DSN", "RECORDBENCH_LOCAL_ACCOUNT_ROOT"):
        monkeypatch.setenv(key, "synthetic-inherited-value")
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "12")
    monkeypatch.setenv("RECORDBENCH_QA_BROWSER_CHANNEL", "chrome")
    namespace["isolate_environment"]()
    assert {k: v for k, v in os.environ.items() if k.startswith(("CASE_INTELLIGENCE_", "CASE_REVIEW_"))} == {
        "CASE_INTELLIGENCE_STORAGE_RESERVE_GIB": "0",
        "CASE_INTELLIGENCE_TEST_MODE": "1"}
    assert "RECORDBENCH_LOCAL_ACCOUNT_ROOT" not in os.environ
    assert os.environ["RECORDBENCH_QA_BROWSER_CHANNEL"] == "chrome"
