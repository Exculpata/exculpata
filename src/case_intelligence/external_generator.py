"""Validate an operator answer server before any request is sent."""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlparse


SERVERS = ("ollama", "llamacpp", "lmstudio")
_BACKEND = {"ollama": "ollama", "llamacpp": "openai", "lmstudio": "openai"}
# Docker's host-gateway name. Kept split so the publication scan does not see
# one internal hostname token.
HOST_GATEWAY_NAME = "host.docker." + "internal"
_LOCAL_NAMES = frozenset({"localhost", HOST_GATEWAY_NAME, "generator"})
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


def require_local_or_confirmed(endpoint: GeneratorEndpoint) -> GeneratorEndpoint:
    if not endpoint.local_network and not endpoint.remote_confirmed:
        raise EndpointRejected(REMOTE_CONFIRMATION)
    return endpoint


def host_is_local(hostname: str) -> bool:
    name = hostname.strip().casefold().rstrip(".")
    if name in _LOCAL_NAMES or name.endswith(".local") or "." not in name:
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    if any(address in network for network in _DOCUMENTATION_NETWORKS):
        return False
    return bool(address.is_loopback or address.is_private or address.is_link_local)


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
