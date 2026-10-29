"""Synthetic probes for false negatives outside the initial canary contracts."""

import struct

import pytest

from tests.test_model_canary_trap import (
    CLIENT, GENERATOR, GENERATOR_V6, TOKEN, _classify, _dns_question, _frame, _icmp_frame,
)


@pytest.mark.parametrize("source,destination", [
    (GENERATOR, "192.0.2.10"), (GENERATOR_V6, "2001:db8::10"),
])
def test_outbound_icmp_echo_is_a_hit(source, destination):
    hit = _classify(_icmp_frame(source, destination))
    assert hit is not None
    assert hit["reasons"] == ["icmp"]
    assert hit["destination"] == destination


@pytest.mark.parametrize("address", ["127.0.0.1", "::1"])
def test_internal_icmp_echo_is_not_a_hit(address):
    assert _classify(_icmp_frame(address, address), "lo") is None


def test_t2_ipv6_outbound_syn():
    hit = _classify(_frame(GENERATOR_V6, "2001:db8::10"))
    assert hit["reasons"] == ["tcp_syn"]
    assert hit["destination"] == "2001:db8::10"


@pytest.mark.parametrize("protocol", ["tcp", "udp"])
@pytest.mark.parametrize("extra", ["trailing", "answer", "authority", "edns"])
def test_peer_dns_with_extra_data_fails_closed(protocol, extra):
    payload = bytearray(_dns_question("canary-client"))
    if extra == "trailing":
        payload += b"synthetic"
    else:
        offset = {"answer": 6, "authority": 8, "edns": 10}[extra]
        payload[offset:offset + 2] = b"\x00\x01"
        payload += b"\x00" + struct.pack("!HHIH", 41, 1232, 0, 0)
    if protocol == "tcp":
        payload = struct.pack("!H", len(payload)) + payload
    with pytest.raises(ValueError, match="unsupported.*classify_frame"):
        _classify(_frame("127.0.0.1", "127.0.0.11", protocol=protocol,
                         destination_port=53, flags=0x18, payload=payload), "lo")


@pytest.mark.parametrize("token", [TOKEN, TOKEN.upper()])
@pytest.mark.parametrize("payload", [TOKEN.encode(), TOKEN.upper().encode()])
def test_honeytoken_matching_is_case_insensitive(token, payload):
    from tests.network_smoke.canary_trap import classify_frame

    hit = classify_frame(_frame(GENERATOR, "192.0.2.10", flags=0x18, payload=payload),
                         interface="eth0", generator_addresses=(GENERATOR,),
                         peer_names=frozenset(), honeytokens=(token,))
    assert hit is not None
    assert hit["honeytokens"] == [token]
    assert hit["reasons"] == ["honeytoken"]


@pytest.mark.parametrize("overrides", [
    {"destination": "192.0.2.10"}, {"destination_port": 41001},
    {"source_port": 8001}, {"flags": 0x02}, {"flags": 0x12}, {"flags": 0},
    {"protocol": "udp"}, {"destination": "127.0.0.1", "destination_port": 29500},
])
def test_honeytoken_reply_exception_is_narrow(overrides):
    fields = dict(destination=CLIENT, source_port=8000, destination_port=41000,
                  flags=0x18, payload=TOKEN.encode())
    fields.update(overrides)
    hit = _classify(_frame(GENERATOR, **fields))
    assert "honeytoken" in hit["reasons"]


@pytest.mark.parametrize("source,destination", [
    (GENERATOR, CLIENT), (GENERATOR_V6, "2001:db8::3"),
])
@pytest.mark.parametrize("kind", ["reply", "unregistered", "other_source", "dns", "padding"])
def test_reply_exemption_requires_the_exact_connection(source, destination, kind):
    from tests.network_smoke.canary_trap import classify_frame

    port = 53 if kind == "dns" else 41000
    payload = TOKEN.upper().encode()
    if kind == "dns":
        payload = _dns_question(f"{TOKEN}.canary.invalid")
        payload = struct.pack("!H", len(payload)) + payload
    frame = _frame(source, destination, source_port=8000, destination_port=port,
                   flags=0x18, payload=payload)
    if kind == "padding":
        frame += TOKEN.encode()
    connections = ((source, 8000, destination, port),)
    if kind == "unregistered":
        connections = ()
    elif kind == "other_source":
        connections = (("192.0.2.99", 8000, destination, port),)
    hit = classify_frame(frame, interface="eth0", generator_addresses=(source,),
                         peer_names=frozenset(), honeytokens=(TOKEN,),
                         answer_connections=connections)
    if kind == "reply":
        assert hit is None
    else:
        assert "honeytoken" in hit["reasons"]
        if kind == "dns":
            assert "dns_question" in hit["reasons"]


@pytest.mark.parametrize("source,destination", [
    (GENERATOR, CLIENT), ("::1", "::1"),
])
def test_honeytoken_in_icmp_reply_is_a_hit(source, destination):
    hit = _classify(_icmp_frame(source, destination, echo_request=False, payload=TOKEN.encode()))
    assert hit["reasons"] == ["honeytoken"]


@pytest.mark.parametrize("token", [TOKEN, TOKEN.upper()])
@pytest.mark.parametrize("url_token", [TOKEN, TOKEN.upper()])
def test_receipt_honeytoken_urls_are_case_insensitive(token, url_token):
    from tests.test_model_canary_trap import test_t7_receipt

    test_t7_receipt(True, True, 300, 0, [f"https://{url_token}.canary.invalid/"],
                    "passed", 1, honeytoken=token)


@pytest.mark.parametrize("name,destination,port,reason", [
    ("CANARY-CLIENT", "127.0.0.11", 53, None),
    ("canary-client", "127.0.0.1", 53, "dns_question"),
    ("canary-control.example", "127.0.0.11", 45678, "dns_question"),
])
def test_dns_peer_exception_is_only_for_embedded_resolver(name, destination, port, reason):
    event = _classify(_frame("127.0.0.1", destination, protocol="udp",
                            destination_port=port, payload=_dns_question(name)), "lo")
    assert (event["reasons"] if event else []) == ([reason] if reason else [])


def test_loopback_honeytoken_is_not_hidden_by_internal_port_exception():
    event = _classify(_frame("::1", "::1", destination_port=29500,
                            payload=TOKEN.encode()), "lo")
    assert event["reasons"] == ["honeytoken"]


@pytest.mark.parametrize("payload", [b"", b"\x00" * 12,
    struct.pack("!6H", 7, 0x100, 1, 0, 0, 0) + b"\xc0\x0c\x00\x01\x00\x01"])
def test_unsupported_dns_cannot_be_a_clean_frame(payload):
    with pytest.raises(ValueError, match="unsupported.*classify_frame"):
        _classify(_frame("127.0.0.1", "127.0.0.11", protocol="udp",
                         destination_port=53, payload=payload), "lo")


def test_fragmented_ip_cannot_hide_a_transport_header():
    frame = bytearray(_frame(GENERATOR, "192.0.2.10"))
    frame[20:22] = b"\x20\x00"
    with pytest.raises(ValueError, match="unsupported.*classify_frame"):
        _classify(bytes(frame))


@pytest.mark.parametrize("drops", [0, 1])
@pytest.mark.parametrize("answer_reply", [False, True])
def test_capture_drains_suite_before_control_and_rejects_loss(monkeypatch, capsys, drops,
                                                             answer_reply):
    from unittest.mock import MagicMock, Mock
    from tests.network_smoke import canary_trap as trap

    handlers, actions = {}, iter(("control", "idle", "stop"))
    capture = MagicMock()
    capture.__enter__.return_value = capture
    capture.getsockopt.return_value = struct.pack("II", 1, drops)

    def receive(size):
        action = next(actions)
        if action == "control":
            handlers[trap.signal.SIGUSR1](0, None)
            frame = (_frame(GENERATOR, CLIENT, source_port=8000, destination_port=41000,
                            flags=0x18, payload=TOKEN.encode()) if answer_reply
                     else _frame(GENERATOR, "192.0.2.10"))
            return frame, ("eth0", 0, trap.socket.PACKET_OUTGOING)
        if action == "stop":
            handlers[trap.signal.SIGTERM](0, None)
        raise trap.socket.timeout

    capture.recvfrom.side_effect = receive
    monkeypatch.setattr(trap.socket, "socket", Mock(return_value=capture))
    monkeypatch.setattr(trap.signal, "signal", Mock(side_effect=handlers.__setitem__))
    monkeypatch.setattr(trap.sys, "argv", ["canary_trap.py", "--generator-address", GENERATOR,
                                         "--peer-name", "canary-client", "--honeytoken", TOKEN,
                                         "--answer-connection", GENERATOR, "8000", CLIENT, "41000"])
    if drops:
        with pytest.raises(RuntimeError, match="Packet loss"):
            trap.main()
    else:
        trap.main()
    import json
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0] == {"state": "ready"}
    assert len(events) == (3 if answer_reply else 4)
    if not answer_reply:
        assert events[1]["phase"] == "suite"
    assert events[-2] == {"state": "control"}
    assert events[-1] == {"state": "complete", "packets": 1, "drops": drops}


@pytest.mark.parametrize("scheme", ["HTTPS", "ftp"])
def test_honeytoken_url_schemes_are_counted(scheme):
    from tests.test_model_canary_trap import test_t7_receipt

    test_t7_receipt(True, True, 300, 0,
                    [f"{scheme}://{TOKEN}.canary.invalid/"], "passed", 1)
