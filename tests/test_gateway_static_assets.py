"""Synthetic regression for the installed HTTPS gateway's internal HTTP hop."""
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from fastapi.testclient import TestClient

from tests.test_browser_local_accounts import configured_app, login


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        value = values.get("src") if tag in {"script", "img"} else values.get("href") if tag == "link" else None
        if value and "/static/" in value:
            self.urls.append(value)


def test_workspace_assets_retain_browser_https_origin_across_internal_http_hop(tmp_path):
    app, _ = configured_app(tmp_path)
    # Nginx terminates TLS and forwards Host without the external port. Do not
    # broaden trusted proxy headers to manufacture the browser's origin here.

    @app.middleware("http")
    async def gateway_hop(request, call_next):
        request.scope["scheme"] = "http"
        request.scope["headers"] = [
            (key, b"localhost" if key == b"host" else value)
            for key, value in request.scope["headers"]
        ]
        return await call_next(request)

    with TestClient(app, base_url="https://localhost:8443") as backend:
        assert login(backend).status_code == 303
        response = backend.get("/admin/setup", headers={"X-Forwarded-Proto": "https"})
        assert response.status_code == 200
        assets = Assets()
        assets.feed(response.text)
        assert any("case-intelligence.js" in url for url in assets.urls)
        assert any("case-intelligence.css" in url for url in assets.urls)
        for asset in assets.urls:
            resolved = urlsplit(urljoin("https://localhost:8443/auth/login", asset))
            assert (resolved.scheme, resolved.netloc) == ("https", "localhost:8443")
            assert backend.get(resolved.path).status_code == 200


def test_gateway_and_kerberos_templates_clear_both_names_before_trusted_assignment():
    from pathlib import Path
    import re

    root = Path(__file__).parents[1]
    gateway = (root / "deploy/gateway/default.conf.template").read_text()
    apache = (root / "deploy/kerberos-proxy/recordbench-auth.conf.template").read_text()
    entrypoint = (root / "deploy/kerberos-proxy/entrypoint").read_text()
    locations = re.findall(r"location\s+[^{}]+\{(.*?)^    \}", gateway, flags=re.M | re.S)
    assert len(locations) == 2
    for brand in ("Exculpata", "RecordBench"):
        for suffix in ("Authenticated-User", "Proxy-Secret", "Auth-Diagnostic"):
            name = f"X-{brand}-{suffix}"
            assert all(f'proxy_set_header {name} "";' in location for location in locations)
            unset = f"RequestHeader unset {name} early"
            assert unset in apache
            if suffix == "Authenticated-User":
                assert apache.index(unset) < apache.index(f'RequestHeader set {name} "expr=%{{REMOTE_USER}}"')
            elif suffix == "Proxy-Secret":
                assert apache.index(unset) < apache.index("Include /run/recordbench-auth/proxy-secret.conf")
                assert f'RequestHeader set {name} "%s"' in entrypoint
    compose = (root / "compose.kerberos.yaml").read_text()
    assert "RECORDBENCH_UPSTREAM: kerberos-proxy:8080" in compose
    assert not re.search(r"^\s+ports:", compose, flags=re.M)


def test_gateway_contract_strips_forged_aliases_on_every_proxy_location(tmp_path):
    """Feed the headers forwarded by each template location to the real app."""
    from pathlib import Path
    import re
    from case_intelligence.workbench import create_workbench_app
    from case_intelligence.generation import UnavailableGenerator
    from case_intelligence.identity import SESSION_COOKIE
    from tests.test_kerberos_authentication import _settings, _profile, _headers

    template = (Path(__file__).parents[1] / "deploy/gateway/default.conf.template").read_text()
    locations = re.findall(r"location\s+([^{}]+)\{(.*?)^    \}", template, flags=re.M | re.S)
    assert len(locations) == 2
    app = create_workbench_app(tmp_path / "runtime", auth_mode="kerberos", secure_cookie=True,
                              generator=UnavailableGenerator(), kerberos_settings=_settings(),
                              kerberos_profile_resolver=_profile)
    with TestClient(app, base_url="https://recordbench.example.test") as client:
        for _, location in locations:
            removed = {name.lower() for name in re.findall(r'proxy_set_header\s+(\S+)\s+"";', location)}
            for mode in ("old-only", "new-only", "both-equal", "both-different"):
                forged = {}
                for name, value in _headers().items():
                    if mode != "new-only":
                        forged[name.lower()] = value
                    if mode != "old-only":
                        forged[name.replace("RecordBench", "Exculpata").lower()] = "synthetic-forged" if mode == "both-different" else value
                forwarded = {name: value for name, value in forged.items() if name not in removed}
                assert forwarded == {}
                assert client.get("/auth/login", headers=forwarded).status_code == 401
                assert client.cookies.get(SESSION_COOKIE) is None
