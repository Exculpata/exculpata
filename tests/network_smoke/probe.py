"""Real Authlib adapter through the relay plus independent direct TCP denials."""
import asyncio
import json
import os
from pathlib import Path
import socket
import ssl
import sys

import httpx
from case_intelligence.identity import AuthlibOidcClient, OidcSettings

ISSUER = "https://issuer.example.test:8443/realm"
CA = ssl.create_default_context(cafile="/fixture/issuer.pem")


def denied_connect(target):
    with socket.create_connection(("proxy", 3128), timeout=3) as client:
        client.sendall(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
        assert client.recv(4096).startswith(b"HTTP/1.1 403"), "CONNECT must be denied"


async def main():
    if len(sys.argv) > 1 and sys.argv[1] == "deny-all":
        denied_connect("issuer.example.test:8443")
        print(json.dumps({"no_issuer_healthy_deny_all": True}))
        return
    settings = OidcSettings(issuer=ISSUER, external_origin="https://app.example.test",
        client_id="synthetic-client", client_secret="synthetic-smoke-secret-value",
        ca_file=Path("/fixture/issuer.pem"))
    oidc = AuthlibOidcClient(settings)
    url = await oidc.authorization_url(state="synthetic-state", nonce="synthetic-nonce", code_verifier="v" * 64)
    assert url.startswith(ISSUER + "/authorize?")
    claims = await oidc.authenticate(code="synthetic-code", nonce="synthetic-nonce", code_verifier="v" * 64)
    assert claims["sub"] == "synthetic-subject"
    async with httpx.AsyncClient(verify=CA, follow_redirects=False, timeout=5) as client:
        assert {"discovery", "token", "jwks"} <= set((await client.get(ISSUER.replace("/realm", "/events"))).json())
        assert (await client.get(ISSUER.replace("/realm", "/redirect"))).status_code == 302
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
    print(json.dumps({"authlib_discovery_token_jwks": True, "custom_ca_required": True,
        "redirect_not_followed": True, "direct_numeric_tcp_denied": True,
        "trust_env_false_denied": True, "foreign_and_wrong_port_denied": True}))


asyncio.run(main())
