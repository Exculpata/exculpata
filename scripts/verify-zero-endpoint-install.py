"""Exercise the documented CPU installation against a fresh root on a disposable host."""

import argparse
import hashlib
import html
from http.cookiejar import CookieJar
import json
from pathlib import Path
import re
import secrets
import ssl
import subprocess
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPSHandler, ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
ADMIN = "synthetic-owner"
PHRASE = "synthetic indigo inventory marker"
CSV_NAME = "synthetic-inventory.csv"
SCAN_NAME = "synthetic-scan.pdf"


class Gateway:
    def __init__(self, node, port):
        self.origin = f"https://localhost:{port}"
        self.cookies = CookieJar()
        self.opener = build_opener(
            ProxyHandler({}), HTTPCookieProcessor(self.cookies),
            HTTPSHandler(context=ssl.create_default_context(cafile=str(node / "tls" / "tls.crt"))),
        )
        self.csrf = None

    def request(self, path, *, fields=None, body=None, content_type=None):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Expected a local application path")
        headers = {"X-CSRF-Token": self.csrf} if self.csrf else {}
        if fields is not None:
            body = urlencode(fields).encode()
            content_type = "application/x-www-form-urlencoded"
        if content_type:
            headers["Content-Type"] = content_type
        with self.opener.open(Request(self.origin + path, data=body, headers=headers), timeout=60) as reply:
            return urlsplit(reply.url).path, reply.read()

    def page(self, path):
        return self.request(path)[1].decode()

    def login(self, password):
        self.page("/auth/login?next=/matters/manage")
        challenge = next(c.value for c in self.cookies if c.name == "case_intelligence_login_challenge")
        path, body = self.request("/auth/local", fields={
            "username": ADMIN, "password": password, "login_challenge": challenge, "next": "/matters/manage",
        })
        assert path == "/matters/manage", "Local account sign-in failed"
        self.csrf = re.search(r'data-csrf-token="([0-9a-f]{64})"', body.decode()).group(1)

    def upload(self, matter, name, media_type, data):
        boundary = "synthetic-" + secrets.token_hex(16)
        before = (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
                  f"Content-Type: {media_type}\r\n\r\n").encode()
        self.request(matter + "/uploads", body=before + data + f"\r\n--{boundary}--\r\n".encode(),
                     content_type="multipart/form-data; boundary=" + boundary)

    def exact_match(self, matter, phrase, filename):
        query = urlencode({"phrase": phrase, "search": "true"})
        for _ in range(90):
            page = self.page(matter + "/exact-search?" + query)
            if 'class="find-document"' in page:
                assert page.count('class="find-document"') == 1 and filename in page
                hit = re.search(r'<a class="find-location" href="([^"]+)">(.*?)</a>', page)
                return html.unescape(hit.group(1)), html.unescape(hit.group(2))
            time.sleep(2)
        raise AssertionError("Expected exact-search result did not appear")

    def review(self, matter, citation, data):
        source = urlsplit(citation).path
        assert CSV_NAME in self.page(citation)
        assert self.request(source + "/content")[1] == data, "Original CSV bytes changed"
        label = "Verify inventory"
        self.request(matter + "/production/codes", fields={"label": label})
        item = matter + "/production/items/" + source.rsplit("/", 1)[1]
        page = self.page(item)
        assert label in page
        code = re.search(r'data-code-id="([^"]+)"', page).group(1)
        self.request(item + "/codes/" + code, fields={"action": "apply"})
        assert re.search(r'data-code-id="' + re.escape(code) + r'"[^>]*aria-pressed="true"', self.page(item))
        return code


def install(command, node, *, password=None):
    # The installer's full diagnostic logs and generated account material stay on the disposable host.
    result = subprocess.run([str(ROOT / "install"), *command], cwd=ROOT,
                            input=(password + "\n").encode() if password else b"",
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        progress_file = node / "state/install-progress.json"
        phases = json.loads(progress_file.read_text())["phases"] if progress_file.exists() else {}
        incomplete = [p for p, value in phases.items() if value == "checking"]
        stage = ", ".join(incomplete) or command[0]
        raise RuntimeError(f"Installer exited {result.returncode} during {stage}")


def run(node, port, exercise_cutover=False):
    if node.exists():
        raise ValueError("A fresh, nonexistent node root is required")
    password = secrets.token_urlsafe(36)
    flags = ["--root", str(node), "--auth", "local", "--enable-account-management",
             "--admin-username", ADMIN, "--admin-display-name", "Synthetic Reviewer",
             "--server-name", "localhost", "--bind-address", "127.0.0.1", "--https-port", str(port),
             "--storage-profile", "evaluation", "--evaluation-reserve-gib", "1", "--non-interactive",
             "--password-stdin", "--plain"]
    preflight = subprocess.run([str(ROOT / "install"), "preflight", *flags, "--json"], cwd=ROOT,
                               capture_output=True, check=False)
    if preflight.returncode:
        checks = json.loads(preflight.stdout)["checks"]
        blocked = [row["name"] for row in checks if row["blocking"] and row["state"] != "pass"]
        raise RuntimeError("Installer preflight failed: " + ", ".join(blocked))
    install(["install", *flags], node, password=password)
    install(["doctor", "--root", str(node), "--plain"], node)
    record = json.loads((node / "installation.json").read_text())
    assert record["models"] == "none" and not record["profiles"] and record["auth"] == "local"
    compose = ["docker", "compose", "--env-file", str(node / "compose.env"),
               "-f", str(Path(record["release_path"]) / "compose.yaml"),
               "-f", str(Path(record["release_path"]) / "compose.local-accounts.yaml")]
    services = set(subprocess.check_output([*compose, "ps", "--status", "running", "--services"],
                                          text=True).splitlines())
    assert {"app", "gateway", "postgres", "clamav", "clamav-updater", "egress-proxy"} <= services
    assert services.isdisjoint({"generator", "retrieval", "transcription-api", "transcription-worker"})
    subprocess.run([*compose, "exec", "-T", "postgres", "sh", "-c",
                    'pg_isready -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"'], check=True)
    print("Fresh local-account node, doctor, PostgreSQL and CPU services passed.", flush=True)

    client = Gateway(node, port)
    client.login(password)
    matter, _ = client.request("/matters", fields={"name": "Synthetic CPU acceptance"})
    match = re.match(r"(/matters/[^/]+)", matter)
    assert match, "Matter creation failed"
    matter = match.group(1)
    assert 'id="model-grants"' not in client.page(matter + "/setup")
    health = json.loads(client.page("/health"))
    capabilities = health["capabilities"]
    assert capabilities["search"] == "word search only" and capabilities["malware_scan"] == "ready"
    assert capabilities["embedding"] == capabilities["reranking"] == "not configured"
    assert health["storage"]["status"] == "ready"
    print("Local HTTPS sign-in, matter creation and antivirus readiness passed.", flush=True)

    data = ("item,description\n1," + PHRASE + "\n").encode()
    client.upload(matter, CSV_NAME, "text/csv", data)
    csv_citation, csv_location = client.exact_match(matter, PHRASE, CSV_NAME)
    code = client.review(matter, csv_citation, data)
    print("Antivirus-backed CSV import, exact search, original bytes and persistent review code passed.", flush=True)

    scan = (ROOT / "src/case_intelligence/config/generated-scan.pdf").read_bytes()
    client.upload(matter, SCAN_NAME, "application/pdf", scan)
    citation, location = client.exact_match(matter, "silver lantern", SCAN_NAME)
    assert location == "Page 2" and SCAN_NAME in client.page(citation)
    original = client.request(urlsplit(citation).path + "/content")[1]
    assert hashlib.sha256(original).digest() == hashlib.sha256(scan).digest()
    print("Local OCR found the image-only phrase on page 2 of the original PDF.", flush=True)
    if exercise_cutover:
        def app_started_at():
            container = subprocess.check_output([*compose, "ps", "-q", "app"], text=True).strip()
            assert container
            return subprocess.check_output(
                ["docker", "inspect", "--format", "{{.State.StartedAt}}", container], text=True,
            ).strip()

        before_start = app_started_at()
        subprocess.run(["python3", str(ROOT / "tests/seed_review_bundle_acceptance.py"), str(node)], cwd=ROOT, check=True)
        install(["update", "--root", str(node), "--retire-bundled-models", "--no-backup", "--plain"], node)
        after = json.loads((node / "installation.json").read_text())
        assert after["bundled_models_retired"] is True and after["models"] == "none" and after["profiles"] == []
        assert after["storage_root"] == record["storage_root"] and after["release_id"] == record["release_id"]
        assert app_started_at() != before_start
        install(["doctor", "--root", str(node), "--plain"], node)
        renewed = Gateway(node, port)
        renewed.login(password)
        def retained_citation(phrase, filename, original_citation, original_location):
            current, location = renewed.exact_match(matter, phrase, filename)
            target, saved = urlsplit(current), urlsplit(original_citation)
            assert location == original_location and target.path == saved.path and target.fragment == saved.fragment
            assert parse_qs(target.query)["unit"] == parse_qs(saved.query)["unit"]
            assert filename in renewed.page(original_citation)

        retained_citation(PHRASE, CSV_NAME, csv_citation, csv_location)
        source = urlsplit(csv_citation).path
        assert renewed.request(source + "/content")[1] == data
        item = matter + "/production/items/" + source.rsplit("/", 1)[1]
        assert re.search(r'data-code-id="' + re.escape(code) + r'"[^>]*aria-pressed="true"', renewed.page(item))
        retained_citation("silver lantern", SCAN_NAME, citation, location)
        assert renewed.request(urlsplit(citation).path + "/content")[1] == scan
        print("Seeded review-bundle cutover restarted the real app. Reauthentication, original sources, "
              "source citations and the saved review code survived.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--port", type=int, default=18443)
    parser.add_argument("--exercise-cutover", action="store_true")
    args = parser.parse_args()
    run(args.root.resolve(), args.port, args.exercise_cutover)
