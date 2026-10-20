"""Shared, offline issuer and pin policy for preflight, settings and relay."""
from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import urlsplit

from . import _idna as idna


class Denied(ValueError):
    pass


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
    if not isinstance(value, str):
        raise Denied("invalid issuer")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise Denied("invalid issuer")
    return value.strip().rstrip("/")


def issuer_pins(value, issuer):
    if value == "":
        return frozenset()
    if not isinstance(value, str) or len(value) > 2048:
        raise Denied("invalid pins")
    data = json.loads(value, object_pairs_hook=_unique_fields)
    issuer = normalized_issuer(issuer)
    if issuer_authority(issuer) is None:
        raise Denied("invalid issuer-bound pins")
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
            or parsed.password is not None or parsed.query or parsed.fragment or '\\' in value
            or parsed.port == 0):
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



def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Denied("duplicate pin field")
        result[key] = value
    return result


def validated_https_url(value, *, origin_only=False):
    authority_value = issuer_authority(value)
    if authority_value is None:
        raise Denied("missing HTTPS URL")
    candidate = normalized_issuer(value)
    if origin_only and urlsplit(candidate).path not in {"", "/"}:
        raise Denied("origin must not contain a path")
    host, _port = authority_value
    if host == "localhost":
        raise Denied("loopback host denied")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not pinnable(address):
        raise Denied("unsafe literal host denied")
    return candidate
