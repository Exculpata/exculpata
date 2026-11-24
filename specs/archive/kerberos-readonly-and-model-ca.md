# Kerberos Read-Only Container Startup, Internal-CA Model Transport, and Health Diagnostics

## Problem
Three deployment blockers affect internal-server installations using Kerberos and private/internal-CA HTTPS model endpoints (such as vLLM or OpenAI-compatible APIs behind an internal CA):
1. `compose.kerberos.yaml` sets `read_only: true` on `kerberos-proxy` with writable tmpfs only on `/run/recordbench-auth` and `/var/{lock,log,run}/apache2`, yet `deploy/kerberos-proxy/entrypoint` writes rendered configs to `/etc/apache2/sites-available/recordbench-auth.conf` and `/etc/apache2/conf-available/recordbench-server-name.conf` and executes `a2ensite`/`a2enconf` at container startup, crashing immediately with `Read-only file system`.
2. `src/case_intelligence/generator_transport.py:open_generator_request()` (shared by all seven operator model APIs) requires HTTPS through the CONNECT relay with end-to-end TLS verification, but has no custom CA bundle setting (unlike OIDC's `CASE_INTELLIGENCE_OIDC_CA_FILE`). Internal HTTPS model endpoints signed by an internal CA fail TLS verification inside the read-only `app` container.
3. `src/case_intelligence/workbench.py` silently swallows PostgreSQL connection and migration exceptions without logging or surfacing Postgres status in `/health`, and labels external ASR APIs as `"local WhisperX v2"` in `/health`.

## Root cause
1. `tests/test_kerberos_authentication.py` inspected config text statically without running the `kerberos-proxy` image under `read_only: true`.
2. `generator_transport.py` instantiated `urllib.request.HTTPSHandler()` without loading an operator-configured CA bundle.
3. `_connect_postgres()` and `apply_postgres_migrations()` suppressed exceptions without emitting sanitized diagnostics or capability state, and `capabilities()["transcription"]` did not distinguish external ASR from the legacy bundled WhisperX worker.

## Approach
1. **Kerberos proxy under `read_only: true`:** Bake static `Include /run/recordbench-auth/...` wrappers into `/etc/apache2/sites-available/recordbench-auth.conf` and `/etc/apache2/conf-available/recordbench-server-name.conf` and run `a2ensite`/`a2enconf` at Docker build time (`deploy/kerberos-proxy/Dockerfile`). In `deploy/kerberos-proxy/entrypoint`, write the rendered site and `ServerName` configs solely inside `/run/recordbench-auth/`.
2. **Custom CA support for all 7 model APIs:** In `src/case_intelligence/generator_transport.py`, support per-role `{environment_prefix}_CA_FILE` (e.g., `CASE_INTELLIGENCE_GENERATOR_CA_FILE`) falling back to shared `CASE_INTELLIGENCE_MODEL_CA_FILE`. Validate the path strictly (absolute, no control characters, no symlinks in path or parents, regular readable file `<= 1 MiB`, not group/world-writable), fail closed with `DestinationRejected`, and pass `ssl.create_default_context(cafile=...)` to `HTTPS(context=...)`.
3. **Health diagnostics:** Log sanitized PostgreSQL connection/migration failure class names (never DSNs or credentials), expose `"postgres": "ready" | "unavailable" | "not configured"` in `capabilities()`, and distinguish `"external speech API"` from `"local WhisperX v2"` in `capabilities()["transcription"]`.

## What it could break
- Existing static assertions in `tests/test_kerberos_authentication.py` and `scripts/rename_contract_check.py`.
- `scripts/network-contract.py` if any new imports or client references enter `generator_transport.py`.
- Existing `/health` callers expecting `"local WhisperX v2"` on bundled transcription nodes (preserved unchanged when `CASE_INTELLIGENCE_ASR_URL` is not set).

## Proof
- Real Docker artifact test: build `deploy/kerberos-proxy`, mount a synthetic Kerberos keytab and proxy secret, run with `--read-only`, `--cap-drop=ALL`, `--cap-add=CHOWN,DAC_OVERRIDE,KILL,SETGID,SETUID`, and exact tmpfs mounts from `compose.kerberos.yaml`, and verify Apache boots cleanly and returns HTTP `401` on `http://127.0.0.1:8080/`.
- Synthetic end-to-end TLS + CONNECT tunnel regression tests in `tests/test_model_ca_transport.py` proving:
  - Self-signed internal HTTPS model endpoint fails verification without CA config,
  - Succeeds with `CASE_INTELLIGENCE_MODEL_CA_FILE` and per-role `{prefix}_CA_FILE`,
  - Fails closed (`DestinationRejected`) on missing, relative, symlinked, or world-writable CA files.
- Synthetic regression tests verifying Kerberos entrypoint never writes to `/etc/apache2`, Postgres failure surfaces in `capabilities()["postgres"]`, and external ASR reports `"external speech API"` in `capabilities()["transcription"]`.
