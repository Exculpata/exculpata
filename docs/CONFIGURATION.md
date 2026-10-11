# Configuration

`compose.env` controls host paths, ports, GPU assignment, and the pinned model
portfolio. `config/recordbench.env` controls application behavior.
`config/transcription.env` controls the bundled media queue and worker. The
installer writes all three outside Git with mode `0600`.

Secrets are separate owner-only files: PostgreSQL password/DSN, local account
hashes, transcription API token, OIDC client secret, or Kerberos keytab/proxy
secret. A real secret must never appear in an environment example.

Key operator decisions include:

- `EXCULPATA_STORAGE_ROOT`: dedicated matter byte boundary;
- `EXCULPATA_MODEL_ROOT`: offline model cache;
- `EXCULPATA_*_GPU`: detected or explicitly selected physical GPU assignment;
- `EXCULPATA_GPU_LAYOUT`, retrieval device, and generator tensor-parallel
  width: portable shared, separated-role, or operator-overridden topology;
- generator dtype, memory share, batch size, and application concurrency are
  derived conservatively from compute capability and whether transcription is
  co-resident;
- `CASE_INTELLIGENCE_*_QUOTA_*`: per-matter, upload, file, and reserve limits;
- OCR language/page cap and ingestion/answer concurrency;
- generator backend/endpoint/model and exact allowed companion-service hosts;
- transcription retention, capacity, diarization, and free-disk reserve.

Do not edit generated files while services run. Back up, validate changes with
`docker compose config`, restart only affected services, and rerun the doctor.

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
