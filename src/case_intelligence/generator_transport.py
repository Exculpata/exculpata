"""Connect model requests to vetted addresses, preserving TLS identity and refusing redirects."""
from __future__ import annotations

import http.client
import socket
import time
import urllib.request

from .external_generator import require_resolved_destination


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_generator_request(request, *, timeout, opener=None):
    addresses = require_resolved_destination(request.full_url)
    if opener is not None:
        # Explicit transport injection is for controlled evaluation/test clients.
        return opener.open(request, timeout=timeout)

    def connect(address, timeout=timeout, source_address=None):
        deadline = time.monotonic() + timeout
        last = None
        for vetted in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Generator connection timed out")
            family = socket.AF_INET6 if vetted.version == 6 else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            try:
                sock.settimeout(remaining)
                if source_address is not None:
                    sock.bind(source_address)
                # Numeric connect: no second hostname resolution after validation.
                sock.connect((str(vetted), address[1]))
                return sock
            except OSError as exc:
                last = exc
                sock.close()
        raise last or OSError("No approved generator address")

    def connection_factory(connection_type):
        def make(host, **kwargs):
            connection = connection_type(host, **kwargs)
            connection._create_connection = connect
            return connection
        return make

    class HTTP(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(connection_factory(http.client.HTTPConnection), req)

    class HTTPS(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(connection_factory(http.client.HTTPSConnection), req,
                                context=self._context)

    # Environment proxies could resolve independently or receive credentials.
    transport = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(), HTTP(), HTTPS())
    return transport.open(request, timeout=timeout)
