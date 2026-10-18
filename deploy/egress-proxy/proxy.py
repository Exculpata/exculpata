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
from urllib.parse import urlsplit

import idna


class Denied(ValueError):
    pass


SETUP_SECONDS = 5
MAX_CONNECTIONS = 8


def pinnable(ip):
    # Only ordinary unicast public addresses, RFC1918 or IPv6 ULA may be pinned.
    # Explicit pins never authorize metadata/link-local/loopback/transition IPs.
    # Published AWS and Google IPv6 metadata endpoints are ULA; ordinary
    # private-address classification does not exclude these host services.
    metadata = (
        ipaddress.IPv6Address((0xfd00 << 112) | (0xec2 << 96) | 0x254),
        ipaddress.IPv6Address((0xfd20 << 112) | (0xce << 96) | 0x254),
    )
    if ip in metadata:
        return False
    private = (
        ipaddress.ip_network((int.from_bytes(bytes((10, 0, 0, 0))), 8)),
        ipaddress.ip_network((int.from_bytes(bytes((172, 16, 0, 0))), 12)),
        ipaddress.ip_network((int.from_bytes(bytes((192, 168, 0, 0))), 16)),
        ipaddress.ip_network((0xfc00 << 112, 7)),
    )
    return public_ip(ip) or any(ip in network for network in private)


def public_ip(ip):
    return (ip.is_global and not ip.is_multicast and not ip.is_unspecified and not ip.is_reserved
            and not (isinstance(ip, ipaddress.IPv6Address)
                     and (ip.ipv4_mapped or ip.sixtofour or ip.teredo)))


def normalized_issuer(value):
    # Match application configuration trimming without accepting line controls.
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise Denied("invalid issuer")
    return value.strip().rstrip("/")


def issuer_pins(value, issuer):
    if not value:
        return frozenset()
    if len(value) > 2048:
        raise Denied("invalid pins")
    data = json.loads(value)
    issuer = normalized_issuer(issuer)
    if (not issuer or not isinstance(data, dict) or set(data) != {"issuer", "addresses"}
            or not isinstance(data["issuer"], str) or normalized_issuer(data["issuer"]) != issuer
            or not isinstance(data["addresses"], list)
            or not 1 <= len(data["addresses"]) <= 16):
        raise Denied("invalid issuer-bound pins")
    addresses = []
    for value in data["addresses"]:
        if not isinstance(value, str) or "/" in value or "%" in value:
            raise Denied("invalid pin")
        ip = ipaddress.ip_address(value)
        if not pinnable(ip):
            raise Denied("pin address denied")
        addresses.append(ip)
    if len(set(addresses)) != len(addresses):
        raise Denied("duplicate pins")
    return frozenset(addresses)


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


def authority(value: str) -> tuple[str, int]:
    # CONNECT uses an authority, never a URL, path, userinfo or wildcard.
    # Underscores occur in existing private issuer names accepted by HTTPX.
    # They remain literal characters in one exact configured authority.
    if not re.fullmatch(r"(?:[a-zA-Z0-9._-]+|\[[0-9a-fA-F:]+\]):[0-9]{1,5}", value):
        raise Denied("invalid authority")
    parsed = urlsplit("//" + value)
    host, port = parsed.hostname, parsed.port
    if not host or not port or host.endswith('.') or '..' in host:
        raise Denied("invalid authority")
    # libc accepts historical integer/short/octal/hex IPv4 spellings. Never
    # interpret a numeric-looking parse failure as an ordinary DNS hostname.
    if re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+))*", host):
        try:
            if str(ipaddress.IPv4Address(host)) != host:
                raise ValueError()
        except ValueError:
            raise Denied("noncanonical numeric authority") from None
    return host.lower(), port


def issuer_authority(value: str) -> tuple[str, int] | None:
    value = normalized_issuer(value)
    if not value:
        return None  # Local/Kerberos mode remains healthy and denies all CONNECTs.
    if len(value) > 1024 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise Denied("invalid issuer")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or '\\' in value):
        raise Denied("invalid issuer")
    host = parsed.hostname
    if not host.isascii():
        try:
            # Match HTTPX's IDNA2008 encoding, not the stdlib's older mapping
            # (which, for example, changes sharp-s into a different hostname).
            host = idna.encode(host.lower()).decode("ascii")
        except idna.IDNAError:
            raise Denied("invalid issuer hostname") from None
    return authority(f"{'[' + host + ']' if ':' in host else host}:{parsed.port or 443}")


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
        if allowed is None or destination != allowed:
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
            with dial(*destination, self.server.pins) as upstream:
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


if __name__ == "__main__":
    try:
        if len(sys.argv) == 4 and sys.argv[1] == "--resolve":
            answers = socket.getaddrinfo(sys.argv[2], int(sys.argv[3]), type=socket.SOCK_STREAM)
            if not answers or len(answers) > 32:
                raise Denied("invalid address set")
            print(json.dumps(answers))
            raise SystemExit(0)
        issuer = os.environ.get("OIDC_ISSUER", "")
        allowed = issuer_authority(issuer)
        pins = issuer_pins(os.environ.get("OIDC_ISSUER_PINS", ""), issuer)
        with Server(("0.0.0.0", 3128), allowed, pins) as server:
            server.serve_forever()
    except (OSError, ValueError):
        raise SystemExit("Egress proxy configuration or listener unavailable") from None
