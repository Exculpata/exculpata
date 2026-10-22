"""Bounded synthetic OIDC/DNS recorder; no forwarding, ports or mounts on host."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ssl
import threading
import time
from urllib.parse import parse_qs

from joserfc import jwt
from joserfc.jwk import RSAKey
from dns_trap import Trap

ORIGIN = "https://issuer.example.test:8443"
KEY = RSAKey.generate_key(2048, parameters={"kid": "synthetic-smoke"})
EVENTS = []
DNS = Trap("0.0.0.0")


class Endpoint(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def event(self, name):
        if len(EVENTS) < 128:
            EVENTS.append(name)

    def reply(self, value, status=200):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, mode):
        self.send_response(307)
        # Same allowed authority: a proxy host denial cannot mask a redirect.
        self.send_header("Location", ORIGIN + "/" + mode + "/redirect-target")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        mode = self.path.split('/')[1]
        issuer = ORIGIN + '/' + mode
        if self.path == "/dns-count":
            return self.reply({"queries": DNS.count})
        if self.path == "/events":
            return self.reply(EVENTS)
        if self.path.endswith('/redirect-target'):
            self.event(mode + ':redirect-target')
            return self.reply({}, 400)
        if self.path.endswith("/.well-known/openid-configuration"):
            self.event(mode + ':discovery')
            if mode == 'discovery-redirect':
                return self.redirect(mode)
            metadata = {"issuer": issuer, "authorization_endpoint": issuer + "/authorize",
                "token_endpoint": issuer + "/token", "jwks_uri": issuer + "/jwks",
                "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["client_secret_post"],
                "id_token_signing_alg_values_supported": ["RS256"]}
            if mode in {'malicious', 'metadata-redirect'}:
                metadata.update(follow_redirects=True, trust_env=True, verify=False,
                                headers={'Authorization':'synthetic-metadata-header'})
            if mode == 'malicious':
                metadata.update(proxy='http://foreign.example.test:8080', timeout=0,
                                client_secret='synthetic-wrong-secret')
            return self.reply(metadata)
        if self.path.endswith('/jwks'):
            self.event(mode + ':jwks')
            if mode == 'jwks-redirect':
                return self.redirect(mode)
            return self.reply({"keys": [KEY.as_dict(private=False)]})
        self.reply({"synthetic": True})

    def do_POST(self):
        mode = self.path.split('/')[1]
        if self.path.endswith('/redirect-target'):
            self.event(mode + ':redirect-target')
            return self.reply({}, 400)
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length < 4096:
            return self.reply({}, 400)
        fields = parse_qs(self.rfile.read(length).decode())
        if (not self.path.endswith('/token') or fields.get("code") != ["synthetic-code"]
                or fields.get("code_verifier") != ["v" * 64]
                or fields.get("client_id") != ["synthetic-client"]
                or fields.get("client_secret") != ["synthetic-smoke-secret-value"]):
            return self.reply({}, 400)
        self.event(mode + ':token')
        if mode in {'token-redirect', 'metadata-redirect'}:
            return self.redirect(mode)
        now = int(time.time())
        token = jwt.encode({"alg": "RS256", "kid": "synthetic-smoke"},
            {"iss": ORIGIN + '/' + mode, "aud": "synthetic-client", "sub": "synthetic-subject",
             "iat": now, "exp": now + 300, "nonce": "synthetic-nonce"}, KEY)
        self.reply({"access_token": "synthetic-access", "token_type": "Bearer", "id_token": token})


plain = ThreadingHTTPServer(("0.0.0.0", 8080), Endpoint)
threading.Thread(target=plain.serve_forever, daemon=True).start()
server = ThreadingHTTPServer(("0.0.0.0", 8443), Endpoint)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain("/fixture/issuer.pem", "/fixture/issuer-key.pem")
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
