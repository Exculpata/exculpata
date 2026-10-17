"""Disposable synthetic OIDC endpoint; never used by the application deployment."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ssl
import time
from urllib.parse import parse_qs

from joserfc import jwt
from joserfc.jwk import RSAKey
from dns_trap import Trap

ISSUER = "https://issuer.example.test:8443/realm"
KEY = RSAKey.generate_key(2048, parameters={"kid": "synthetic-smoke"})
EVENTS = []
DNS = Trap("0.0.0.0")


class Endpoint(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value, status=200):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/dns-count":
            return self.reply({"queries": DNS.count})
        if self.path.endswith("/.well-known/openid-configuration"):
            EVENTS.append("discovery")
            return self.reply({"issuer": ISSUER, "authorization_endpoint": ISSUER + "/authorize",
                "token_endpoint": ISSUER + "/token", "jwks_uri": ISSUER + "/jwks",
                "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["client_secret_post"],
                "id_token_signing_alg_values_supported": ["RS256"]})
        if self.path == "/realm/jwks":
            EVENTS.append("jwks")
            return self.reply({"keys": [KEY.as_dict(private=False)]})
        if self.path == "/events":
            return self.reply(EVENTS)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "https://foreign.example.test:8443/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.reply({"synthetic": True})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length < 4096:
            return self.reply({}, 400)
        fields = parse_qs(self.rfile.read(length).decode())
        if (self.path != "/realm/token" or fields.get("code") != ["synthetic-code"]
                or fields.get("code_verifier") != ["v" * 64]
                or fields.get("client_id") != ["synthetic-client"]
                or fields.get("client_secret") != ["synthetic-smoke-secret-value"]):
            return self.reply({}, 400)
        EVENTS.append("token")
        now = int(time.time())
        token = jwt.encode({"alg": "RS256", "kid": "synthetic-smoke"},
            {"iss": ISSUER, "aud": "synthetic-client", "sub": "synthetic-subject",
             "iat": now, "exp": now + 300, "nonce": "synthetic-nonce"}, KEY)
        self.reply({"access_token": "synthetic-access", "token_type": "Bearer", "id_token": token})


server = ThreadingHTTPServer(("0.0.0.0", 8443), Endpoint)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain("/fixture/issuer.pem", "/fixture/issuer-key.pem")
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
