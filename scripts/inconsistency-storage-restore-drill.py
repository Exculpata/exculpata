#!/usr/bin/env python3
"""Run the synthetic stopped-runtime backup and clean restore regression."""
from pathlib import Path
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if __name__ == '__main__':
    env = {**os.environ, 'CASE_INTELLIGENCE_STORAGE_RESERVE_GIB': '0', 'CASE_INTELLIGENCE_TEST_MODE': '1'}
    raise SystemExit(subprocess.call([sys.executable, '-m', 'pytest', '-q',
        'tests/test_inconsistency_workflow.py::test_review_persists_exports_and_clean_restore'], cwd=ROOT, env=env))
