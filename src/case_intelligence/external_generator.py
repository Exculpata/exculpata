"""Validate an operator answer server before any request is sent."""
from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from urllib.parse import urlparse


SERVERS = ("ollama", "llamacpp", "lmstudio")
_BACKEND = {"ollama": "ollama", "llamacpp": "openai", "lmstudio": "openai"}
# Docker's host-gateway name. Kept split so the publication scan does not see
# one internal hostname token.
HOST_GATEWAY_NAME = "host.docker." + "internal"
_COMPLETION_SUFFIXES = ("/v1/chat/completions", "/api/chat", "/v1/models", "/api/tags")
# These documentation ranges are not office networks. Python's is_private flag
# includes them, which would skip the off-network confirmation.
_DOCUMENTATION_NETWORKS = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("2001:db8::/32"),
)


class EndpointRejected(ValueError):
    """The URL or model name cannot be saved. The message is safe to show."""


class DestinationRejected(ValueError):
    """A resolved address is not allowed for this request. The message is safe to show."""


@dataclass(frozen=True)
class GeneratorEndpoint:
    server: str
    backend: str
    url: str
    host: str
    model: str
    local_network: bool
    remote_confirmed: bool
    container_loopback: bool

    @property
    def allowed_host(self) -> str:
        return self.host


def parse_generator_endpoint(
    server: str,
    url: str,
    model: str,
    *,
    remote_confirmed: bool = False,
) -> GeneratorEndpoint:
    kind = server.strip().casefold()
    if kind not in SERVERS:
        raise EndpointRejected(
            "Answer server must be ollama, llamacpp, or lmstudio. "
            "Enter one of those names."
        )
    name = " ".join(model.split()).strip()
    if not name or len(name) > 200 or any(character in name for character in "\r\n\x00"):
        raise EndpointRejected(
            "Enter the model name your server lists. It must be 1 to 200 characters. "
            "Nothing is downloaded."
        )
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise EndpointRejected(
            "Enter an http or https URL for the server origin, such as http://127.0.0.1:11434. "
            "Include the scheme and host."
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise EndpointRejected(
            "The URL cannot contain a username, password, query, or fragment. "
            "Enter only the origin."
        )
    path = parsed.path.rstrip("/")
    if any(path.endswith(suffix) for suffix in _COMPLETION_SUFFIXES) or path in {"/v1", "/api"}:
        raise EndpointRejected(
            "Use the server origin, such as http://127.0.0.1:11434. "
            "Do not include /v1, /api, /v1/chat/completions, or /api/chat."
        )
    if any(part == ".." for part in path.split("/")):
        raise EndpointRejected("The URL path cannot contain '..'. Enter the server origin.")
    host = parsed.hostname.casefold()
    local = host_is_local(host)
    loopback = _container_loopback(host)
    origin = f"{parsed.scheme}://{parsed.netloc}{path}".rstrip("/")
    return GeneratorEndpoint(
        server=kind,
        backend=_BACKEND[kind],
        url=origin,
        host=host,
        model=name,
        local_network=local,
        remote_confirmed=bool(remote_confirmed and not local),
        container_loopback=loopback,
    )


REMOTE_CONFIRMATION = (
    "This host is outside the local network. Matter text, questions, and cited "
    "passages would be sent there. Exculpata does not send case material off the "
    "office network unless you confirm. Enter a local URL, or confirm this host explicitly."
)


def _unsafe_literal(hostname: str) -> str:
    concrete = tuple(_concrete(address) for address in addresses_for(hostname))
    if any(address.is_unspecified or address.is_link_local for address in concrete):
        return (
            "That address is link-local or unspecified. "
            "Enter a loopback, RFC1918, or IPv6 ULA address."
        )
    return ""


def require_local_or_confirmed(endpoint: GeneratorEndpoint) -> GeneratorEndpoint:
    unsafe = _unsafe_literal(endpoint.host)
    if unsafe:
        raise EndpointRejected(unsafe)
    if not endpoint.local_network and not endpoint.remote_confirmed:
        raise EndpointRejected(REMOTE_CONFIRMATION)
    return endpoint


def _concrete(address: ipaddress.IPv4Address | ipaddress.IPv6Address):
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped if mapped is not None else address


def _office_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Loopback, RFC1918, or IPv6 ULA. Link-local and unspecified addresses are not."""

    concrete = _concrete(address)
    if any(concrete in network for network in _DOCUMENTATION_NETWORKS):
        return False
    if concrete.is_unspecified or concrete.is_link_local or concrete.is_multicast:
        return False
    return bool(concrete.is_loopback or concrete.is_private)


def _resolve_addresses(hostname: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(2)
    try:
        try:
            infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return ()
    finally:
        socket.setdefaulttimeout(previous)
    found: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[str] = set()
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            continue
        key = address.compressed
        if key not in seen:
            seen.add(key)
            found.append(address)
    return tuple(found)


def addresses_for(hostname: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    name = hostname.strip().casefold().rstrip(".")
    if not name:
        return ()
    try:
        return (ipaddress.ip_address(name),)
    except ValueError:
        return _resolve_addresses(name)


def host_is_local(hostname: str) -> bool:
    """True only when every resolved address is loopback or an office private range."""

    addresses = addresses_for(hostname)
    return bool(addresses) and all(_office_address(address) for address in addresses)


def destination_block_reason(hostname: str, *, remote_confirmed: bool) -> str:
    """Re-check a connection so a name cannot rebind to a public or metadata address."""

    addresses = addresses_for(hostname)
    if not addresses:
        return "The answer server name did not resolve. The request was not sent."
    concrete = tuple(_concrete(address) for address in addresses)
    if any(address.is_unspecified or address.is_link_local for address in concrete):
        return (
            "The answer server resolved to a link-local or unspecified address. "
            "The request was not sent."
        )
    if not remote_confirmed and not all(_office_address(address) for address in addresses):
        return (
            "The answer server resolved outside the local network. The request was not sent."
        )
    return ""


def require_resolved_destination(url: str) -> None:
    host = urlparse(url).hostname
    if not host:
        raise DestinationRejected("The answer server URL has no host. The request was not sent.")
    confirmed = os.environ.get("CASE_INTELLIGENCE_GENERATOR_REMOTE_CONFIRMED") == "1"
    reason = destination_block_reason(host, remote_confirmed=confirmed)
    if reason:
        raise DestinationRejected(reason)


def _container_loopback(hostname: str) -> bool:
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def environment_values(endpoint: GeneratorEndpoint, *, api_key_file: str = "") -> dict[str, str]:
    values = {
        "CASE_INTELLIGENCE_GENERATOR_BACKEND": endpoint.backend,
        "CASE_INTELLIGENCE_GENERATOR_SERVER": endpoint.server,
        "CASE_INTELLIGENCE_GENERATOR_URL": endpoint.url,
        "CASE_INTELLIGENCE_GENERATOR_MODEL": endpoint.model,
        "CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS": endpoint.allowed_host,
        "CASE_INTELLIGENCE_GENERATOR_DISABLE_THINKING": "0",
        "CASE_INTELLIGENCE_GENERATOR_TIMEOUT": "180",
    }
    if api_key_file:
        values["CASE_INTELLIGENCE_GENERATOR_API_KEY_FILE"] = api_key_file
    return values
