"""Previous product spelling, isolated so the rest of the tree can avoid it.

Fresh installs and staff-facing copy use Exculpata. Existing nodes, applied
database functions, and clients that already send the previous names keep
working through the constants in this module.
"""
from __future__ import annotations

import logging
import threading

LEGACY_STEM = "recordbench"
LEGACY_PRODUCT = "RecordBench"
LEGACY_ENV_PREFIX = "RECORDBENCH_"
LEGACY_ENV_NAME = LEGACY_ENV_PREFIX[:-1]
LEGACY_HEADER_PREFIX = "X-RecordBench-"
LEGACY_GIT_PREFIX = "recordbench."

CURRENT_STEM = "exculpata"
CURRENT_PRODUCT = "Exculpata"
CURRENT_ENV_PREFIX = "EXCULPATA_"
CURRENT_HEADER_PREFIX = "X-Exculpata-"
CURRENT_GIT_PREFIX = "exculpata."

_LOG = logging.getLogger(__name__)
_warned: set[str] = set()
_lock = threading.Lock()


def warn_legacy(old: str, new: str) -> None:
    """Log a content-free deprecation once per name per process."""
    with _lock:
        if old in _warned:
            return
        _warned.add(old)
    _LOG.warning("%s is deprecated; use %s", old, new)


def sql_function(suffix: str) -> str:
    """Applied SQLite function name. The stored name is unchanged."""
    return f"{LEGACY_STEM}_{suffix}"


def accepts_product(value: object) -> bool:
    """Accept either product string. Malformed JSON values fail closed."""
    return isinstance(value, str) and value in (LEGACY_PRODUCT, CURRENT_PRODUCT)
