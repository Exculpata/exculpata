"""Bounded CONNECT relay. It does not decrypt TLS or authorize OIDC URL paths."""
from __future__ import annotations

import ipaddress
import os
import re
import select
import socket
import socketserver
import threading
import time
from urllib.parse import urlsplit


class Denied(ValueError):
    pass


def authority(value: str) -> tuple[str, int]:
    # CONNECT uses an authority, never a URL, path, userinfo or wildcard.
    if not re.fullmatch(r"(?:[a-zA-Z0-9.-]+|\[[0-9a-fA-F:]+\]):[0-9]{1,5}", value):
        raise Denied("invalid authority")
    parsed = urlsplit("//" + value)
    host, port = parsed.hostname, parsed.port
    if not host or not port or host.endswith('.') or '..' in host:
        raise Denied("invalid authority")
    return host.lower(), port


def issuer_authority(value: str) -> tuple[str, int] | None:
    if not value:
        return None  # Local/Kerberos mode remains healthy and denies all CONNECTs.
    if len(value) > 1024 or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
        raise Denied("invalid issuer")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or '\\' in value):
        raise Denied("invalid issuer")
    host = parsed.hostname
    return authority(f"{'[' + host + ']' if ':' in host else host}:{parsed.port or 443}")


def public_addresses(host: str, port: int):
    """Resolve once, reject the entire answer set on any forbidden address.

    Dialing the returned sockaddr avoids a second DNS lookup after validation.
    IPv6 transition mechanisms are denied, including mapped/6to4/Teredo forms.
    """
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not answers or len(answers) > 32:
        raise Denied("invalid address set")
    for family, socktype, proto, _, address in answers:
        ip = ipaddress.ip_address(address[0])
        if (family not in (socket.AF_INET, socket.AF_INET6) or not ip.is_global
                or ip.is_multicast or ip.is_unspecified or ip.is_reserved
                or (isinstance(ip, ipaddress.IPv6Address)
                    and (ip.ipv4_mapped or ip.sixtofour or ip.teredo))):
            raise Denied("destination address denied")
    return answers


def dial(host: str, port: int):
    answers = public_addresses(host, port)
    for family, socktype, proto, _, address in answers:
        connection = socket.socket(family, socktype, proto)
        try:
            connection.settimeout(5)
            connection.connect(address)
            return connection
        except OSError:
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
            with dial(*destination) as upstream:
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
    request_queue_size = 32

    def __init__(self, address, allowed):
        self.allowed = allowed
        self.slots = threading.BoundedSemaphore(32)
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
        allowed = issuer_authority(os.environ.get("OIDC_ISSUER", ""))
        with Server(("0.0.0.0", 3128), allowed) as server:
            server.serve_forever()
    except (OSError, ValueError):
        raise SystemExit("Egress proxy configuration or listener unavailable") from None
