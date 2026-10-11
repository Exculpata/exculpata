"""Run the deployed gateway template against an observing upstream.

Requires Docker and OpenSSL; also runs directly with the standard library in
CI's deployment-contract job so a missing runtime cannot silently skip it.
"""
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]


def run(*command, timeout=60):
    return subprocess.run(command, check=True, capture_output=True, text=True,
                          timeout=timeout).stdout.strip()


class GatewayHeaderContract(unittest.TestCase):
    def test_real_gateway_strips_both_auth_header_families(self):
        image = next(line.split()[1] for line in
                     (ROOT / "deploy/gateway/Dockerfile").read_text().splitlines()
                     if line.startswith("FROM "))
        container = f"synthetic-gateway-{uuid.uuid4().hex}"
        names = [f"X-{brand}-{suffix}" for brand in ("Exculpata", "RecordBench")
                 for suffix in ("Authenticated-User", "Proxy-Secret", "Auth-Diagnostic")]
        absent = "|".join("" for _ in names)
        with tempfile.TemporaryDirectory(prefix="synthetic-gateway-") as temporary:
            scratch = Path(temporary)
            run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                "-keyout", str(scratch / "tls.key"), "-out", str(scratch / "tls.crt"),
                "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost")
            template = (ROOT / "deploy/gateway/default.conf.template").read_text()
            rendered = template.replace("${RECORDBENCH_SERVER_NAME}", "localhost").replace(
                "${RECORDBENCH_UPSTREAM}", "127.0.0.1:8786")
            # A separate HTTP server observes the headers that actually arrive
            # after proxying; the TLS gateway configuration is otherwise intact.
            variables = "|".join("$http_" + name.lower().replace("-", "_") for name in names)
            rendered += '\nserver { listen 127.0.0.1:8786; location / { return 200 "' + variables + '"; } }\n'
            (scratch / "default.conf").write_text(rendered)
            try:
                run("docker", "run", "-d", "--name", container, "--network", "none",
                    "-v", f'{scratch / "default.conf"}:/etc/nginx/conf.d/default.conf:ro',
                    "-v", f"{scratch}:/run/tls:ro", image, timeout=180)

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
                                if mode == "new-only" and "RecordBench" in name:
                                    continue
                                if mode == "old-only" and "Exculpata" in name:
                                    continue
                                forged[name] = ("synthetic-different" if mode == "both-different"
                                                and "RecordBench" in name else "synthetic-forged")
                            # A control for every case proves the observer sees
                            # supplied headers when the gateway is bypassed.
                            self.assertEqual(request(path, forged, direct=True),
                                             "|".join(forged.get(name, "") for name in names))
                            self.assertEqual(request(path, forged), absent)
            finally:
                run("docker", "rm", "-f", container)


if __name__ == "__main__":
    unittest.main()
