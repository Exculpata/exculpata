#!/usr/bin/env python3
"""Exculpata entry point for the generation-aware backup and restore tool."""
import sys
import recordbench_backup as _backup

if __name__ == "__main__":
    raise SystemExit(_backup.main())

sys.modules[__name__] = _backup
