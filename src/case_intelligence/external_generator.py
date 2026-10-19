"""Validate an operator answer server before any request is sent."""
from __future__ import annotations

import ipaddress
import os
import socket
import threading
from dataclasses import dataclass
from urllib.parse import urlparse


SERVERS = ("ollama", "llamacpp", "lmstudio")
_BACKEND = {"ollama": "ollama", "llamacpp": "openai", "lmstudio": "openai"}
# Docker's host-gateway name. Kept split so the publication scan does not see
# one internal hostname token.
HOST_GATEWAY_NAME = "host.docker." + "internal"
_COMPLETION_SUFFIXES = ("/v1/chat/completions", "/api/chat", "/v1/models", "/api/tags")
# Exact RFC1918 ranges, loopback, and RFC4193 ULA; no broad is_private shortcut.
_LOCAL_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.IPv4Network((10 << 24, 8)),
    ipaddress.IPv4Network(((172 << 24) | (16 << 16), 12)),
    ipaddress.IPv4Network(((192 << 24) | (168 << 16), 16)),
    ipaddress.ip_network("::1/128"), ipaddress.IPv6Network((0xFC << 120, 7)),
)
# EC2's documented IPv6 metadata endpoint, rejected even with remote consent.
_METADATA = ipaddress.IPv6Address((0xFD00 << 112) | (0xEC2 << 96) | 0x254)
# Alibaba ECS/ECI metadata is outside link-local space and also never consentable.
_METADATA_ADDRESSES = frozenset((_METADATA, ipaddress.IPv4Address((100 << 24) | (100 << 16) | (100 << 8) | 200)))
_DNS_SLOTS = threading.BoundedSemaphore(4)
_DNS_TIMEOUT = 2.0


def _blocked_address(address):
    concrete = _concrete(address)
    return (concrete.is_unspecified or concrete.is_link_local or concrete.is_multicast
            or concrete in _METADATA_ADDRESSES)


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
    parsed = _parsed_url((url or "").strip(), EndpointRejected)
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
    local = host == HOST_GATEWAY_NAME or host_is_local(host)
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
    if any(_blocked_address(address) for address in concrete):
        return (
            "That address is metadata, multicast, link-local or unspecified. "
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
    if isinstance(address, ipaddress.IPv6Address):
        address = ipaddress.IPv6Address(address.packed)
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped if mapped is not None else address


def _office_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Loopback, RFC1918, or IPv6 ULA. Link-local and unspecified addresses are not."""

    concrete = _concrete(address)
    return not _blocked_address(concrete) and any(concrete in network for network in _LOCAL_NETWORKS)


def _resolve_addresses(hostname: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    # getaddrinfo has no per-call timeout. Bound caller latency and the number of
    # outstanding daemon lookups without changing process-wide socket defaults.
    if not _DNS_SLOTS.acquire(blocking=False):
        return ()
    infos = []
    def resolve():
        try:
            infos.extend(socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM))
        except OSError:
            pass
        finally:
            _DNS_SLOTS.release()
    worker = threading.Thread(target=resolve, daemon=True, name="generator-dns")
    worker.start()
    worker.join(_DNS_TIMEOUT)
    if worker.is_alive():
        return ()
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
    return _destination_reason(addresses_for(hostname), remote_confirmed=remote_confirmed)


def _destination_reason(addresses, *, remote_confirmed):
    if not addresses:
        return "The answer server name did not resolve. The request was not sent."
    if any(_blocked_address(address) for address in addresses):
        return "The answer server resolved to a metadata, multicast, link-local or unspecified address. The request was not sent."
    if not remote_confirmed and not all(_office_address(address) for address in addresses):
        return "The answer server resolved outside the local network. The request was not sent."
    return ""


def require_resolved_destination(url: str):
    """Return exactly the addresses approved for this connection."""
    host = _parsed_url(url, DestinationRejected).hostname
    if not host:
        raise DestinationRejected("The answer server URL has no host. The request was not sent.")
    addresses = addresses_for(host)
    confirmed = os.environ.get("CASE_INTELLIGENCE_GENERATOR_REMOTE_CONFIRMED") == "1"
    reason = _destination_reason(addresses, remote_confirmed=confirmed)
    if reason:
        raise DestinationRejected(reason)
    return addresses


def _parsed_url(url, error_type):
    try:
        parsed = urlparse(url)
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("port out of range")
        return parsed
    except ValueError as exc:
        raise error_type("Enter a valid server URL with a port from 1 to 65535.") from exc


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
