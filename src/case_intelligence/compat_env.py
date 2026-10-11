"""Compatibility for the Exculpata environment and HTTP header names."""
from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping

_LOG = logging.getLogger(__name__)
_warned: set[str] = set()
_conflicts: set[str] = set()
_lock = threading.Lock()


def env(name: str, default: str | None = None, *, environ: Mapping[str, str] | None = None) -> str | None:
    """Read either prefix, preferring a nonempty new name, like Compose ``:-``.

    Warnings contain names only. Deprecation and conflicting values are each
    reported once per setting per process, including under concurrent reads.
    """
    suffix = name.removeprefix("RECORDBENCH_").removeprefix("EXCULPATA_")
    new, old = f"EXCULPATA_{suffix}", f"RECORDBENCH_{suffix}"
    source = os.environ if environ is None else environ
    new_value, old_value = source.get(new), source.get(old)
    with _lock:
        if old_value and old not in _warned:
            _warned.add(old)
            _LOG.warning("%s is deprecated; use %s", old, new)
        if new_value and old_value and new_value != old_value and old not in _conflicts:
            _conflicts.add(old)
            _LOG.warning("%s and %s differ; using %s", new, old, new)
    return new_value or old_value or default


def required_env(name: str, *, environ: Mapping[str, str]) -> str:
    """Preserve required-key failures when reading saved node coordinates."""
    value = env(name, environ=environ)
    if value is None:
        raise KeyError(name)
    return value


def header_conflicts(headers: Mapping[str, str], legacy: str) -> bool:
    new = legacy.replace("X-RecordBench-", "X-Exculpata-", 1)
    return new in headers and legacy in headers and headers[new] != headers[legacy]


def header(headers: Mapping[str, str], legacy: str) -> str | None:
    """Accept either header; ambiguous proof never grants authority."""
    if header_conflicts(headers, legacy):
        return None
    new = legacy.replace("X-RecordBench-", "X-Exculpata-", 1)
    return headers[new] if new in headers else headers.get(legacy)


def kerberos_headers(headers: Mapping[str, str]) -> tuple[str | None, str | None]:
    return (header(headers, "X-RecordBench-Authenticated-User"),
            header(headers, "X-RecordBench-Proxy-Secret"))


def response_headers(suffix: str, value: str) -> dict[str, str]:
    return {f"X-Exculpata-{suffix}": value, f"X-RecordBench-{suffix}": value}
