#!/usr/bin/env python3
"""Compatibility entry point for the Exculpata node installer."""
import sys
import exculpata_install as _installer

if __name__ == "__main__":
    raise SystemExit(_installer.main())

# Preserve imports (including operator tooling) as well as the historical CLI.
sys.modules[__name__] = _installer
