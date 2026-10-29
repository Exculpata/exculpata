"""Synthetic probes for false negatives outside the initial canary contracts."""

import struct

import pytest

from tests.test_model_canary_trap import GENERATOR, TOKEN, _classify, _dns_question, _frame


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
def test_capture_drains_suite_before_control_and_rejects_loss(monkeypatch, capsys, drops):
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
            return _frame(GENERATOR, "192.0.2.10"), ("eth0", 0, trap.socket.PACKET_OUTGOING)
        if action == "stop":
            handlers[trap.signal.SIGTERM](0, None)
        raise trap.socket.timeout

    capture.recvfrom.side_effect = receive
    monkeypatch.setattr(trap.socket, "socket", Mock(return_value=capture))
    monkeypatch.setattr(trap.signal, "signal", Mock(side_effect=handlers.__setitem__))
    monkeypatch.setattr(trap.sys, "argv", ["canary_trap.py", "--generator-address", GENERATOR,
                                         "--peer-name", "canary-client", "--honeytoken", TOKEN])
    if drops:
        with pytest.raises(RuntimeError, match="Packet loss"):
            trap.main()
    else:
        trap.main()
    import json
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0] == {"state": "ready"}
    assert events[1]["phase"] == "suite"
    assert events[2] == {"state": "control"}
    assert events[3] == {"state": "complete", "packets": 1, "drops": drops}


@pytest.mark.parametrize("scheme", ["HTTPS", "ftp"])
def test_honeytoken_url_schemes_are_counted(scheme):
    from tests.test_model_canary_trap import test_t7_receipt

    test_t7_receipt(True, True, 300, 0,
                    [f"{scheme}://{TOKEN}.canary.invalid/"], "passed", 1)
