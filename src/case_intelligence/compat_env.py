"""Compatibility for the Exculpata environment and HTTP header names."""
from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping

from case_intelligence.legacy_names import (
    CURRENT_ENV_PREFIX,
    CURRENT_HEADER_PREFIX,
    LEGACY_ENV_PREFIX,
    LEGACY_HEADER_PREFIX,
)

_LOG = logging.getLogger(__name__)
_warned: set[str] = set()
_conflicts: set[str] = set()
_lock = threading.Lock()


def _suffix(name: str) -> str:
    for prefix in (CURRENT_ENV_PREFIX, LEGACY_ENV_PREFIX):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _warn_once(old: str, new: str) -> None:
    with _lock:
        if old in _warned:
            return
        _warned.add(old)
    _LOG.warning("%s is deprecated; use %s", old, new)


def env(name: str, default: str | None = None, *, environ: Mapping[str, str] | None = None) -> str | None:
    """Read either prefix, preferring a nonempty new name, like Compose ``:-``.

    Warnings contain names only. Deprecation and conflicting values are each
    reported once per setting per process, including under concurrent reads.
    Conflicting OIDC issuer and egress pin aliases are refused on every read.
    """
    suffix = _suffix(name)
    new, old = f"{CURRENT_ENV_PREFIX}{suffix}", f"{LEGACY_ENV_PREFIX}{suffix}"
    source = os.environ if environ is None else environ
    new_value, old_value = source.get(new), source.get(old)
    if suffix in {"OIDC_ISSUER", "OIDC_EGRESS_PINS"} and new_value and old_value and new_value != old_value:
        raise RuntimeError(f"{new} and {old} differ")
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
    new = str(legacy).replace(LEGACY_HEADER_PREFIX, CURRENT_HEADER_PREFIX, 1)
    return new in headers and legacy in headers and headers[new] != headers[legacy]


def header(headers: Mapping[str, str], legacy: str) -> str | None:
    """Accept either header; ambiguous proof never grants authority."""
    if legacy in headers:
        _warn_once(legacy, str(legacy).replace(LEGACY_HEADER_PREFIX, CURRENT_HEADER_PREFIX, 1))
    if header_conflicts(headers, legacy):
        return None
    new = str(legacy).replace(LEGACY_HEADER_PREFIX, CURRENT_HEADER_PREFIX, 1)
    return headers[new] if new in headers else headers.get(legacy)


def kerberos_headers(headers: Mapping[str, str]) -> tuple[str | None, str | None]:
    return (
        header(headers, f"{LEGACY_HEADER_PREFIX}Authenticated-User"),
        header(headers, f"{LEGACY_HEADER_PREFIX}Proxy-Secret"),
    )


def response_headers(suffix: str, value: str) -> dict[str, str]:
    return {
        f"{CURRENT_HEADER_PREFIX}{suffix}": value,
        f"{LEGACY_HEADER_PREFIX}{suffix}": value,
    }
