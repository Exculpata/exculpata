"""Bounded CONNECT relay. It does not decrypt TLS or authorize OIDC URL paths."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import select
import socket
import socketserver
import subprocess
import sys
import threading
import time
from case_intelligence.oidc_network_policy import (
    Denied, authority, issuer_authority, issuer_pins, normalized_issuer, pinnable, public_ip,
    validated_https_url,
)


SETUP_SECONDS = 5
MAX_CONNECTIONS = 8


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise Denied("setup deadline")
    return value


def resolve(host, port, deadline):
    """A killable resolver process prevents stuck libc DNS from occupying slots.

    At most one child exists per connection slot; no abandoned resolver threads
    or queued futures survive timeout. The same deadline covers all TCP attempts.
    """
    with subprocess.Popen([sys.executable, "-I", __file__, "--resolve", host, str(port)],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, env={}) as child:
        try:
            output, _ = child.communicate(timeout=remaining(deadline))
        except (subprocess.TimeoutExpired, Denied):
            child.kill()
            child.communicate()
            raise Denied("resolver deadline") from None
        if child.returncode != 0:
            raise Denied("resolver unavailable")
    return json.loads(output)


def public_addresses(host: str, port: int, pins=frozenset(), deadline=None):
    """Resolve once, reject the entire answer set on any forbidden address.

    Dialing the returned sockaddr avoids a second DNS lookup after validation.
    IPv6 transition mechanisms are denied, including mapped/6to4/Teredo forms.
    """
    answers = resolve(host, port, deadline if deadline is not None else time.monotonic() + SETUP_SECONDS)
    if not answers or len(answers) > 32:
        raise Denied("invalid address set")
    for family, socktype, proto, _, address in answers:
        ip = ipaddress.ip_address(address[0])
        if (family not in (socket.AF_INET, socket.AF_INET6)
                or (ip not in pins or not pinnable(ip) if pins else not public_ip(ip))):
            raise Denied("destination address denied")
    return answers


def dial(host: str, port: int, pins=frozenset()):
    deadline = time.monotonic() + SETUP_SECONDS
    answers = public_addresses(host, port, pins, deadline)
    for index, (family, socktype, proto, _, address) in enumerate(answers):
        # Share the remaining budget so one dropped endpoint cannot starve
        # every validated fallback. DNS and all attempts retain one deadline.
        timeout = remaining(deadline) / (len(answers) - index)
        connection = socket.socket(family, socktype, proto)
        try:
            connection.settimeout(timeout)
            connection.connect(tuple(address))
            remaining(deadline)
            connection.settimeout(5)
            return connection
        except (OSError, Denied):
            connection.close()
    raise Denied("destination unavailable")


def read_connect(client, allowed):
    header = bytearray()
    deadline = time.monotonic() + 5
    # Read only through the delimiter; never consume initial TLS bytes.
    while not header.endswith(b"\r\n\r\n"):
        if time.monotonic() >= deadline:
            raise Denied("request deadline")
        value = client.recv(1)
        if not value or len(header) >= 8192:
            raise Denied("invalid request")
        header.extend(value)
    try:
        lines = bytes(header).decode("ascii").split("\r\n")
        method, target, version = lines[0].split(" ")
        if method != "CONNECT" or version not in {"HTTP/1.0", "HTTP/1.1"}:
            raise Denied("method denied")
        destination = authority(target)
        if allowed is None or (destination not in allowed if isinstance(allowed, dict) else destination != allowed):
            raise Denied("destination denied")
        seen = set()
        for line in lines[1:-2]:
            name, separator, value = line.partition(":")
            if (not separator or not re.fullmatch(r"[A-Za-z0-9-]+", name)
                    or name.lower() in seen or any(ord(c) < 32 for c in value)):
                raise Denied("invalid header")
            seen.add(name.lower())
            if name.lower() in {"content-length", "transfer-encoding", "upgrade"}:
                raise Denied("body denied")
            if name.lower() == "host" and authority(value.strip()) != destination:
                raise Denied("host mismatch")
        return destination
    except (UnicodeError, ValueError) as exc:
        raise Denied("invalid request") from exc


def relay(client, upstream):
    deadline, remaining = time.monotonic() + 60, 8 * 1024 * 1024
    while remaining > 0 and time.monotonic() < deadline:
        readable, _, _ = select.select([client, upstream], [], [], max(0, min(5, deadline - time.monotonic())))
        for source in readable:
            data = source.recv(min(65536, remaining))
            if not data:
                return
            (upstream if source is client else client).sendall(data)
            remaining -= len(data)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(5)
        established = False
        try:
            destination = read_connect(client, self.server.allowed)
            pins = self.server.allowed[destination] if isinstance(self.server.allowed, dict) else self.server.pins
            with dial(*destination, pins) as upstream:
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                established = True
                relay(client, upstream)
        except (OSError, ValueError):
            if not established:
                try:
                    client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass
        # No request/destination/TLS payload logging.


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = MAX_CONNECTIONS

    def __init__(self, address, allowed, pins=frozenset()):
        self.allowed = allowed
        self.pins = pins
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


# Operator-configured relay destinations: (exact HTTPS URL setting, reviewed pins setting).
# The OIDC issuer is handled first with its existing rules.
OPERATOR_DESTINATIONS = (("GENERATOR_EGRESS_URL", "GENERATOR_EGRESS_PINS"),)


class DestinationConfigurationError(Denied):
    pass


def configured_destinations(environment):
    """Exact authorities mapped to their pins; conflicting pins on a shared authority fail closed."""
    issuer = environment.get("OIDC_ISSUER", "")
    allowed = issuer_authority(issuer)
    pins = issuer_pins(environment.get("OIDC_ISSUER_PINS", ""), issuer)
    destinations = {allowed: pins} if allowed is not None else {}
    for url_setting, pins_setting in OPERATOR_DESTINATIONS:
        url = environment.get(url_setting, "")
        pin_value = environment.get(pins_setting, "")
        reason = "destination interpolation denied"
        try:
            if "$" in url or "$" in pin_value:
                raise Denied(reason)
            if not url:
                reason = "destination pins require a URL"
                if pin_value:
                    raise Denied(reason)
                continue
            reason = "invalid HTTPS URL"
            validated_https_url(url)
            destination = issuer_authority(url)
            reason = "invalid destination pins"
            destination_pins = issuer_pins(pin_value, url)
            reason = "conflicting authority pins"
            if destination in destinations and destinations[destination] != destination_pins:
                raise Denied(reason)
        except ValueError:
            raise DestinationConfigurationError(f"{url_setting}/{pins_setting}: {reason}") from None
        destinations[destination] = destination_pins
    return destinations


if __name__ == "__main__":
    try:
        if len(sys.argv) == 4 and sys.argv[1] == "--resolve":
            answers = socket.getaddrinfo(sys.argv[2], int(sys.argv[3]), type=socket.SOCK_STREAM)
            if not answers or len(answers) > 32:
                raise Denied("invalid address set")
            print(json.dumps(answers))
            raise SystemExit(0)
        with Server(("0.0.0.0", 3128), configured_destinations(os.environ)) as server:
            server.serve_forever()
    except DestinationConfigurationError as error:
        raise SystemExit(f"Egress proxy configuration invalid: {error}") from None
    except (OSError, ValueError):
        raise SystemExit("Egress proxy configuration or listener unavailable") from None
