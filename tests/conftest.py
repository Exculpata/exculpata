"""Pytest process defaults for the application suite."""

from __future__ import annotations

import os

# Reserve 0 is the suite's disk escape. A real node must set this switch itself.
os.environ.setdefault("CASE_INTELLIGENCE_TEST_MODE", "1")
