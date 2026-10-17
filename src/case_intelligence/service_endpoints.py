"""Strict operator-configured boundaries for private companion services.

Exculpata accepts service URLs only from process configuration, never from a
browser request. Even so, configuration should not silently turn the
application into an unrestricted network client. Loopback is always permitted;
container DNS names or appliance addresses must be named in an exact allowlist.
"""
from __future__ import annotations

import ipaddress
import os
import re
from urllib.parse import urlparse


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_DNS_NAME = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)


def configured_service_hosts(environment_name: str) -> frozenset[str]:
    """Return loopback plus a bounded exact host allowlist from one setting."""

    values = {
        value.strip().casefold()
        for value in os.getenv(environment_name, "").split(",")
        if value.strip()
    }
    if len(values) > 32:
        raise ValueError(f"{environment_name} contains too many service hosts")
    for value in values:
        if len(value) > 253 or any(character in value for character in "/@?#"):
            raise ValueError(f"{environment_name} contains an invalid service host")
        try:
            ipaddress.ip_address(value)
        except ValueError:
            if _DNS_NAME.fullmatch(value) is None:
                raise ValueError(
                    f"{environment_name} contains an invalid service host"
                )
    return frozenset((*_LOOPBACK_HOSTS, *values))


def service_host_allowed(hostname: str | None, environment_name: str) -> bool:
    return bool(
        hostname
        and hostname.casefold() in configured_service_hosts(environment_name)
    )


def validate_service_endpoint(
    endpoint: str,
    *,
    environment_name: str,
    label: str,
    schemes: frozenset[str] = frozenset({"http", "https"}),
    origin_only: bool = False,
) -> str:
    """Validate an exact private service URL without resolving or contacting it."""

    value = (endpoint or "").strip().rstrip("/")
    parsed = urlparse(value)
    if (
        parsed.scheme not in schemes
        or not service_host_allowed(parsed.hostname, environment_name)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (origin_only and parsed.path not in {"", "/"})
        or any(part == ".." for part in parsed.path.split("/"))
    ):
        raise ValueError(
            f"{label} must use an exact host allowed by {environment_name}"
        )
    return value
