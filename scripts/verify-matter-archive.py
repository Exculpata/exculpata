#!/usr/bin/env python3
"""Verify a preservation ZIP offline without extracting its contents."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from case_intelligence.matter_archive_verifier import main

if __name__ == '__main__':
    raise SystemExit(main())
