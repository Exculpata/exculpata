"""Causal embedded-DNS diagnostic; ephemeral app-loopback listener, no query logs.

A random six-question challenge must arrive at this process on container loopback
and receive its answer through embedded DNS. NXDOMAIN/refusal/timeout alone never
passes. No host/daemon DNS configuration changes. Listener failure fails closed.
"""
import json
import secrets
import socket
import socketserver
import threading
import struct
import sys


def question(name, kind):
    identifier = secrets.randbelow(65536)
    labels = name.rstrip(".").encode("ascii").split(b".")
    if not labels or any(not label or len(label) > 63 for label in labels):
        raise ValueError("invalid synthetic DNS name")
    return struct.pack("!6H", identifier, 0x100, 1, 0, 0, 0) + b"".join(
        bytes([len(label)]) + label for label in labels
    ) + b"\0" + struct.pack("!HH", kind, 1)


def receive_exact(stream, length):
    data = b""
    while len(data) < length:
        part = stream.recv(length - len(data))
        if not part:
            raise ValueError("truncated DNS response")
        data += part
    return data


def query(name, kind, protocol, server="127.0.0.11", port=53):
    packet = question(name, kind)
    sock_type = socket.SOCK_DGRAM if protocol == "udp" else socket.SOCK_STREAM
    with socket.socket(socket.AF_INET, sock_type) as client:
        client.settimeout(3)
        client.connect((server, port))
        if protocol == "udp":
            client.send(packet)
            response = client.recv(4096)
        else:
            client.sendall(struct.pack("!H", len(packet)) + packet)
            length = struct.unpack("!H", receive_exact(client, 2))[0]
            if length > 4096:
                raise ValueError("oversized DNS response")
            response = receive_exact(client, length)
    if len(response) < 12 or response[:2] != packet[:2]:
        raise ValueError("invalid DNS response")
    _, flags, questions, answers, _, _ = struct.unpack("!6H", response[:12])
    if not flags & 0x8000 or flags & 0x200 or questions != 1:
        raise ValueError("invalid DNS response flags")
    return flags & 15, answers


class LoopbackChallenge:
    """Bounded responder for exactly six unpredictable questions; no forwarding."""
    def __init__(self, names, address="127.0.0.1", port=53):
        self.names = names
        self.seen = set()
        self.lock = threading.Lock()
        self.servers = []
        self.threads = []
        owner = self

        class UDP(socketserver.BaseRequestHandler):
            def handle(self):
                packet, stream = self.request
                reply = owner.answer(packet, "udp")
                if reply is not None:
                    stream.sendto(reply, self.client_address)

        class TCP(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(3)
                try:
                    size = struct.unpack("!H", receive_exact(self.request, 2))[0]
                    if size > 4096:
                        return
                    reply = owner.answer(receive_exact(self.request, size), "tcp")
                    if reply is not None:
                        self.request.sendall(struct.pack("!H", len(reply)) + reply)
                except (OSError, ValueError):
                    return
        class TCPServer(socketserver.TCPServer):
            allow_reuse_address = True

        try:
            self.servers.append(socketserver.UDPServer((address, port), UDP))
            self.servers.append(TCPServer((address, self.servers[0].server_address[1]), TCP))
            for server in self.servers:
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
                thread.start()
                self.threads.append(thread)
        except OSError:
            for server in self.servers:
                server.server_close()
            raise

    def answer(self, packet, protocol):
        if not 17 <= len(packet) <= 4096:
            return None
        _, flags, questions, answers, ns, ar = struct.unpack("!6H", packet[:12])
        if flags & 0x8000 or questions != 1 or answers or ns:
            return None
        labels, offset = [], 12
        try:
            while packet[offset]:
                length = packet[offset]
                if length > 63 or offset + 1 + length >= len(packet):
                    return None
                labels.append(packet[offset+1:offset+1+length].decode("ascii"))
                offset += length + 1
            offset += 1
            kind, qclass = struct.unpack("!HH", packet[offset:offset+4])
            end = offset + 4
        except (IndexError, UnicodeError, struct.error):
            return None
        name = ".".join(labels)
        if qclass != 1 or self.names.get((protocol, kind)) != name:
            return packet[:2] + struct.pack("!5H", 0x8185, 1, 0, 0, 0) + packet[12:end]
        with self.lock:
            self.seen.add((protocol, kind, name))
        # Answers are synthetic and never used for a network connection.
        data = {1: bytes((127, 0, 0, 2)), 28: bytes.fromhex("20010db8000000000000000000000001"),
                16: bytes([len(name)]) + name.encode("ascii")}[kind]
        record = b"\xc0\x0c" + struct.pack("!HHIH", kind, 1, 0, len(data)) + data
        return packet[:2] + struct.pack("!5H", 0x8180, 1, 1, 0, 0) + packet[12:end] + record

    def close(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=3)


def verify(internal_names):
    if not internal_names:
        raise ValueError("positive internal DNS control required")
    for name in internal_names:
        for protocol in ("udp", "tcp"):
            rcode, answers = query(name, 1, protocol)
            if rcode != 0 or answers < 1:
                raise ValueError("internal service resolution failed")
    names = {(protocol, kind): "synthetic-" + secrets.token_hex(16) + ".example.invalid"
             for protocol in ("udp", "tcp") for kind in (1, 28, 16)}
    challenge = LoopbackChallenge(names)
    try:
        for (protocol, kind), name in names.items():
            if query(name, kind, protocol) != (0, 1):
                raise ValueError("container-loopback DNS challenge was not answered")
        expected = {(protocol, kind, name) for (protocol, kind), name in names.items()}
        if challenge.seen != expected:
            raise ValueError("container-loopback DNS challenge was not observed locally")
    finally:
        challenge.close()
    return {"internal_dns_udp_tcp": True, "container_loopback_challenges": 6,
            "dns_behavior_verified": True}


if __name__ == "__main__":
    try:
        print(json.dumps(verify(sys.argv[1:])))
    except (OSError, ValueError):
        print("DNS boundary diagnostic failed; no request data retained", file=sys.stderr)
        raise SystemExit(1)
