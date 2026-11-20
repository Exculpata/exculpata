#!/usr/bin/env python3
"""Exercise the installed speech diagnostics and production relay on isolated Docker networks."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import uuid

import yaml


ROOT = Path(__file__).resolve().parents[1]
CAPABILITIES = {"ASR": "transcription", "ALIGNMENT": "alignment",
                "TRANSLATION": "translation", "DIARIZATION": "speaker_labeling"}


def diagnostics(compose):
    outcomes = {}
    for capability in CAPABILITIES.values():
        if capability == "transcription":
            command = ["case_intelligence.speech_endpoint"]
        else:
            command = ["case_intelligence.media_endpoint_doctor", capability]
        result = compose("exec", "-T", "app", "python", "-m", *command)
        outcomes[capability] = json.loads(result)[capability]
    return outcomes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-context", required=True, help="disposable local Docker context")
    parser.add_argument("--app-image", help="use an application image already built from this checkout")
    options = parser.parse_args()
    docker = ["docker", "--context", options.docker_context]
    project = "speech-contract-" + uuid.uuid4().hex[:10]
    app_image = options.app_image or project + "-app"
    compose_path = ROOT / "compose.yaml"
    source = yaml.safe_load(compose_path.read_text())["services"]
    app = {key: source["app"][key] for key in ("user", "cap_drop", "read_only", "security_opt", "dns", "dns_search")}
    app.update(image=app_image, command=["sleep", "1800"], pull_policy="never",
               tmpfs=["/tmp"], networks=["app-egress"], healthcheck={"disable": True},
               environment={"SSL_CERT_FILE": "/tmp/speech-contract-ca.pem"})
    if not options.app_image:
        app["build"] = {**source["app"]["build"], "context": str(ROOT)}
    for role in CAPABILITIES:
        key = f"CASE_INTELLIGENCE_{role}_PROXY"
        app["environment"][key] = source["app"]["environment"][key]

    relay = {**source["egress-proxy"], "image": project + "-relay", "pull_policy": "never", "restart": "no",
             "build": {**source["egress-proxy"]["build"], "context": str(ROOT)}, "environment": {}}
    hosts = [role.lower() + ".example.test" for role in CAPABILITIES]
    fixture = {"image": app_image, "pull_policy": "never", "user": "65532:65532",
               "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
               "tmpfs": ["/tmp"], "healthcheck": {"disable": True}, "networks": {"front": {"aliases": hosts}}, "command": ["sleep", "1800"]}
    graph = {"services": {"app": app, "egress-proxy": relay, "fixture": fixture},
             "networks": {"app-egress": {"internal": True}, "front": {"internal": True}}}

    with tempfile.TemporaryDirectory(prefix=project) as temporary:
        root = Path(temporary)
        path = root / "compose.yaml"

        def compose(*args, timeout=300, stdin=None):
            return subprocess.run([*docker, "compose", "-f", str(path), "-p", project, *args], check=True,
                                  timeout=timeout, text=True, input=stdin, stdout=subprocess.PIPE).stdout.strip()

        path.write_text(yaml.safe_dump(graph))
        try:
            compose("build", "egress-proxy", *([] if options.app_image else ["app"]), timeout=1800)
            compose("up", "--no-build", "-d", "fixture")
            compose("exec", "-T", "fixture", "sh", "-ec", "cat > /tmp/speech-provider.py",
                    stdin=(ROOT / "tests/speech_contract_provider.py").read_text(), timeout=10)
            compose("exec", "-d", "fixture", "python", "/tmp/speech-provider.py")
            compose("exec", "-T", "fixture", "sh", "-ec",
                    "for n in $(seq 1 30); do test -s /tmp/speech-contract-ca.pem && exit 0; sleep 1; done; exit 1")
            certificate = compose("exec", "-T", "fixture", "cat", "/tmp/speech-contract-ca.pem")
            container = compose("ps", "-q", "fixture")
            info = json.loads(subprocess.check_output([*docker, "inspect", container]))[0]
            address = info["NetworkSettings"]["Networks"][project + "_front"]["IPAddress"]
            for role, host in zip(CAPABILITIES, hosts, strict=True):
                origin = f"https://{host}:8443"
                pins = json.dumps({"issuer": origin, "addresses": [address]})
                for suffix, value in (("URL", origin), ("EGRESS_PINS", pins), ("MODEL", "synthetic-" + role.lower()),
                                      ("ALLOWED_HOSTS", host), ("TIMEOUT", "5")):
                    app["environment"][f"CASE_INTELLIGENCE_{role}_{suffix}"] = value
                for suffix, value in (("URL", origin), ("PINS", pins)):
                    key = f"{role}_EGRESS_{suffix}"
                    if key not in source["egress-proxy"]["environment"]:
                        raise RuntimeError("Compose does not forward " + key)
                    relay["environment"][key] = value
            path.write_text(yaml.safe_dump(graph))
            compose("up", "--no-build", "-d", "--wait", "--wait-timeout", "60", "egress-proxy")
            compose("up", "--no-build", "-d", "app")
            compose("exec", "-T", "app", "sh", "-ec", "cat > /tmp/speech-contract-ca.pem", stdin=certificate + "\n")
            online = diagnostics(compose)
            print("Relay online", json.dumps(online, sort_keys=True), flush=True)
            if any(result != "ok" for result in online.values()):
                raise SystemExit("One or more speech contracts failed")
            compose("stop", "egress-proxy")
            offline = diagnostics(compose)
            print("Relay stopped", json.dumps(offline, sort_keys=True), flush=True)
            if any("unreachable" not in result.lower() and "unavailable" not in result.lower() for result in offline.values()):
                raise SystemExit("A capability did not report the relay outage")
        finally:
            compose("down", "--volumes", "--remove-orphans")
            images = [project + "-relay"]
            if not options.app_image:
                images.append(app_image)
            subprocess.run([*docker, "image", "rm", *images], stdout=subprocess.DEVNULL, check=False, timeout=60)


if __name__ == "__main__":
    main()
