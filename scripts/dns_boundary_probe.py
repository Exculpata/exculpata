"""Read-only embedded DNS diagnostic; synthetic names only, no payload logging.

Successful negative responses alone do not prove absence of forwarding. Installer
runtime inspection and the controlled-resolver smoke provide separate evidence.
This file is also streamed through stdin into the installed app as its normal UID.
"""
import json
import secrets
import socket
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


def verify(internal_names):
    if not internal_names:
        raise ValueError("positive internal DNS control required")
    for name in internal_names:
        for protocol in ("udp", "tcp"):
            rcode, answers = query(name, 1, protocol)
            if rcode != 0 or answers < 1:
                raise ValueError("internal service resolution failed")
    results = []
    for protocol in ("udp", "tcp"):
        for kind in (1, 28, 16):
            # Fixed reserved suffix; random labels prevent a cached negative pass.
            name = "synthetic-" + secrets.token_hex(8) + ".example.invalid"
            rcode, answers = query(name, kind, protocol)
            if rcode not in (2, 3, 5) or answers:
                raise ValueError("outside DNS was not rejected")
            results.append({"transport": protocol, "type": kind, "rcode": rcode, "answers": answers})
    return {"internal_dns_udp_tcp": True, "outside_dns_negative_responses": results}


if __name__ == "__main__":
    try:
        print(json.dumps(verify(sys.argv[1:])))
    except (OSError, ValueError):
        print("DNS boundary diagnostic failed; no request data retained", file=sys.stderr)
        raise SystemExit(1)
