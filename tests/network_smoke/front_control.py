"""Prove the synthetic foreign TLS endpoint is alive before app denial checks.

Runs only inside the disposable relay. Its test certificate names the issuer,
so this reachability control ignores the foreign hostname mismatch. Application
and Authlib certificate validation remain strict and are tested independently.
"""
import http.client
import ipaddress
import json
import ssl
import sys
import time

address = str(ipaddress.ip_address(sys.argv[1]))
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    connection = http.client.HTTPSConnection(
        address, 8443, timeout=3, context=ssl._create_unverified_context())
    try:
        connection.request('GET', '/')
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read(4096)) == {'synthetic': True}
        print(json.dumps({'foreign_front_tls_reachable': True}))
        break
    except (OSError, ValueError, AssertionError, http.client.HTTPException):
        time.sleep(.2)
    finally:
        connection.close()
else:
    raise RuntimeError('foreign TLS reachability control failed')
