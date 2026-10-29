"""Standard-library-only interfaces for the container's synthetic packet trap."""

from collections.abc import Mapping, Sequence
from typing import TextIO


def classify_frame(
    frame: bytes,
    *,
    interface: str,
    generator_addresses: Sequence[str],
    peer_names: frozenset[str],
    honeytokens: Sequence[str],
) -> dict[str, object] | None:
    """Classify one Ethernet frame from AF_PACKET, including Linux loopback.

    Return one event with interface, protocol, source/destination addresses and
    ports, dns_name (or None), honeytokens and reasons, or None for allowed
    traffic. Reasons are ordered tcp_syn, udp, dns_question, honeytoken; DNS
    questions use dns_question instead of the generic udp reason. The capture
    caller supplies each observed frame once and separates the control phase.
    """
    raise NotImplementedError("Model canary frame classification is not implemented")


def write_jsonl(stream: TextIO, event: Mapping[str, object]) -> None:
    """Write one event as a newline-terminated JSON object and flush the stream."""
    raise NotImplementedError("Model canary JSONL writing is not implemented")
