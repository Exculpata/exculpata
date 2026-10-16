"""Isolate standalone synthetic browser acceptance from deployment settings."""
from __future__ import annotations

import os


def isolate_environment() -> None:
    for name in tuple(os.environ):
        if name.startswith(("CASE_INTELLIGENCE_", "CASE_REVIEW_")) or (
                name.startswith("RECORDBENCH_") and not name.startswith("RECORDBENCH_QA_")):
            del os.environ[name]
    os.environ["CASE_INTELLIGENCE_STORAGE_RESERVE_GIB"] = "0"
    os.environ["CASE_INTELLIGENCE_TEST_MODE"] = "1"
