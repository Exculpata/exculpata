"""Prove the synthetic foreign TLS endpoint is alive before app denial checks.

Runs only inside the disposable relay. Its test certificate names the issuer,
so this reachability control ignores the foreign hostname mismatch. Application
and Authlib certificate validation remain strict and are tested independently.
"""
import http.client
import ipaddress
import json
from pathlib import Path
import ssl
import sys
import time

def assert_front_default_route(expected, routes):
    gateways = set()
    for line in routes.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 8 and fields[1] == fields[7] == '00000000' and int(fields[3], 16) & 3 == 3:
            gateways.add(ipaddress.IPv4Address(int(fields[2], 16).to_bytes(4, 'little')))
    assert gateways == {ipaddress.IPv4Address(expected)}, 'relay default route does not select front'


def main():
    address = str(ipaddress.ip_address(sys.argv[1]))
    assert_front_default_route(sys.argv[2], Path('/proc/net/route').read_text())
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        connection = http.client.HTTPSConnection(
            address, 8443, timeout=3, context=ssl._create_unverified_context())
        try:
            connection.request('GET', '/')
            response = connection.getresponse()
            assert response.status == 200
            assert json.loads(response.read(4096)) == {'synthetic': True}
            print(json.dumps({'foreign_front_tls_reachable': True, 'relay_front_default_route': True}))
            return
        except (OSError, ValueError, AssertionError, http.client.HTTPException):
            time.sleep(.2)
        finally:
            connection.close()
    raise RuntimeError('foreign TLS reachability control failed')


if __name__ == '__main__':
    main()
