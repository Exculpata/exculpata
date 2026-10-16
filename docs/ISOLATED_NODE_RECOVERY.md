# Boot a verified local-account restore on a separate node

This procedure extends the file-verification drill in
[storage and backup](STORAGE_AND_BACKUP.md). Its scope is the local-account GPU
reference profile with browser account management. Other identity integrations
and operator-specific model configurations require their own recovery plan.
An encrypted upload or a verified dump listing alone does not prove recovery.

Use an isolated replacement host or a deliberately bounded disposable test
node. Keep the original stopped if the two nodes share GPU capacity; never
point the replacement at original data directories or Docker volumes. Keep
maintenance disabled until restored deadlines and scheduled work are reviewed.
Do not publish restored files, account data, environment files or logs.

## Prepare the two new targets

1. Restore and verify into a previously absent directory using the documented
   `./install restore --root ... --restore-target ...` command. Retain
   `RESTORE_DRILL_VERIFIED.json`. Run a full encrypted-repository data check when
   required. This directory contains confidential decrypted snapshot data.
2. Obtain the exact reviewed release recorded by the snapshot. Provision a
   **second new empty node root** with `--prepare-only`, the same local-account
   management/model choices and service UID, and a distinct loopback port and
   Compose project. Use the installation playbook's account/TLS preparation.
   Do not start the application. Build/acquire the recorded images/models or
   verify your preserved matching artifacts; these are excluded from backup.
3. Verify that the generated node's Compose volumes, bind sources and project
   differ from the original. A different port alone is insufficient. If sharing
   a machine, inspect the actual merged Compose configuration and set resource
   and exact GPU-device limits before starting anything.

For the reference profile, the prepare command has this form (set real new paths
and available resources deliberately):

```sh
./install --root /srv/exculpata-recovered --prepare-only \
  --models all --auth local --enable-account-management \
  --gpu-layout shared --review-model-profile portable --retrieval-device cuda \
  --transcription-languages en,es --enable-diarization --diarization-backend nemotron \
  --server-name localhost --bind-address 127.0.0.1 --https-port 9443
```

This creates only replacement-node configuration and prepared artifacts. Its
new bootstrap account will be replaced with the restored canonical accounts.
You must know a restored administrator password or use the documented account
recovery procedure. Do not overwrite any node that has admitted user work.

## Map verified snapshot data

The verified restic restore retains original path prefixes. Under the drill
root, identify the unique `payload/CONTROL_SHA256SUMS` and the unique
`managed-storage/.exculpata-managed-storage.json`; fail if either is missing
or ambiguous. The payload contains `runtime/`, `accounts/`, `secrets/`,
`configuration/`, `control/`, and `postgres/review-index.dump`.

While **all replacement application services are stopped**, copy these verified
stores into the newly prepared node, preserving ownership and private modes:

| Verified snapshot entry | Fresh replacement destination |
| --- | --- |
| `payload/runtime/` | node `runtime/` |
| `payload/accounts/` | node `accounts/` |
| `payload/secrets/` | node `secrets/` |
| `managed-storage/` including its ownership marker | node's newly initialized dedicated matter-storage directory |

Keep the newly generated `compose.env`, installation metadata, release capsule
and TLS files. Those bind the replacement's paths, project, port and images.
Do **not** copy the old `compose.env` or start a stack using the restored control
metadata: it can refer to the original node. Retain the snapshot itself untouched.
Compare the restored application options with the replacement's configuration
and deliberately reconcile any non-default settings. The reference procedure
uses generated local-account/service endpoints, with restored account/session
and service secrets; inspect any custom endpoint before enabling it.

Set `CASE_INTELLIGENCE_MAINTENANCE_ENABLED=0` and
`CASE_INTELLIGENCE_AUTOMATIC_DISCOVERY=0` in the replacement's effective
`config/exculpata.env` before the first app process starts. Follow
[expired-matter recovery](EXPIRED_MATTER_RECOVERY.md); a five-minute maintenance
interval is not a safety hold. Do not edit stored deadlines or SQLite data.

## Import PostgreSQL and start the replacement

Use the replacement's generated Compose environment and installed release
files, including its local-account overlay and profiles. For example, after
setting `RECOVERED_ROOT` and `RECOVERED_RELEASE` to the two verified paths:

```sh
docker compose --env-file "$RECOVERED_ROOT/compose.env" \
  -f "$RECOVERED_RELEASE/compose.yaml" \
  -f "$RECOVERED_RELEASE/compose.local-accounts.yaml" \
  --profile ai --profile transcription up -d --no-build postgres
```

Verify that this PostgreSQL service uses a **new empty volume**, the restored
PostgreSQL secret, no published port, and the replacement project. Wait for
`pg_isready`. Import into that empty database, without dropping existing objects:

```sh
docker compose --env-file "$RECOVERED_ROOT/compose.env" \
  -f "$RECOVERED_RELEASE/compose.yaml" \
  -f "$RECOVERED_RELEASE/compose.local-accounts.yaml" \
  exec -T postgres pg_restore --exit-on-error --no-owner \
  -U exculpata -d exculpata < "$VERIFIED_PAYLOAD/postgres/review-index.dump"
```

Stop on any import error. Compare schema and representative row counts with the
backup acceptance record. Then use the normal installer resume operation on the
**replacement root**, with maintenance still disabled and resource admission
satisfied. Never run both GPU stacks concurrently unless separately provisioned.

```sh
./install --root "$RECOVERED_ROOT" --resume
./install doctor --root "$RECOVERED_ROOT"
```

## Prove usability before activation

Verify both restored local accounts, retained matter grants/revocations,
original source playback, saved citations/answers, and transcript exports.
Generated export timestamps can differ; saved content, source references and
processing notices must agree. The transient transcription queue is intentionally
not restored; verify a **new synthetic job** through the replacement worker to
prove that model access and service credentials work.

Record the exact release/image/model identities and each result locally. Decide
restored matters' retention disposition while maintenance remains held. Only
then prepare a separate activation plan covering trusted TLS, routing, backup
schedule, maintenance and rollback. A disposable drill should be stopped after
acceptance, retaining evidence and keys securely. Never switch production traffic
or restore over the original node merely to test this procedure.

## Re-establish backup deliberately

The restored secrets include the original backup credential. Preserve it securely
for access to the original encrypted repository; do not overwrite it. The fresh
node has no backup schedule by default. To create an independent repository,
first retain that credential outside the replacement node in an owner-only
recovery location, then use the normal initializer with a new empty repository
and a new recovery-key destination. The initializer refuses existing credential
files. Verify a new snapshot and restore before scheduling replacement backups.
Do not blindly copy the original backup configuration or retention schedule.
