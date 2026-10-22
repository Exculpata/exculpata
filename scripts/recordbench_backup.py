#!/usr/bin/env python3
"""Compatibility entry point for the Exculpata backup tool."""
import sys

import exculpata_backup as _backup

if __name__ == "__main__":
    print(
        "Deprecation notice: recordbench_backup.py is deprecated; use exculpata_backup.py.",
        file=sys.stderr,
    )
    raise SystemExit(_backup.main())

sys.modules[__name__] = _backup
