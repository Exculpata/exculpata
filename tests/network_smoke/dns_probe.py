"""Prove container-local forwarding and absence at a reachable outside recorder."""
import json
import os
import ssl

import httpx
from dns_boundary_probe import query, verify
from dns_trap import Trap


def main():
    context = ssl.create_default_context(cafile="/fixture/issuer.pem")
    def count():
        response = httpx.get("https://issuer.example.test:8443/dns-count", verify=context, timeout=5)
        response.raise_for_status()
        return response.json()["queries"]

    # Controls from the upstream network have already reached this recorder.
    before = count()
    assert before == 6, "outside DNS recorder needs all six positive controls"
    verify(["proxy"])
    assert count() == before, "embedded DNS forwarded outside the app namespace"
    for protocol in ("udp", "tcp"):
        for kind in (1, 28, 16):
            try:
                query("synthetic-direct.example.invalid", kind, protocol, os.environ["ISSUER_ADDRESS"])
            except OSError:
                pass
            else:
                raise AssertionError("direct outside DNS was reachable")
    # A test-only listener in the app namespace proves that explicit upstream
    # queries really stay on its loopback, rather than silently using host DNS.
    local = Trap("127.0.0.1")
    try:
        verify(["proxy"])
        assert local.count == 6, "all outside-name questions must reach container loopback"
    finally:
        local.close()
    assert count() == before, "outside recorder received an app query"
    print(json.dumps({"internal_dns_udp_tcp": True, "outside_dns_udp_tcp_a_aaaa_txt_denied": True,
        "container_local_dns_forwarding_proven": True, "outside_dns_recorder_positive_controls": before,
        "outside_dns_recorder_app_queries": 0}))


if __name__ == "__main__":
    main()
