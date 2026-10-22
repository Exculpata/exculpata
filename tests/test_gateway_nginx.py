"""Run the deployed gateway template against an observing upstream.

Requires Docker and OpenSSL. CI's deployment-contract job sets PYTHONPATH=src
so the naming constants import without installing the package. A missing
runtime still cannot silently skip the test.
"""

from case_intelligence.legacy_names import (
    LEGACY_PRODUCT,
)
from pathlib import Path
import hashlib
import re
import subprocess
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]


def run(*command, timeout=60):
    return subprocess.run(command, check=True, capture_output=True, text=True,
                          timeout=timeout).stdout.strip()


def assert_reviewed_nginx_conf(config):
    # Pin the enclosing scopes, includes and inherited logging directives, not
    # just the template. Image updates require review of both configurations.
    assert hashlib.sha256(config).hexdigest() == (
        "4dee6b0c7c8278f6a9b827261c25d3215c9ea8955e41ff7fe3eabdd5297edce7"
    ), "nginx.conf changed: review gateway logging scopes/settings before updating the pin"


class GatewayNginxConfigPin(unittest.TestCase):
    def test_gateway_nginx_conf_matches_reviewed_pin(self):
        assert_reviewed_nginx_conf(
            (ROOT / "tests/fixtures/gateway/nginx.conf").read_bytes())

    def test_gateway_nginx_conf_pin_rejects_logging_drift(self):
        config = (ROOT / "tests/fixtures/gateway/nginx.conf").read_bytes()
        mutations = (
            (b'"$request"', b'"$request_uri"'),
            (b"access_log  /var/log/nginx/access.log  main;", b"access_log off;"),
            (b"error.log notice;", b"error.log debug;"),
            (b"include /etc/nginx/conf.d/*.conf;",
             b"include /etc/nginx/conf.d/*.conf;\n    error_log /dev/stderr;"),
        )
        for before, after in mutations:
            with self.subTest(directive=before):
                self.assertIn(before, config)
                with self.assertRaisesRegex(AssertionError, "nginx.conf changed"):
                    assert_reviewed_nginx_conf(config.replace(before, after, 1))


class GatewayHeaderContract(unittest.TestCase):
    def test_real_gateway_strips_both_auth_header_families(self):
        image = next(line.split()[1] for line in
                     (ROOT / "deploy/gateway/Dockerfile").read_text().splitlines()
                     if line.startswith("FROM "))
        container = f"synthetic-gateway-{uuid.uuid4().hex}"
        names = [f"X-{brand}-{suffix}" for brand in ("Exculpata", LEGACY_PRODUCT)
                 for suffix in ("Authenticated-User", "Proxy-Secret", "Auth-Diagnostic")]
        absent = "|".join("" for _ in names)
        with tempfile.TemporaryDirectory(prefix="synthetic-gateway-") as temporary:
            scratch = Path(temporary)
            run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                "-keyout", str(scratch / "tls.key"), "-out", str(scratch / "tls.crt"),
                "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost")
            template = (ROOT / "deploy/gateway/default.conf.template").read_text()
            rendered = template.replace("${EXCULPATA_SERVER_NAME}", "localhost").replace(
                "${EXCULPATA_UPSTREAM}", "127.0.0.1:8786")
            # A separate HTTP server observes the headers that actually arrive
            # after proxying; the TLS gateway configuration is otherwise intact.
            variables = "|".join("$http_" + name.lower().replace("-", "_") for name in names)
            rendered += '\nserver { listen 127.0.0.1:8786; access_log off; location / { return 200 "' + variables + '"; } }\n'
            (scratch / "default.conf").write_text(rendered)
            try:
                run("docker", "run", "-d", "--name", container, "--network", "none",
                    "-v", f'{scratch / "default.conf"}:/etc/nginx/conf.d/default.conf:ro',
                    "-v", f"{scratch}:/run/tls:ro", image, timeout=180)
                nginx_conf = subprocess.run(
                    ["docker", "exec", container, "cat", "/etc/nginx/nginx.conf"],
                    check=True, capture_output=True, timeout=15).stdout
                assert_reviewed_nginx_conf(nginx_conf)

                def request(path, headers=None, *, direct=False):
                    command = ["docker", "exec", container, "curl", "--silent", "--show-error",
                               "--fail", "--max-time", "5", "--cacert", "/run/tls/tls.crt"]
                    for name, value in (headers or {}).items():
                        command += ["-H", f"{name}: {value}"]
                    origin = "http://localhost:8786" if direct else "https://localhost:8443"
                    return run(*command, origin + path, timeout=15)

                for attempt in range(40):
                    try:
                        self.assertEqual(request("/"), absent)
                        break
                    except subprocess.CalledProcessError:
                        if attempt == 39:
                            raise
                        time.sleep(0.25)
                for path in ("/", "/auth/local"):
                    for mode in ("new-only", "old-only", "both-equal", "both-different"):
                        with self.subTest(path=path, mode=mode):
                            forged = {}
                            for name in names:
                                if mode == "new-only" and LEGACY_PRODUCT in name:
                                    continue
                                if mode == "old-only" and "Exculpata" in name:
                                    continue
                                forged[name] = ("synthetic-different" if mode == "both-different"
                                                and LEGACY_PRODUCT in name else "synthetic-forged")
                            # A control for every case proves the observer sees
                            # supplied headers when the gateway is bypassed.
                            self.assertEqual(request(path, forged, direct=True),
                                             "|".join(forged.get(name, "") for name in names))
                            self.assertEqual(request(path, forged), absent)
                markers = ["SYNTHETIC-SEARCH-336", "SYNTHETIC-CODE-336",
                           "SYNTHETIC-STATE-336", "SYNTHETIC-COOKIE-336",
                           "SYNTHETIC-AUTHORIZATION-336", "SYNTHETIC-REFERER-336",
                           "SYNTHETIC-AGENT-336", "SYNTHETIC-BODY-336",
                           "synthetic-matter-336"]
                paths = ["/matters/synthetic-matter-336?q=" + markers[0],
                         "/auth/oidc/callback?code=" + markers[1] + "&state=" + markers[2],
                         "/auth/login?next=%2Fmatters%2Fsynthetic-matter-336%3Fq%3D" + markers[0]]
                private_headers = {"Cookie": "session=" + markers[3],
                                   "Authorization": "Bearer " + markers[4],
                                   "Referer": "https://example.test/" + markers[5],
                                   "User-Agent": markers[6]}
                for path in paths:
                    self.assertEqual(request(path, private_headers), absent)

                def status(path, *extra):
                    return run("docker", "exec", container, "curl", "--silent", "--show-error",
                               "--max-time", "5", "--cacert", "/run/tls/tls.crt",
                               *extra, "--data", markers[7], "-o", "/dev/null", "-w", "%{http_code}",
                               "https://localhost:8443" + path)

                self.assertEqual(status("/", "-H", "Host: invalid host-" + markers[0]), "400")
                self.assertEqual(status("/" + markers[0] * 1000), "414")
                statuses = [status("/auth/local?q=" + markers[0]) for _ in range(12)]
                self.assertIn("503", statuses)  # The unchanged nginx rate-limit response.
                # Remove only the synthetic observer. The deployed gateway stays intact.
                (scratch / "default.conf").write_text(rendered.split("\nserver { listen 127.0.0.1:8786;")[0])
                run("docker", "exec", container, "nginx", "-s", "reload")
                for attempt in range(40):
                    if status(paths[1]) == "502":
                        break
                    time.sleep(0.1)
                else:
                    self.fail("synthetic upstream failure was not observed")
                logs = subprocess.run(["docker", "logs", container], check=True,
                                      capture_output=True, text=True, timeout=15)
                for stream in (logs.stdout, logs.stderr):
                    for marker in markers:
                        self.assertNotIn(marker, stream)
                for code in (200, 400, 414, 502, 503):
                    self.assertRegex(logs.stdout, rf"gateway time=\S+ status={code} bytes=\d+ duration=[\d.]+")
            finally:
                run("docker", "rm", "-f", container)


if __name__ == "__main__":
    unittest.main()


def test_gateway_logging_has_only_content_free_fields():
    template = (ROOT / "deploy/gateway/default.conf.template").read_text()
    formats = re.findall(r"log_format\s+exculpata_safe\s+([^;]+);", template)
    assert len(formats) == 1
    assert set(re.findall(r"\$[a-zA-Z_0-9]+", formats[0])) == {
        "$time_iso8601", "$status", "$body_bytes_sent", "$request_time"}
    assert re.findall(r"^\s*access_log\s+([^;]+);", template, re.M) == ["/dev/stdout exculpata_safe"]
    assert re.findall(r"^error_log\s+([^;]+);", template, re.M) == ["/dev/null"]
    assert not re.search(r"^\s+(error_log|log_format)\s", template, re.M)
