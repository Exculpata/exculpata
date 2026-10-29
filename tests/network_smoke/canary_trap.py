"""Outbound AF_PACKET trap; stdout is JSONL. Unsupported packets abort capture.

Capture only PACKET_OUTGOING, including lo, to exclude incoming prompts and the
second loopback copy. No IP/TCP reassembly: fragments, extension headers and
partial/compressed/multiple DNS questions invalidate capture. Honeytokens are
matched within individual frames, not across packets or encoded payloads.
"""

import argparse
from ipaddress import ip_address
import json
import signal
import socket
import struct
import sys


def _require(supported):
    if not supported:
        raise ValueError("unsupported packet for classify_frame: use unfragmented IP "
                         "and complete, single, uncompressed DNS questions")


def _u16(data, offset):
    _require(len(data) >= offset + 2)
    return struct.unpack_from("!H", data, offset)[0]


def _dns_name(data):
    _require(len(data) >= 12)
    if data[2] & 0x80:
        return None
    _require(_u16(data, 4) == 1)
    labels, offset = [], 12
    while True:
        _require(offset < len(data))
        size = data[offset]
        offset += 1
        if size == 0:
            break
        _require(size <= 63 and offset + size <= len(data))
        label = data[offset:offset + size]
        _require(label.isascii())
        labels.append(label.decode("ascii"))
        offset += size
        _require(offset <= 266)
    _require(offset + 4 <= len(data))
    return ".".join(labels).lower()


def classify_frame(frame, *, interface, generator_addresses, peer_names, honeytokens):
    """Return a hit or None for one outgoing Ethernet frame; reject unknown forms."""
    lowered = frame.lower()
    tokens = [token for token in honeytokens if token.encode("ascii") in lowered]
    ether_type = _u16(frame, 12)
    packet = frame[14:]
    source = destination = source_port = destination_port = name = None
    reasons, protocol = [], "arp"
    if ether_type != 0x0806:
        _require(ether_type in (0x0800, 0x86dd))
        if ether_type == 0x0800:
            _require(len(packet) >= 20 and packet[0] >> 4 == 4)
            offset, end, number = (packet[0] & 15) * 4, _u16(packet, 2), packet[9]
            _require(20 <= offset <= end <= len(packet) and not _u16(packet, 6) & 0x3fff)
            source, destination = ip_address(packet[12:16]), ip_address(packet[16:20])
        else:
            _require(len(packet) >= 40 and packet[0] >> 4 == 6)
            offset, end, number = 40, 40 + _u16(packet, 4), packet[6]
            _require(40 < end <= len(packet))
            source, destination = ip_address(packet[8:24]), ip_address(packet[24:40])
        _require(str(source) in generator_addresses or source.is_loopback)
        _require(number in (1, 6, 17, 58))
        protocol = {1: "icmp", 6: "tcp", 17: "udp", 58: "icmpv6"}[number]
        segment = packet[offset:end]
        if number in (6, 17):
            source_port, destination_port = _u16(segment, 0), _u16(segment, 2)
            flags = 0
            if number == 6:
                _require(len(segment) >= 20)
                header, flags = (segment[12] >> 4) * 4, segment[13]
                _require(20 <= header <= len(segment))
            else:
                header = 8
                _require(8 <= _u16(segment, 4) == len(segment))
            payload = segment[header:]
            dns = destination_port == 53 or str(destination) == "127.0.0.11"
            internal = source.is_loopback and destination.is_loopback and not dns
            if dns and (number == 17 or payload):
                if number == 6:
                    _require(_u16(payload, 0) == len(payload) - 2)
                    payload = payload[2:]
                name = _dns_name(payload)
            peer = (name is not None and str(destination) == "127.0.0.11"
                    and name in {peer.lower().rstrip(".") for peer in peer_names})
            if not internal and flags & 2 and not flags & 16:
                reasons.append("tcp_syn")
            if name is not None and not peer:
                reasons.append("dns_question")
            elif number == 17 and not internal and not peer:
                reasons.append("udp")
    if tokens:
        reasons.append("honeytoken")
    if reasons:
        return dict(interface=interface, protocol=protocol,
                    source=str(source) if source is not None else None,
                    destination=str(destination) if destination is not None else None,
                    source_port=source_port, destination_port=destination_port,
                    dns_name=name, honeytokens=tokens, reasons=reasons)
    return None


def write_jsonl(stream, event):
    # fixture-write: synthetic packet observations to the caller's capture stream.
    stream.write(json.dumps(event, sort_keys=True) + "\n")
    stream.flush()


def main():
    """SIGTERM drains capture; SIGUSR1 drains the suite before marking control.

    Consumers must require ready and complete records, zero drops and exit 0.
    Only verified negative-control events may be excluded from receipt hits.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("generator-address", "peer-name", "honeytoken"):
        parser.add_argument("--" + option, action="append", required=True)
    args = parser.parse_args()
    stopping, control_requested, phase = False, False, "suite"

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    def control(signum, frame):
        nonlocal control_requested
        control_requested = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGUSR1, control)
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)) as capture:
        capture.settimeout(0.2)
        write_jsonl(sys.stdout, {"state": "ready"})
        while True:
            try:
                frame, address = capture.recvfrom(262144)
            except socket.timeout:
                if stopping:
                    break
                if control_requested and phase == "suite":
                    phase = "control"
                    write_jsonl(sys.stdout, {"state": "control"})
                continue
            if address[2] != socket.PACKET_OUTGOING:
                continue
            event = classify_frame(frame, interface=address[0],
                                   generator_addresses=args.generator_address,
                                   peer_names=frozenset(args.peer_name), honeytokens=args.honeytoken)
            if event:
                write_jsonl(sys.stdout, {**event, "phase": phase})
        packets, drops = struct.unpack("II", capture.getsockopt(263, 6, 8))
        write_jsonl(sys.stdout, {"state": "complete", "packets": packets, "drops": drops})
        if drops:
            raise RuntimeError("Packet loss invalidates the canary capture")


if __name__ == "__main__":
    main()
