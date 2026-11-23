# Configuration

`compose.env` controls host paths, ports, release identifiers, and (on legacy
bundled nodes) GPU assignment and bundled model revisions. `config/exculpata.env`
controls application behavior and operator-managed model APIs on a fresh node.
A legacy node keeps `config/recordbench.env`; legacy bundled transcription nodes
also keep `config/transcription.env`. The installer writes these files outside
Git with mode `0600`.

Secrets are separate owner-only files under the node secrets directory
(`/run/recordbench-secrets` inside containers): PostgreSQL password/DSN, local
account hashes, model API keys, optional internal CA bundles, OIDC client
secret, Kerberos keytab/proxy secret, or legacy transcription API token. A real
secret must never appear in an environment example.

Key operator decisions include:

- `EXCULPATA_STORAGE_ROOT`: dedicated matter byte boundary;
- `EXCULPATA_BIND_ADDRESS`, `EXCULPATA_HTTPS_PORT`, `EXCULPATA_SERVER_NAME`,
  `EXCULPATA_TLS_CERT`, and `EXCULPATA_TLS_KEY`: HTTPS gateway identity and TLS;
- **Operator-managed HTTPS model APIs** in `config/exculpata.env` (see
  [model configuration](MODEL_CONFIGURATION.md)): up to seven independent role
  prefixes (`CASE_INTELLIGENCE_GENERATOR_*`, `EMBEDDING_*`, `RERANK_*`, `ASR_*`,
  `ALIGNMENT_*`, `TRANSLATION_*`, `DIARIZATION_*`) with `*_URL`, `*_MODEL`,
  `*_ALLOWED_HOSTS`, `*_EGRESS_PINS` (required for private RFC1918/ULA hosts) or
  `*_REMOTE_CONFIRMED=1` (for remote hosts), optional `*_API_KEY_FILE`, and
  optional internal CA bundles (`CASE_INTELLIGENCE_MODEL_CA_FILE` or per-role
  `CASE_INTELLIGENCE_<ROLE>_CA_FILE`);
- `CASE_INTELLIGENCE_*_QUOTA_*` and `CASE_INTELLIGENCE_STORAGE_RESERVE_GIB`:
  per-matter, upload, file, and storage-reserve limits;
- OCR language/page cap and ingestion/answer concurrency;
- **Legacy bundled-model nodes (`v0.1.0-beta.1` / `--resume` only):**
  `EXCULPATA_MODEL_ROOT`, `EXCULPATA_*_GPU`, `EXCULPATA_GPU_LAYOUT`, and
  `config/transcription.env` (retired for fresh installs; migrate in place via
  `./install update --root <path> --retire-bundled-models`).

Do not edit generated files while services run. Back up, validate changes with
`docker compose config`, apply updates through `./install` (so `egress-proxy`
receives updated relay destinations/pins), and rerun `./install doctor`.

## Compatible environment names

Use `EXCULPATA_*` for settings previously named `RECORDBENCH_*`. Every old
name remains accepted as a deprecated alias; no new setting is required for
an existing installation. `CASE_INTELLIGENCE_*`, `CASE_REVIEW_*` and
`TRANSCRIPTION_V2_*` names and Python import packages are unchanged.

Application and node-tool reads prefer an explicitly set `EXCULPATA_*` value,
including an empty value, then fall back to `RECORDBENCH_*` and the existing
default. A legacy name logs one deprecation warning per process. If both names
are set differently, the new name wins and a separate warning is logged once
per setting per process. Warnings contain setting names, never values.

Compose uses `${EXCULPATA_X:-${RECORDBENCH_X:-default}}`: as usual for Compose's
`:-` operator, an empty value falls back as well as an unset value. Required
storage and secret mounts still need valid paths; the Kerberos entrypoint
validates its required service identity before starting. `make compose-check`
checks standard, Kerberos and local-account graphs with old-only, new-only,
equal and conflicting settings and requires equivalent resolved graphs.

New nodes write both names with equal values. Node updates keep both release
names synchronized. Installer diagnostics, resume/update and backup readers
accept either spelling. When editing a generated node configuration, prefer the
new name; changing only the deprecated copy while a new value exists has no
effect. Keep both copies equal when retaining compatibility with older tools.
