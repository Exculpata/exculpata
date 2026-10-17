#!/usr/bin/env python3
"""Opt-in disposable Compose smoke. Requires an explicitly selected Docker context.

No host mounts/ports, real credentials, host policy changes or existing projects.
The caller owns runtime creation/removal. Standard image/dependency acquisition
happens at build time, before the two internal-only test networks are created.
"""
import argparse
from datetime import datetime, timedelta, timezone
import ipaddress
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import yaml

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-context", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    project = "issue319-smoke-" + uuid.uuid4().hex[:10]
    docker = ["docker", "--context", args.docker_context]
    results = {"project": project, "passed": False}
    with tempfile.TemporaryDirectory(prefix="egress-smoke-") as temporary:
        root = Path(temporary)
        fixture = root / "fixture"
        shutil.copytree(ROOT / "tests/network_smoke", fixture)
        shutil.copytree(ROOT / "src", fixture / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
        shutil.copy(ROOT / "requirements/application.lock", fixture / "requirements.lock")
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "issuer.example.test")])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("issuer.example.test")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
        (fixture / "issuer.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        private = fixture / "issuer-key.pem"
        private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        private.chmod(0o600)
        # Synthetic RFC1918 subnet exists only inside the selected disposable VM.
        network = ipaddress.ip_network((int.from_bytes(bytes((172, 30, 219, 0))), 24))
        issuer_address, foreign_address = str(network[10]), str(network[11])
        issuer = "https://issuer.example.test:8443/realm"
        limits = {"cpus": 0.5, "mem_limit": "128m", "pids_limit": 64,
                  "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"]}
        endpoint = {**limits, "build": {"context": str(fixture), "target": "endpoint"},
                    "image": project + "-endpoint", "tmpfs": ["/tmp:rw,noexec,nosuid,size=8m"]}
        data = {"services": {
            "issuer": {**endpoint, "networks": {"upstream": {"ipv4_address": issuer_address, "aliases": ["issuer.example.test"]}}},
            "foreign": {**endpoint, "networks": {"upstream": {"ipv4_address": foreign_address, "aliases": ["foreign.example.test"]}}},
            "proxy": {**limits, "build": {"context": str(ROOT / "deploy/egress-proxy")}, "image": project + "-proxy",
                "networks": ["app", "upstream"], "environment": {"OIDC_ISSUER": issuer,
                    "OIDC_ISSUER_PINS": json.dumps({"issuer": issuer, "addresses": [issuer_address]})}},
            "app": {**limits, "build": {"context": str(fixture), "target": "probe"}, "image": project + "-app",
                "networks": ["app"], "command": ["python", "-c", "import time; time.sleep(1200)"],
                "environment": {"HTTPS_PROXY": "http://proxy:3128", "https_proxy": "http://proxy:3128",
                    "HTTP_PROXY": "", "http_proxy": "", "ALL_PROXY": "", "all_proxy": "", "NO_PROXY": "", "no_proxy": "",
                    "ISSUER_ADDRESS": issuer_address, "FOREIGN_ADDRESS": foreign_address}},
            }, "networks": {"app": {"internal": True}, "upstream": {"internal": True,
                "ipam": {"config": [{"subnet": str(network)}]}}}}
        compose_file = root / "compose.yaml"
        compose_file.write_text(yaml.safe_dump(data))
        compose = [*docker, "compose", "-p", project, "-f", str(compose_file)]
        def run(argv, *, timeout=600, filename=None):
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            if filename:
                (args.output / filename).write_text(completed.stdout + completed.stderr)
            if completed.returncode:
                raise RuntimeError(f"smoke command failed; see {filename or 'runtime diagnostics'}")
            return completed.stdout
        try:
            graph = json.loads(run([*compose, "config", "--format", "json"]))
            for service in graph["services"].values():
                assert not any(service.get(field) for field in ("ports", "volumes", "secrets", "devices"))
            assert all(n["internal"] for n in graph["networks"].values())
            run([*compose, "build"], timeout=900, filename="build.log")
            run([*compose, "up", "-d", "--wait", "--wait-timeout", "60"], filename="up.log")
            ids = run([*compose, "ps", "-q"]).split()
            containers = json.loads(run([*docker, "inspect", *ids]))
            assert len(containers) == 4
            for container in containers:
                assert not container["Mounts"] and not container["HostConfig"]["PortBindings"]
                assert not container["HostConfig"]["Privileged"]
                assert container["HostConfig"]["Memory"] == 128 * 1024 * 1024
            # Require TLS readiness from the allowed endpoint before denial checks.
            run([*compose, "exec", "-T", "app", "python", "-c",
                 "import httpx,ssl,time; c=ssl.create_default_context(cafile='/fixture/issuer.pem'); "
                 "exec(\"for attempt in range(20):\\n try:\\n  httpx.get('https://issuer.example.test:8443/',verify=c,timeout=3).raise_for_status(); break\\n except httpx.HTTPError:\\n  time.sleep(.2)\\nelse: raise RuntimeError('issuer unavailable')\")"], filename="readiness.log")
            results.update(json.loads(run([*compose, "exec", "-T", "app", "python", "/fixture/probe.py"], filename="probe.log")))
            # Prove local/Kerberos's healthy deny-all configuration, then clean up.
            data["services"]["proxy"]["environment"] = {"OIDC_ISSUER": "", "OIDC_ISSUER_PINS": ""}
            compose_file.write_text(yaml.safe_dump(data))
            run([*compose, "up", "-d", "--wait", "--wait-timeout", "60", "proxy"], filename="deny-all-up.log")
            results.update(json.loads(run([*compose, "exec", "-T", "app", "python", "/fixture/probe.py", "deny-all"], filename="deny-all.log")))
            results.update({"no_mounts_or_published_ports": True, "internal_test_networks": True, "passed": True})
        finally:
            cleanup = subprocess.run([*compose, "down", "--volumes", "--rmi", "all", "--remove-orphans"],
                                     capture_output=True, text=True, timeout=120)
            (args.output / "cleanup.log").write_text(cleanup.stdout + cleanup.stderr)
            results["compose_cleanup_passed"] = cleanup.returncode == 0
            if cleanup.returncode:
                results["passed"] = False
            (args.output / "summary.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results))
    return 0 if results["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
