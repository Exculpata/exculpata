"""External generator HTTPS tunnels through the explicitly configured relay."""
from __future__ import annotations

import http.client
import os
from pathlib import Path
import ssl
import stat
import urllib.request
from urllib.parse import urlsplit

from .external_generator import DestinationRejected, _destination_reason
from .generator_config import generator_pins, validate_generator_url
from .oidc_network_policy import issuer_authority
from .service_endpoints import validate_service_endpoint

_MAX_CA_BYTES = 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validated_ca_file(environment_prefix: str) -> Path | None:
    raw = os.environ.get(f"{environment_prefix}_CA_FILE", "").strip()
    if not raw:
        raw = os.environ.get("CASE_INTELLIGENCE_MODEL_CA_FILE", "").strip()
    if not raw:
        return None
    if any(ord(ch) < 32 for ch in raw):
        raise ValueError("Model CA file path contains control characters")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ValueError("Model CA file path must be absolute")
    if candidate.is_symlink() or any(parent.is_symlink() for parent in candidate.parents):
        raise ValueError("Model CA file path cannot use symbolic links")
    try:
        metadata = candidate.stat(follow_symlinks=False)
    except OSError as exc:
        raise ValueError("Model CA file is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Model CA file must be a regular file")
    if metadata.st_mode & 0o022:
        raise ValueError("Model CA file cannot be group- or world-writable")
    if metadata.st_size <= 0 or metadata.st_size > _MAX_CA_BYTES:
        raise ValueError("Model CA file size is out of bounds")
    if not os.access(candidate, os.R_OK):
        raise ValueError("Model CA file is unreadable")
    return candidate


def open_generator_request(request, *, timeout, opener=None, environment_prefix="CASE_INTELLIGENCE_GENERATOR"):
    try:
        validate_generator_url(request.full_url)
        if urlsplit(request.full_url).scheme != "https":
            raise ValueError("External answer servers require HTTPS")
        validate_service_endpoint(request.full_url, environment_name=f"{environment_prefix}_ALLOWED_HOSTS",
                                  label="generator endpoint", schemes=frozenset({"https"}))
        configured = os.environ.get(f"{environment_prefix}_URL", request.full_url)
        pins = generator_pins(os.environ.get(f"{environment_prefix}_EGRESS_PINS", ""), configured)
        if issuer_authority(configured) != issuer_authority(request.full_url):
            raise ValueError("Generator request authority differs from configuration")
        confirmed = os.environ.get(f"{environment_prefix}_REMOTE_CONFIRMED") == "1"
        if pins:
            reason = _destination_reason(pins, remote_confirmed=confirmed)
            if reason:
                raise ValueError(reason)
        elif not confirmed:
            raise ValueError("External answer servers require remote consent or reviewed private address pins")
        proxy = urlsplit(os.environ.get(f"{environment_prefix}_PROXY", ""))
        if (proxy.scheme != "http" or not proxy.hostname or not proxy.port or proxy.path not in {"", "/"}
                or proxy.username is not None or proxy.password is not None or proxy.query or proxy.fragment):
            raise ValueError("Configure CASE_INTELLIGENCE_GENERATOR_PROXY for the CONNECT relay")
        ca_file = _validated_ca_file(environment_prefix)
        ssl_context = ssl.create_default_context(cafile=str(ca_file)) if ca_file is not None else None
    except (ValueError, ssl.SSLError, OSError) as exc:
        raise DestinationRejected(str(exc)) from exc
    if opener is not None:
        raise DestinationRejected("External generator requests must use the configured CONNECT relay")

    class HTTPS(urllib.request.HTTPSHandler):
        def https_open(self, req):
            def connection(host, **kwargs):
                target = urlsplit("//" + host)
                connection = http.client.HTTPSConnection(proxy.hostname, proxy.port, **kwargs)
                connection.set_tunnel(target.hostname, target.port or 443)
                return connection
            return self.do_open(connection, req, context=self._context)

    # Explicit tunnel construction cannot be bypassed by ambient NO_PROXY.
    transport = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(), HTTPS(context=ssl_context))
    return transport.open(request, timeout=timeout)
