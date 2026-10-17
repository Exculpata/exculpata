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
`config/recordbench.env`, and `.recordbench-managed-storage.json`.

The previous installer and backup commands remain as shims and print a
one-line deprecation notice. Historical changelog entries use the previous
name. The README uses the Exculpata wordmark. The earlier banner remains at
[assets/exculpata-banner.svg](assets/exculpata-banner.svg), and the original
banner remains at [assets/recordbench-banner.svg](assets/recordbench-banner.svg).
