"""Real Authlib adapter through the relay plus independent direct TCP denials."""
import asyncio
import json
import os
from pathlib import Path
import socket
import ssl
import sys

import httpx
from case_intelligence.identity import AuthlibOidcClient, OidcSettings, OidcAuthenticationError
from case_intelligence.service_endpoints import open_service_request

ISSUER = "https://issuer.example.test:8443/realm"
FIXTURE = Path(os.environ.get("FIXTURE_ROOT", "/tmp/issue319-fixture"))
CA = ssl.create_default_context(cafile=str(FIXTURE / "issuer.pem"))


def denied_connect(target):
    with socket.create_connection(("egress-proxy", 3128), timeout=3) as client:
        client.sendall(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
        assert client.recv(4096).startswith(b"HTTP/1.1 403"), "CONNECT must be denied"


async def main():
    if len(sys.argv) > 1 and sys.argv[1] == "deny-all":
        denied_connect("issuer.example.test:8443")
        print(json.dumps({"no_issuer_healthy_deny_all": True}))
        return
    settings = OidcSettings(issuer=ISSUER, external_origin="https://app.example.test",
        client_id="synthetic-client", client_secret="synthetic-smoke-secret-value",
        ca_file=FIXTURE / "issuer.pem")
    oidc = AuthlibOidcClient(settings)
    url = await oidc.authorization_url(state="synthetic-state", nonce="synthetic-nonce", code_verifier="v" * 64)
    assert url.startswith(ISSUER + "/authorize?")
    claims = await oidc.authenticate(code="synthetic-code", nonce="synthetic-nonce", code_verifier="v" * 64)
    assert claims["sub"] == "synthetic-subject"
    async with httpx.AsyncClient(verify=CA, follow_redirects=False, timeout=5) as client:
        events = (await client.get(ISSUER.replace("/realm", "/events"))).json()
        assert {"realm:discovery", "realm:token", "realm:jwks"} <= set(events)
    from dataclasses import replace
    for mode in ('discovery-redirect', 'token-redirect', 'jwks-redirect', 'metadata-redirect'):
        adapter = AuthlibOidcClient(replace(settings, issuer=ISSUER.replace('/realm', '/' + mode)))
        try:
            await adapter.authenticate(code='synthetic-code', nonce='synthetic-nonce', code_verifier='v'*64)
        except OidcAuthenticationError:
            pass
        else:
            raise AssertionError('Authlib accepted redirect')
    without_ca = AuthlibOidcClient(replace(settings, ca_file=None))
    try:
        await without_ca.authenticate(code='synthetic-code', nonce='synthetic-nonce', code_verifier='v'*64)
    except OidcAuthenticationError as exc:
        reasons = []
        while exc is not None:
            reasons.append(str(exc).upper())
            exc = exc.__cause__ or exc.__context__
        assert any('CERTIFICATE' in reason for reason in reasons), 'Authlib denial was not TLS validation'
    else:
        raise AssertionError('Authlib accepted untrusted issuer certificate')
    malicious = AuthlibOidcClient(replace(settings, issuer=ISSUER.replace('/realm', '/malicious')))
    assert (await malicious.authenticate(code='synthetic-code', nonce='synthetic-nonce', code_verifier='v'*64))['sub'] == 'synthetic-subject'
    async with httpx.AsyncClient(verify=CA, timeout=5) as client:
        events = (await client.get(ISSUER.replace('/realm', '/events'))).json()
        assert not any(event.endswith(':redirect-target') for event in events)
    # The canary is directly reachable: HTTP denial cannot be a routing accident.
    import http.client
    control = http.client.HTTPConnection(os.environ['DNS_RECORDER_ADDRESS'],8080,timeout=3)
    control.request('GET','/'); assert control.getresponse().status == 200; control.close()
    async with httpx.AsyncClient(timeout=5) as client:
        for url in ('http://canary:8080/', 'http://x.postgres:8080/', 'http://evil.generator:8080/'):
            response = await client.get(url)
            assert response.status_code == 403, 'plain HTTP did not reach deny-capable relay'
        for url in ('https://x.postgres:8443/', 'https://evil.generator:8443/'):
            try:
                await client.get(url)
            except httpx.ProxyError as exc:
                assert '403' in str(exc)
            else:
                raise AssertionError('suffix authority bypassed relay')
    with open_service_request('http://retrieval:8080/', timeout=3,
            environment_name='CASE_INTELLIGENCE_RETRIEVAL_ALLOWED_HOSTS') as response:
        assert response.status == 200
    # Success above proves reachability. Numeric addresses avoid a DNS-only false pass.
    for address in (os.environ["ISSUER_ADDRESS"], os.environ["FOREIGN_ADDRESS"]):
        try:
            with socket.create_connection((address, 8443), timeout=2):
                raise AssertionError("direct numeric TCP escaped internal network")
        except OSError:
            pass
        async with httpx.AsyncClient(trust_env=False, verify=CA, timeout=2) as client:
            try:
                await client.get(f"https://{address}:8443/")
                raise AssertionError("proxy-bypassing HTTPX escaped internal network")
            except httpx.ConnectError as exc:
                # Certificate errors prove a connection, so cannot count as isolation.
                assert "CERTIFICATE" not in str(exc).upper()
            except httpx.ConnectTimeout:
                pass
    for target in ("foreign.example.test:8443", "issuer.example.test:443", "issuer.example.test:8443/path"):
        denied_connect(target)
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            await client.get(ISSUER)
            raise AssertionError("custom CA must be required")
        except httpx.ConnectError as exc:
            assert "CERTIFICATE" in str(exc).upper()
    print(json.dumps({"authlib_discovery_token_jwks": True, "custom_ca_required": True, "authlib_custom_ca_required": True,
        "authlib_redirects_not_followed": True, "metadata_transport_override_ignored": True,
        "plain_http_relay_denied": True, "suffix_hosts_relay_denied": True, "exact_internal_service_reachable": True, "direct_numeric_tcp_denied": True,
        "trust_env_false_denied": True, "foreign_and_wrong_port_denied": True}))


asyncio.run(main())
