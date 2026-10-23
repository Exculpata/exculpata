"""Run the deployed gateway template against an observing upstream.

Requires Docker and OpenSSL. CI's deployment-contract job sets PYTHONPATH=src
so the naming constants import without installing the package. A missing
runtime is reported as an explicit skip.
"""

from case_intelligence.legacy_names import (
    LEGACY_PRODUCT,
)
from pathlib import Path
import hashlib
import re
import shlex
import subprocess
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]


def require_docker():
    try:
        run("docker", "info", timeout=15)
    except (OSError, subprocess.SubprocessError):
        raise unittest.SkipTest("Docker unavailable: CLI missing or daemon inaccessible (including socket permission denied)")


def access_log_pattern(code):
    return rf"(?m)^gateway time=\S+ status={code} bytes=\d+ duration=\d+\.\d+$"


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
    @classmethod
    def setUpClass(cls):
        require_docker()

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
            # fixture-write: Generate disposable synthetic TLS certificate and key.
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
            # fixture-write: Render a synthetic observer beside the gateway.
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
                # fixture-write: Remove the synthetic observer to exercise upstream failure.
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
                        encoded = "".join(f"%{ord(char):02X}" for char in marker)
                        for value in (marker, encoded, encoded.lower()):
                            self.assertNotIn(value, stream)
                for code in (200, 400, 414, 502, 503):
                    self.assertRegex(logs.stdout, access_log_pattern(code))
            finally:
                run("docker", "rm", "-f", container)


def nginx_directives(config):
    """Read the reviewed nginx directive/block syntax, including nginx -T dumps."""
    config = re.sub(r"\$\{(\w+)\}", r"$\1", config)
    lexer = shlex.shlex(config, posix=True, punctuation_chars="{};")
    lexer.whitespace_split = True
    tokens = []
    for token in lexer:
        tokens.extend(token if re.fullmatch(r"[{};]+", token) else [token])
    root, stack, words = [], [], []
    current = root
    for token in tokens:
        if token == "{":
            assert words, "unsupported block: use nginx_directives with reviewed syntax"
            children = []
            current.append((words, children))
            stack.append(current)
            current, words = children, []
        elif token == "}":
            assert stack and not words, "unsupported block end: use nginx_directives with reviewed syntax"
            current = stack.pop()
        elif token == ";":
            assert words, "unsupported directive: use nginx_directives with reviewed syntax"
            current.append((words, None))
            words = []
        else:
            words.append(token)
    assert not stack and not words, "unsupported incomplete config: use nginx_directives with reviewed syntax"
    return root


def walk_directives(nodes):
    for words, children in nodes:
        yield words, children
        if children is not None:
            yield from walk_directives(children)


def assert_server_logging(config):
    nodes = list(walk_directives(nginx_directives(config)))
    servers = [children for words, children in nodes if words == ["server"]]
    assert servers, "no gateway servers found"
    for server in servers:
        logs = [words[1:] for words, _ in server if words[0] == "access_log"]
        assert logs == [["/dev/stdout", "exculpata_safe"]], "server needs explicit safe access_log"
        for words, _ in walk_directives(server):
            if words[0] == "access_log":
                assert words[1:] in (["off"], ["/dev/stdout", "exculpata_safe"]), "unsafe access_log"


def assert_gateway_logging(template):
    nodes = nginx_directives(template)
    flattened = list(walk_directives(nodes))
    assert not any(words[0] == "include" for words, _ in flattened), (
        "unsupported include: use assert_gateway_logging with a self-contained template")
    formats = [words for words, _ in flattened if words[0] == "log_format"]
    assert len(formats) == 1 and formats[0][1] == "exculpata_safe", "unexpected log_format"
    assert set(re.findall(r"\$[a-zA-Z_0-9]+", " ".join(formats[0][2:]))) == {
        "$time_iso8601", "$status", "$body_bytes_sent", "$request_time"}, "unsafe log_format variables"
    assert (formats[0], None) in nodes, "log_format must be at template top level"
    errors = [(words, children) for words, children in flattened if words[0] == "error_log"]
    assert errors == [(["error_log", "/dev/null"], None)], "unsafe error_log"
    assert errors[0] in nodes, "error_log must be at template top level"
    assert_server_logging(template)


class GatewayImageContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_docker()
        cls.image = f"synthetic-gateway-privacy:{uuid.uuid4().hex}"
        cls.addClassCleanup(run, "docker", "image", "rm", "-f", cls.image)
        run("docker", "build", "-t", cls.image, str(ROOT / "deploy/gateway"), timeout=300)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-gateway-image-")
        self.addCleanup(temporary.cleanup)
        self.scratch = Path(temporary.name)
        # fixture-write: Generate disposable synthetic TLS certificate and key.
        run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-keyout", str(self.scratch / "tls.key"), "-out", str(self.scratch / "tls.crt"),
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost")

    def start_gateway(self, *environment):
        container = f"synthetic-gateway-image-{uuid.uuid4().hex}"
        self.addCleanup(run, "docker", "rm", "-f", container)
        run("docker", "run", "-d", "--name", container, "--network", "none",
            "-e", "EXCULPATA_SERVER_NAME=localhost", "-e", "EXCULPATA_UPSTREAM=127.0.0.1:8786",
            *environment, "-v", f"{self.scratch}:/run/tls:ro",
            "-v", f"{ROOT / 'deploy/gateway/default.conf.template'}:/etc/nginx/templates/gateway.conf.template:ro",
            self.image, timeout=180)
        for attempt in range(40):
            try:
                config = run("docker", "exec", container, "nginx", "-T")
                if "server_name localhost;" in config:
                    run("docker", "exec", container, "curl", "--silent", "--show-error",
                        "--insecure", "--max-time", "1", "-o", "/dev/null",
                        "https://127.0.0.1:8443/")
                    return container, config
            except subprocess.CalledProcessError:
                if attempt == 39:
                    raise
            time.sleep(0.25)
        self.fail("real entrypoint did not render the gateway template")

    def test_entrypoint_has_only_safe_listeners_and_logs(self):
        container, config = self.start_gateway()
        listeners = set()
        for words, _ in walk_directives(nginx_directives(config)):
            if words[0] != "listen":
                continue
            endpoint = words[1]
            host = "[::1]" if endpoint.startswith("[") else "127.0.0.1"
            port = endpoint.rsplit(":", 1)[-1]
            self.assertTrue(port.isdigit(), "unsupported listener; extend the request probe")
            listeners.add(("https" if "ssl" in words else "http", host, port))
        self.assertTrue(listeners)
        # Probe the stock port even when it is absent from nginx -T.
        probes = listeners | {("http", "127.0.0.1", "80")}
        marker = "SYNTHETIC-PRIVATE-336"
        encoded = "".join(f"%{ord(char):02X}" for char in marker)
        for scheme, host, port in sorted(probes):
            for value in (marker, encoded):
                response = subprocess.run([
                    "docker", "exec", container, "curl", "--silent", "--show-error",
                    "--noproxy", "*", "--insecure", "--max-time", "5",
                    "-H", "Referer: https://example.test/" + value,
                    "-H", "User-Agent: " + value, "-o", "/dev/null", "-w", "%{http_code}",
                    f"{scheme}://{host}:{port}/{value}?q={value}",
                ], capture_output=True, text=True, timeout=15)
                if (scheme, host, port) not in listeners and response.returncode == 7:
                    continue
                self.assertEqual(response.returncode, 0, response.stderr)
                self.assertRegex(response.stdout, r"^[1-5][0-9]{2}$")
        run("docker", "stop", "-t", "5", container)
        logs = subprocess.run(["docker", "logs", container], check=True,
                              capture_output=True, text=True, timeout=15)
        for stream in (logs.stdout, logs.stderr):
            for value in (marker, encoded, encoded.lower()):
                self.assertNotIn(value, stream)
        assert_server_logging(config)
        self.assertRegex(logs.stdout, access_log_pattern(502))

    def test_entrypoint_preserves_variables_with_and_without_compose_filter(self):
        compose = (ROOT / "compose.yaml").read_text()
        compose_filter = re.search(r"NGINX_ENVSUBST_FILTER: (\S+)", compose).group(1)
        template = (ROOT / "deploy/gateway/default.conf.template").read_text()
        collisions = ["status", "request_time", "host", "http_authorization", "binary_remote_addr"]
        environment = [arg for name in collisions for arg in ("-e", name + "=SYNTHETIC-CLOBBER")]
        for extra in ([], ["-e", "NGINX_ENVSUBST_FILTER=" + compose_filter]):
            with self.subTest(compose_filter=bool(extra)):
                _, config = self.start_gateway(*environment, *extra)
                self.assertEqual(re.findall(r"log_format exculpata_safe[^;]+;", config),
                                 re.findall(r"log_format exculpata_safe[^;]+;", template))
                for name in collisions:
                    self.assertIn("$" + name, config)
                self.assertNotIn("SYNTHETIC-CLOBBER", config)
                self.assertNotIn("${EXCULPATA_", config)


class GatewayLoggingRegressions(unittest.TestCase):
    def test_gateway_logging_has_only_content_free_fields(self):
        assert_gateway_logging((ROOT / "deploy/gateway/default.conf.template").read_text())

    def test_allowlist_is_discovered_by_unittest(self):
        import sys
        suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        names = [test._testMethodName for group in suite for test in group]
        self.assertIn("test_gateway_logging_has_only_content_free_fields", names)

    def test_allowlist_rejects_unlogged_server_and_request_formats(self):
        template = (ROOT / "deploy/gateway/default.conf.template").read_text()
        mutations = [template + "\nserver { listen 8080; }\n",
                     template + "\ninclude /synthetic/extra.conf;\n"]
        for variable in ("request", "request_uri", "uri", "args", "http_referer",
                         "http_user_agent", "remote_addr", "host"):
            mutations += [template.rstrip() + f"\nlog_format leak '${variable}';\n",
                          template.replace("$status", f"${variable}")]
        for mutation in mutations:
            with self.subTest(mutation=mutation[-100:]):
                with self.assertRaises(AssertionError):
                    assert_gateway_logging(mutation)

    def test_status_pattern_rejects_extra_fields(self):
        line = "gateway time=2026-01-01T00:00:00+00:00 status=200 bytes=0 duration=0.001"
        self.assertIsNotNone(re.search(access_log_pattern(200), line))
        for changed in (line + " 192.0.2.1", "192.0.2.1 " + line):
            with self.subTest(line=changed):
                self.assertIsNone(re.search(access_log_pattern(200), changed))

    def test_image_removes_stock_server(self):
        dockerfile = (ROOT / "deploy/gateway/Dockerfile").read_text()
        self.assertRegex(dockerfile, r"(?m)^RUN .*rm -f /etc/nginx/conf\.d/default\.conf(?:\s|$)")

    def test_image_filters_envsubst_without_compose(self):
        dockerfile = (ROOT / "deploy/gateway/Dockerfile").read_text()
        self.assertIn("ENV NGINX_ENVSUBST_FILTER=^EXCULPATA_", dockerfile)


if __name__ == "__main__":
    unittest.main()
