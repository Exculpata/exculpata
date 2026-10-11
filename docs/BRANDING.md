# Exculpata brand assets and project copy

Use these assets and descriptions for the repository, project pages, and
presentations. They describe a development alpha; they do not imply an
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
sign-in screen still shows "Review the record. Build the work." Product UI
colors and type are unchanged.

## Assets

| Asset | Use |
| --- | --- |
| [Icon](assets/brand/exculpata-icon.svg) | Canonical mark. The application favicon uses the same drawing. |
| [Wordmark](assets/brand/exculpata-wordmark.svg) | `> exculpata` with the block cursor, for dark backgrounds. |
| [Light-text wordmark](assets/brand/wordmark-cyan-transparent-lighttext.png) | README hero on a dark background. |
| [Dark-text wordmark](assets/brand/wordmark-cyan-transparent-darktext.png) | README hero on a light background. |
| [Social preview](assets/brand/social-preview-1280x640.png) | 1280 × 640 image for the GitHub social preview setting. |
| [Desktop walkthrough](assets/recordbench-desktop-walkthrough.mp4) | 4:47 landscape tour of the fictional Harbor Street matter, at 1280 × 720. |
| [Mobile walkthrough](assets/recordbench-mobile-walkthrough.mp4) | 5:34 portrait tour of the same fictional matter, at 720 × 1280. |

The SVGs are self-contained. They contain no external image or font requests,
case screenshots, or deployment details. The application favicon repeats the
canonical icon; keep them synchronized if the mark changes.
Use descriptive alternative text when embedding an asset.

## Demo walkthroughs

Present the videos as **Desktop walkthrough** and **Mobile walkthrough**, under
**See Exculpata in action**. They replace the former 51-second teaser as the
repository's featured demos. Both follow the fictional Harbor Street matter
through records, source-linked questions, review, and work product. Keep the
on-screen labels and fictional-case/development-alpha notices visible. Do not
present either walkthrough as production validation or institutional endorsement.

For GitHub, upload each video as an attachment and place its stable
`github.com/user-attachments/assets/` URL alone in a paragraph to render a
player. A repository-file link is a download fallback, not an inline player.
Keep that fallback for other Markdown viewers. Never commit the temporary signed
media URL returned by the renderer. Preserve each video's aspect ratio and timing.

See [media rights](assets/MEDIA_RIGHTS.md) for the supplied walkthroughs and the
separate rights notice retained for the older teaser.

## Short description

Local-first discovery and case review for defense teams. Search records,
review media, organize findings, and build work product. Development alpha.

## Project introduction

Discovery arrives in folders. Understanding a case takes more.

Exculpata is a self-hosted workspace being built for the work of federal
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

Describe the current alpha's implemented workflows separately from its
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

Exculpata is the visible product name. Package names, environment variables,
headers, storage markers, format IDs, installation paths and repository URLs
retain their existing identifiers. The health response and JSON export product
fields retain `RecordBench`; the CSV column key `RecordBench label` also remains
stable for existing consumers. Human-readable document exports use Exculpata.

The previous banner and recorded demo videos remain historical assets. The
README uses the Exculpata wordmark. The earlier banner remains at
[assets/exculpata-banner.svg](assets/exculpata-banner.svg), and the original
banner remains at [assets/recordbench-banner.svg](assets/recordbench-banner.svg).
