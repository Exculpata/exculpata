#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scratch="$(mktemp -d)"
cleanup() {
  rm -rf -- "$scratch"
}
trap cleanup EXIT

install -d -m 0700 \
  "$scratch/config" \
  "$scratch/secrets" \
  "$scratch/accounts" \
  "$scratch/runtime" \
  "$scratch/matter-storage" \
  "$scratch/transcription" \
  "$scratch/state" \
  "$scratch/models" \
  "$scratch/tls"
install -m 0600 "$project_root/config/recordbench.env.example" "$scratch/config/recordbench.env"
install -m 0600 "$project_root/config/transcription.env.example" "$scratch/config/transcription.env"
for name in postgres-password postgres-dsn transcription-token kerberos-proxy-secret recordbench.keytab; do
  install -m 0600 /dev/null "$scratch/secrets/$name"
done
install -m 0600 /dev/null "$scratch/tls/tls.crt"
install -m 0600 /dev/null "$scratch/tls/tls.key"

# Resolve every graph in isolated environments: no inherited setting can hide a
# missing alias, introduce a new requirement, or mask precedence differences.
python3 - "$project_root" "$scratch" <<'PYTHON'
import json
import os
from pathlib import Path
import subprocess
import sys

root, scratch = map(Path, sys.argv[1:])
values = {}
for line in (root / ".env.example").read_text().splitlines():
    if line.startswith("EXCULPATA_"):
        key, value = line.split("=", 1)
        values[key.removeprefix("EXCULPATA_")] = value
for name, directory in {
    "CONFIG": "config", "SECRETS": "secrets", "RUNTIME": "runtime",
    "STORAGE": "matter-storage", "TRANSCRIPTION": "transcription", "STATE": "state",
    "MODEL": "models", "LOCAL_ACCOUNT": "accounts",
}.items():
    values[f"{name}_ROOT"] = str(scratch / directory)
values.update(TLS_CERT=str(scratch / "tls/tls.crt"), TLS_KEY=str(scratch / "tls/tls.key"),
              KERBEROS_PRINCIPAL="HTTP/recordbench.example.test@EXAMPLE.TEST")
clean = {key: value for key, value in os.environ.items()
         if not key.startswith(("RECORDBENCH_", "EXCULPATA_", "COMPOSE_"))}
reference = {}
for mode in ("old-only", "new-only", "both-equal", "both-different"):
    settings = {}
    for key, value in values.items():
        if mode != "new-only":
            settings[f"RECORDBENCH_{key}"] = "ignored" if mode == "both-different" else value
        if mode != "old-only":
            settings[f"EXCULPATA_{key}"] = value
    env_file = scratch / f"{mode}.env"
    env_file.write_text("".join(f"{key}={json.dumps(value)}\n" for key, value in settings.items()))
    for profile, overlay in (("standard", None), ("kerberos", "compose.kerberos.yaml"),
                             ("local-accounts", "compose.local-accounts.yaml")):
        command = ["docker", "compose", "--profile", "tools", "--env-file", str(env_file),
                   "-f", str(root / "compose.yaml")]
        if overlay:
            command += ["-f", str(root / overlay)]
        subprocess.run(command + "config --quiet".split(), env=clean, check=True)
        result = subprocess.run(command + ["config", "--format", "json"], env=clean,
                                check=True, stdout=subprocess.PIPE, text=True)
        graph = json.loads(result.stdout)
        if mode == "old-only":
            reference[profile] = graph
        else:
            assert graph == reference[profile], (mode, profile, "alias graph changed")
        if profile == "local-accounts":
            for name in ("app", "account-admin"):
                mounts = {mount["target"]: mount for mount in graph["services"][name]["volumes"]}
                assert mounts["/run/recordbench-secrets"].get("read_only") is True, name
                assert not mounts["/var/lib/recordbench-accounts"].get("read_only", False), name
    print(f"{mode}: standard, Kerberos and local-account Compose graphs valid and equivalent.")
PYTHON
