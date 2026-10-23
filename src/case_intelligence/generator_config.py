"""Offline generator configuration shared by installer and deployed app settings."""
from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

from .oidc_network_policy import issuer_pins, validated_https_url


HTTPS_REQUIRED = (
    "External answer servers require HTTPS. Only the bundled generator on port 8000 permits HTTP. "
    "Host loopback servers cannot be reached by the containerized app; use a reachable HTTPS server."
)


def generator_literal(key, value):
    if "GENERATOR" in key and "$" in str(value):
        raise ValueError("Generator values cannot contain $; write resolved literals for Compose.")


def bundled_generator(url):
    parsed = urlsplit(url)
    return (parsed.scheme == "http" and parsed.hostname == "generator" and parsed.port == 8000
            and parsed.username is None and parsed.password is None
            and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment and "\\" not in url
            and not any(ord(c) <= 32 for c in url))


def validate_generator_url(url):
    generator_literal("GENERATOR_URL", url)
    try:
        if bundled_generator(url):
            return url.rstrip("/")
        if urlsplit(url).scheme != "https":
            raise ValueError(HTTPS_REQUIRED)
        return validated_https_url(url)
    except ValueError as exc:
        raise ValueError(f"{HTTPS_REQUIRED} Invalid server URL or denied destination.") from exc


def generator_pins(value, url):
    generator_literal("GENERATOR_EGRESS_PINS", value)
    pins = issuer_pins(value, url)
    host = urlsplit(url).hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (pins and address not in pins or not address.is_global and not pins):
        raise ValueError("Private answer servers require an explicit reviewed generator egress pin matching the URL.")
    return pins


def generator_server(backend: str) -> str:
    server = os.getenv("CASE_INTELLIGENCE_GENERATOR_SERVER", "").strip().casefold()
    if backend == "ollama":
        if server and server != "ollama":
            raise ValueError("ollama backend only supports the ollama server")
        return "ollama"
    if server and server not in {"vllm", "llamacpp", "lmstudio"}:
        raise ValueError("unsupported local generator server")
    return server or "vllm"


def validate_generator_environment(environment):
    endpoint = environment.get("CASE_INTELLIGENCE_GENERATOR_URL", "").strip()
    validate_generator_url(endpoint)
    for key, value in environment.items():
        if key.startswith("CASE_INTELLIGENCE_GENERATOR_"):
            generator_literal(key, value)
    if not bundled_generator(endpoint):
        generator_pins(environment.get("CASE_INTELLIGENCE_GENERATOR_EGRESS_PINS", ""), endpoint)
