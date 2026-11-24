# Installer, Backup, and Preflight Live-Server Readiness Fixes

## Problem
Auditing Exculpata's deployment stack ahead of a live internal-server installation surfaced concrete defects hidden by mocked unit tests:
1. `scripts/exculpata_backup.py:_health()` calls bare `urllib.request.urlopen("http://127.0.0.1:8786/health")` inside `app`, where `compose.yaml` sets `HTTP_PROXY=http://egress-proxy:3128` and `NO_PROXY=""`, routing loopback health checks to the egress relay (`403`).
2. `scripts/exculpata_backup.py:backup()` stages the hardlink snapshot in `managed_storage.parent`, which raises `PermissionError` when `--storage-root` lives directly beneath a `0755 root:root` mount parent (the layout recommended in `docs/INSTALL.md`).
3. `scripts/exculpata_backup.py:restore()` runs `pg_restore --list` using unpinned `pgvector/pgvector:pg17` instead of the node's built `${IMAGE_NAMESPACE}/postgres:${RELEASE_ID}` image, causing Docker Hub pulls on air-gapped or restricted hosts.
4. `scripts/exculpata_install.py:_build_targets()` builds both `app` and `account-admin` in parallel even though both build the identical `${IMAGE_NAMESPACE}/app:${RELEASE_ID}` image, causing Docker Compose build collisions without `COMPOSE_PARALLEL_LIMIT=1`.
5. `scripts/exculpata_install.py:_compose_environment()` did not propagate `CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS` from `config/exculpata.env` into `EXCULPATA_GENERATOR_ALLOWED_HOSTS` (so `compose.yaml` silently overrode post-install generator edits back to `generator`), and raised on invalid generator egress config even during maintenance commands (`stop`, `logs`, `ps`, `down`).
6. `./install preflight` skipped validating `--external-generator*` flags, did not enforce the Docker Compose v2 `>= 2.20.2` floor required by `depends_on.<service>.required: false`, and labeled fresh-install preflight exceptions as `"saved-node"`.
7. Interactive fresh installs turned an empty Kerberos keytab prompt into `Path(".")`, omitted LAN TLS prompts when a non-loopback bind address was entered, defaulted OIDC/Kerberos prompts to legacy names on fresh `exculpata` nodes, still exposed retired `--hf-token-stdin`/`--accept-model-terms` in `--help`, and `deploy/systemd/exculpata-backup.service.in` ordered a user unit after system `docker.service`.

## Root cause
Unit tests mocked `_run` and `_health`, exercised writable `tmp_path` parents, and ran Compose builds with `COMPOSE_PARALLEL_LIMIT=1` externally rather than enforcing the invariants directly in the installer and backup scripts.

## Approach
Fix each defect directly in code at the owning boundary:
- Bypass ambient `HTTP_PROXY` in `exculpata_backup.py:_health()` using `urllib.request.build_opener(urllib.request.ProxyHandler({}))`.
- In `exculpata_backup.py:backup()`, fall back to staging the hardlink snapshot in a private hidden directory inside `managed_storage` (copying top-level entries except the staging directory) when `managed_storage.parent` is not writable, with matching cleanup.
- In `exculpata_backup.py:restore()`, resolve `${IMAGE_NAMESPACE}/postgres:${RELEASE_ID}` from `compose.env` when both keys are present, falling back to `POSTGRES_IMAGE` only when unset.
- Remove duplicate `"account-admin"` from `exculpata_install.py:_build_targets()` (matching how `clamav` covers `clamav-updater`).
- Propagate validated `CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS` from the selected app env file inside `_compose_environment()`, and apply the `ps`/`logs`/`stop`/`down` maintenance exemption to generator egress parsing.
- Enforce Docker Compose v2 `>= 2.20.2` and validate `--external-generator*` in `_collect_preflight()`, and report fresh preflight exceptions under `"preflight"` rather than `"saved-node"`.
- Fix the interactive wizard traps (empty Kerberos keytab, non-loopback TLS prompts, `exculpata` defaults), suppress retired flags from `--help`, and remove `docker.service` from the user systemd unit template.

## What it could break
- Existing tests asserting `"account-admin"` in `_build_targets()` (`tests/test_install_naming.py`, `tests/test_oss_installer.py`) must be updated to verify `"app"` builds the shared image once.
- Existing tests checking backup snapshot cleanup or `POSTGRES_IMAGE` must continue passing when `managed_storage.parent` is writable and when `compose.env` omits `IMAGE_NAMESPACE`/`RELEASE_ID`.
- Existing Compose graph equivalence checks (`scripts/compose-check.sh`) and rename contract checks (`scripts/rename_contract_check.py`) must remain green.

## Proof
- Synthetic regression tests in `tests/test_backup_consistency.py` verifying:
  - `_health()` probe script bypasses `HTTP_PROXY` (executed with `HTTP_PROXY=http://127.0.0.1:1` against a real loopback HTTP test server).
  - `backup()` succeeds and cleans up when `managed_storage.parent` is read-only (`0o555`), preserving hardlinks and excluding the staging directory from the snapshot.
  - `restore()` invokes `pg_restore --list` with the node's pinned `${IMAGE_NAMESPACE}/postgres:${RELEASE_ID}` image when recorded in `compose.env`.
- Synthetic regression tests in `tests/test_install_naming.py` and `tests/test_oss_installer.py` verifying:
  - `_build_targets()` never duplicates the shared `app`/`account-admin` image target.
  - `_compose_environment()` propagates post-install `CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS` from `exculpata.env` and exempts `stop`/`logs`/`ps`/`down` from invalid generator config.
  - `_cli_preflight()` rejects invalid `--external-generator*` flags and Docker Compose `< 2.20.2`, and does not mislabel fresh-install errors as `"saved-node"`.
  - Interactive Kerberos empty keytab prompt yields `None` rather than `Path(".")`, and fresh `exculpata` nodes use `exculpata` defaults.
- Full verification via `make compile`, `make compose-check`, `make network-contract`, `rename_contract_check.py`, `publication-check.py`, and pytest across installer/backup/BYOM/naming suites.
