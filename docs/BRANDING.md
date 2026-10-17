# Exculpata brand assets and project copy

Use these assets and descriptions for the repository, project pages, and
presentations. They describe community beta v0.1.0-beta.1; they do not imply an
institutional endorsement or production acceptance.

Visual colors, the wordmark, and the icon are specified in [BRAND.md](BRAND.md).

## Identity

- **Name:** Exculpata
- **Tagline:** your discovery, cited.
- **Positioning:** Local-first discovery and case review for defense teams. Local-first by default.
- **Proof Cyan:** `#22D3EE`
- **Near-black:** `#0B0D10`
- **Off-white:** `#EDEAE2`

The icon is a Proof Cyan `>` chevron and solid block cursor on a near-black
rounded square. Keep its geometry, colors, and proportions intact. Give it
clear space and use a solid background with enough contrast. The application
sign-in screen still shows "Review the record. Build the work."

The workbench appearance is the theme in [Exculpata theme](EXCULPATA_THEME.md).
Dark/Cyan is the default. Dark and Light each support independently saved Cyan, Green, and Amber accents. See [appearance tokens and contrast](EXCULPATA_THEME.md).
Geist and JetBrains Mono are self-hosted; the app does not load them from a font service.

## Assets

| Asset | Use |
| --- | --- |
| [Icon](assets/brand/exculpata-icon.svg) | Canonical mark. The application favicon uses the same drawing. |
| [Wordmark](assets/brand/exculpata-wordmark.svg) | `> exculpata` with the block cursor, for dark backgrounds. |
| [Light-text wordmark](assets/brand/wordmark-cyan-transparent-lighttext.png) | README hero on a dark background. |
| [Dark-text wordmark](assets/brand/wordmark-cyan-transparent-darktext.png) | README hero on a light background. |
| [Social preview](assets/brand/social-preview-1280x640.png) | 1280 × 640 image for the GitHub social preview setting. |
| [Launch video, 16:9](assets/exculpata-launch-16x9.mp4) | 1:00 landscape video with soundtrack, at 1920 × 1080. |
| [Launch video, 9:16](assets/exculpata-launch-9x16.mp4) | 1:00 vertical video with soundtrack, at 1080 × 1920. |

The SVGs are self-contained. They contain no external image or font requests,
case screenshots, or deployment details. The application favicon repeats the
canonical icon; keep them synchronized if the mark changes.
Use descriptive alternative text when embedding an asset.

## Launch video

Present the one-minute launch video under **See Exculpata in action**, desktop
(16:9) first, then vertical (9:16). It follows a fictional Calder Reservoir
administrative inquiry built from synthetic records. Keep the fictional-case
notice visible. Do not present the video as production validation or
institutional endorsement.

On GitHub, link each committed poster to https://exculpata.com and put an
absolute raw download link under the poster:

`https://github.com/Exculpata/exculpata/raw/main/docs/assets/exculpata-launch-16x9.mp4`

and the 9:16 file beside it. Poster images are the JPEGs in `docs/assets/`
copied from `site/assets/`. Do not leave an unfilled user-attachment
placeholder, and do not use a relative `?raw=true` repository link as the only
player. Preserve each video's aspect ratio and timing.

See [media rights](assets/MEDIA_RIGHTS.md) for the launch videos and the
separate rights notice retained for the older teaser.

## Short description

Local-first discovery and case review for defense teams. Search records,
review media, organize findings, and build work product. Community beta v0.1.0-beta.1.

## Project introduction

Discovery arrives in folders. Understanding a case takes more.

Exculpata is a self-hosted workspace being built for the work of
criminal defense. It brings documents, images, email, spreadsheets, audio,
and video into one place to search, review, ask questions, and develop work
product with the source material close at hand. Its bundled AI workflows use
local models on infrastructure the operator controls.

The aim is practical: help attorneys, investigators, and support staff turn
unorganized discovery into a clearer understanding of the case.

## Maintainer introduction

I'm building Exculpata to help defense teams get more out of discovery.
The goal is to bring scattered records, source-linked review, and useful work
product into one workspace that an office can run itself. It's early, and I'm
looking for people who can help test it with synthetic material, improve the
workflows, and make it easier for another office to get started.

## Keep claims specific

Describe the current beta's implemented workflows separately from its
long-term direction. The [README](../README.md) carries both. Durable matters,
complete reopenable case packages, Relativity-style load file interchange,
and matter-controlled frontier AI are development goals.

Describe answers, transcripts, and source screening as review aids. Avoid
claims of exhaustive review, guaranteed accuracy, validated confidentiality,
turnkey installation, or complete exports. Hardware figures are evaluation
targets until the required acceptance evidence exists.

Use only synthetic demonstrations. Follow the
[publication checklist](PUBLICATION_CHECKLIST.md) before distributing assets
or changing repository visibility.

## Compatibility and historical assets

RecordBench is now Exculpata. Fresh installs use Exculpata for the service
account, host paths, configuration filename, image namespace, and visible
product strings. Existing nodes keep their account, root, configuration,
database name, container mounts, images, marker, and backup units. There is
no naming migration. `--names exculpata` refuses a legacy node.
Saved browser preferences are different: a value stored under the previous
assistant, review-layout, or readiness key is copied to the Exculpata key on
read and the previous key is removed. In-page readiness and answer-complete
events use the Exculpata name and still accept the previous event name.

Settings and request headers that still use the `RECORDBENCH_` and
`X-RecordBench-` prefixes keep working. The process logs a one-time warning
when a legacy environment variable or request header is actually used, and it
does not log the value. Response headers include both names so existing clients
keep working. Fresh Compose files set `EXCULPATA_ALLOW_CONTAINER_BIND`,
`EXCULPATA_BACKUP_STATUS_FILE`, `EXCULPATA_SERVER_NAME`, and
`EXCULPATA_UPSTREAM` so a new container does not log a deprecation for a
setting it did not choose. The gateway still clears both `X-Exculpata-*` and
`X-RecordBench-*` request headers. When the project or image namespace is
unset, the image-namespace default is `exculpata` and the project-name
default stays `recordbench`. A fresh installer still writes its project name
into `compose.env`. Newly built application images use
`HOME=/tmp/exculpata-home`. A fresh portable review service advertises
`exculpata-qwen35-4b`; `recordbench-qwen35-4b` and `recordbench-qwen35-9b`
remain accepted so an existing generator id still matches. Reconfigure keeps
a served-model id already saved in `compose.env`. `/opt/recordbench` stays
the image workdir. PostgreSQL's user and database remain `recordbench`. Applied SQL
functions keep `recordbench_principal_enabled`,
`recordbench_case_lifecycle_writer`, and
`recordbench_case_content_cleanup`. Container paths such as
`/var/lib/recordbench`, `/run/recordbench-secrets`, and
`/var/lib/recordbench-accounts` stay because existing volumes already use them.
Legacy nodes keep `recordbench.admin` / `RecordBench Administrator`,
`config/recordbench.env`, `.recordbench-managed-storage.json`, and host root
`/srv/recordbench`. Grammar and receipt identifiers stay
`recordbench-exact-v1`, `recordbench-exact-v2`, `recordbench-words-v1`,
`recordbench-question-draft-evaluation-v1`, and
`recordbench-question-draft-evaluation-v2`. The qualified WhisperX build is
`3.8.6+recordbench.1`. Images copy the VAD notice to
`/usr/share/licenses/recordbench-transcription/`. The transcription cache
default is `/var/lib/recordbench/model-cache/huggingface/hub`. Git settings
`recordbench.publicCloneUrl`, `recordbench.publicGitIdentity`,
`recordbench.publicBaselineIdentity`, `recordbench.reviewedMediaDigest`,
`recordbench.publicMergeCommit`, `recordbench.requirePrivatePublicationCheck`,
and `recordbench.publicationDenyFile` are still read.

The previous installer and backup commands remain as shims and print a
one-line deprecation notice. Historical changelog entries use the previous
name. The README uses the Exculpata wordmark. The earlier banner remains at
[assets/exculpata-banner.svg](assets/exculpata-banner.svg), and the original
banner remains at [assets/recordbench-banner.svg](assets/recordbench-banner.svg).

<!-- BEGIN GENERATED RENAME CONTRACT -->
### Rename contract

Generated from `src/case_intelligence/legacy_names.py`. Run
`python scripts/rename_contract_artifacts.py --write` after an intentional contract change;
`python scripts/rename_contract_artifacts.py --check` verifies these artifacts in CI.

Aliases accept the previous name. Generation-specific names preserve an existing
node's coordinates. Retained names stay the same for both generations; they are
not migration targets. Environment aliases prefer a nonempty current value,
while conflicting authentication headers fail closed.

| Contract | Previous name | Current name | Behavior |
| --- | --- | --- | --- |
| administrator | `recordbench.admin` | `exculpata.admin` | generation |
| app_env | `recordbench.env` | `exculpata.env` | generation |
| backup_shim | `recordbench-backup.sh` | `exculpata-backup.sh` | alias |
| backup_status_shim | `recordbench-backup-status.sh` | `exculpata-backup-status.sh` | alias |
| backup_template | `recordbench-backup.{kind}.in` | `exculpata-backup.{kind}.in` | generation |
| backup_tool | `recordbench_backup.py` | `exculpata_backup.py` | generation |
| backup_unit | `recordbench-backup-{node_id}.{kind}` | `exculpata-backup-{node_id}.{kind}` | generation |
| browser_name | `recordbench:{suffix}` | `exculpata:{suffix}` | alias |
| browser_source_note | `recordbenchSourceNote` | `exculpataSourceNote` | alias |
| container_accounts | `/var/lib/recordbench-accounts` | `/var/lib/recordbench-accounts` | retained |
| container_auth | `/run/recordbench-auth` | `/run/recordbench-auth` | retained |
| container_data | `/var/lib/recordbench` | `/var/lib/recordbench` | retained |
| container_secrets | `/run/recordbench-secrets` | `/run/recordbench-secrets` | retained |
| database | `recordbench` | `recordbench` | retained |
| environment | `RECORDBENCH_{suffix}` | `EXCULPATA_{suffix}` | alias |
| exact_v1 | `recordbench-exact-v1` | `recordbench-exact-v1` | retained |
| exact_v2 | `recordbench-exact-v2` | `recordbench-exact-v2` | retained |
| git_config | `recordbench.{suffix}` | `exculpata.{suffix}` | alias |
| gpu_lock | `/tmp/recordbench-transcription-gpu.lock` | `/tmp/recordbench-transcription-gpu.lock` | retained |
| header | `X-RecordBench-{suffix}` | `X-Exculpata-{suffix}` | alias |
| host_root | `/srv/recordbench` | `/srv/exculpata` | generation |
| hostname | `recordbench.example.test` | `exculpata.example.test` | generation |
| ignored_node_directory | `recordbench-node` | `recordbench-node` | retained |
| image_workdir | `/opt/recordbench` | `/opt/recordbench` | retained |
| installer_shim | `recordbench_install.py` | `exculpata_install.py` | alias |
| keytab | `recordbench.keytab` | `recordbench.keytab` | retained |
| legacy_generator_9b | `recordbench-qwen35-9b` | `recordbench-qwen35-9b` | retained |
| local_admin_group | `recordbench_administrators` | `recordbench_administrators` | retained |
| local_users_group | `recordbench_users` | `recordbench_users` | retained |
| login_rate_zone | `recordbench_login` | `recordbench_login` | retained |
| nemotron_schema | `recordbench-nemotron-v1` | `recordbench-nemotron-v1` | retained |
| nemotron_temporary | `recordbench-nemotron-` | `recordbench-nemotron-` | retained |
| ocr_limit | `recordbench-ocr-limit` | `recordbench-ocr-limit` | retained |
| oidc_admin_group | `RecordBench_Administrators` | `RecordBench_Administrators` | retained |
| oidc_users_group | `RecordBench_Users` | `RecordBench_Users` | retained |
| portable_generator | `recordbench-qwen35-4b` | `exculpata-qwen35-4b` | alias |
| product | `RecordBench` | `Exculpata` | alias |
| proxy_entrypoint | `/usr/local/sbin/recordbench-auth-entrypoint` | `/usr/local/sbin/recordbench-auth-entrypoint` | retained |
| proxy_server_config | `/etc/apache2/conf-available/recordbench-server-name.conf` | `/etc/apache2/conf-available/recordbench-server-name.conf` | retained |
| proxy_server_id | `recordbench-server-name` | `recordbench-server-name` | retained |
| proxy_site | `/etc/apache2/sites-available/recordbench-auth.conf` | `/etc/apache2/sites-available/recordbench-auth.conf` | retained |
| proxy_site_id | `recordbench-auth` | `recordbench-auth` | retained |
| proxy_template | `recordbench-auth.conf.template` | `recordbench-auth.conf.template` | retained |
| question_draft_set | `recordbench-question-draft-set-v1` | `recordbench-question-draft-set-v1` | retained |
| question_draft_v1 | `recordbench-question-draft-evaluation-v1` | `recordbench-question-draft-evaluation-v1` | retained |
| question_draft_v2 | `recordbench-question-draft-evaluation-v2` | `recordbench-question-draft-evaluation-v2` | retained |
| restore_shim | `recordbench-restore-drill.sh` | `exculpata-restore-drill.sh` | alias |
| retrieval_cli | `recordbench-retrieval-worker` | `exculpata-retrieval-worker` | alias |
| review_acceptance | `recordbench-review-acceptance-v1` | `recordbench-review-acceptance-v1` | retained |
| sql_function | `recordbench_{suffix}` | `recordbench_{suffix}` | retained |
| stage_receipt | `.recordbench-stage-receipt.json` | `.recordbench-stage-receipt.json` | retained |
| stem | `recordbench` | `exculpata` | generation |
| storage_marker | `.recordbench-managed-storage.json` | `.exculpata-managed-storage.json` | generation |
| transcription_notice | `/usr/share/licenses/recordbench-transcription` | `/usr/share/licenses/recordbench-transcription` | retained |
| whisperx_build | `3.8.6+recordbench.1` | `3.8.6+recordbench.1` | retained |
| words_v1 | `recordbench-words-v1` | `recordbench-words-v1` | retained |
| workbench_cli | `recordbench-workbench` | `exculpata-workbench` | alias |
| worker_gpu_lock | `/run/lock/recordbench-transcription-gpu.lock` | `/run/lock/recordbench-transcription-gpu.lock` | retained |
| worker_name | `recordbench-transcription-worker-1` | `recordbench-transcription-worker-1` | retained |
<!-- END GENERATED RENAME CONTRACT -->
