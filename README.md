<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/brand/wordmark-cyan-transparent-lighttext.png">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/brand/wordmark-cyan-transparent-darktext.png">
    <img src="docs/assets/brand/wordmark-cyan-dark.png" width="560" alt="Exculpata">
  </picture>
</h1>

<p align="center">your discovery, cited.<br>local-first by default.</p>

<p align="center"><strong>Local-first discovery and case review for defense teams.</strong></p>

Discovery arrives in folders. Understanding a case takes more.

Exculpata is a self-hosted workspace for legal discovery review.
It brings documents, images, email, spreadsheets, audio,
and video into one place to search, review, ask questions, and develop work
product with the source material close at hand.

The aim is practical: help attorneys, investigators, and support staff turn
unorganized discovery into a clearer understanding of the case. The same
tools can support other legal and investigation teams.

Exculpata is designed to run on infrastructure you control. Fresh installations
on `main` start without bundled model downloads and connect to up to seven
operator-managed HTTPS model APIs (such as local or LAN vLLM, Ollama, llama.cpp,
and speech servers) with per-matter owner/admin approval.

> **Community beta v0.1.0-beta.1.** Follow the [installation playbook](docs/INSTALL.md)
> and [model configuration](docs/MODEL_CONFIGURATION.md) with synthetic material
> on `main` (or the tag-specific [GPU reference installation](docs/BETA_REFERENCE_INSTALL.md)
> when checking out `v0.1.0-beta.1`). This prerelease is for evaluation and
> contribution. It is not production certification for confidential casework.
> See the [beta release notes](docs/BETA_RELEASE_NOTES.md). Checksummed source
> artifacts and recorded image and model identities are follow-ups for a later beta.

The beta ethics kit provides an evidence-linked [data-handling statement](docs/ethics/DATA_HANDLING.md),
[vendor-vetting checklist](docs/ethics/VENDOR_VETTING_CHECKLIST.md), and
[client discussion template](docs/ethics/CLIENT_DISCLOSURE_TEMPLATE.md).
It supports a lawyer’s assessment of a specific installation and use; it is not legal advice or a compliance certification.

## See Exculpata in action

A one-minute launch video follows a fictional Calder Reservoir administrative
inquiry built from synthetic records. Desktop (16:9) is first, then the vertical
(9:16) cut. The case is fictional; the app and its actions are real. This is a
preview, not production validation.

### Launch video · 1:00 · 16:9

https://github.com/user-attachments/assets/aac555ae-8c88-4deb-9719-69a5d646e279

[Watch or download the 16:9 launch video](docs/assets/exculpata-launch-16x9.mp4?raw=true)

### Launch video · 1:00 · 9:16

https://github.com/user-attachments/assets/1a71e2e1-eee5-4c60-8055-c647edc7e11f

[Watch or download the 9:16 launch video](docs/assets/exculpata-launch-9x16.mp4?raw=true)

Want to help shape it? [Help build Exculpata](#help-build-it)—workflow feedback,
synthetic test material, and clear bug reports are as useful as code.

## Try the demo

From a [development checkout](CONTRIBUTING.md), run:

```console
make demo
```

The command prepares **Harbor Street**, a wholly invented matter with 40 records,
then prints a loopback URL and a synthetic sign-in: choose **Taylor Morgan**,
with no password. Text reports, email, a CSV and two PDFs provide recurring
people, places and dates. One deliberately damaged text file remains visible as
unreadable. The folder goes through ordinary intake, with a selection receipt,
and processing and automatic discovery finish before the URL is printed.

Home shows the landed discovery briefing: who appears most, the busiest dates,
and what could not be read. The demo also enables one-box routing and deeper
investigation. Explore the originals, exact search, and the suggested people,
places, and dates. Answers and automatic every-source analysis need a configured
generator; the existing unavailable message keeps this limitation visible, while
search and manual source review work offline.

No model, service or network connection is needed after development dependencies
are installed. The demo ignores deployment settings, uses only synthetic records,
and binds to `127.0.0.1`. It keeps its throwaway data in the system temporary
directory, outside the checkout. Press **Ctrl-C** to stop and remove that data;
each run starts fresh. This preview sign-in is for synthetic evaluation only.
See the [corpus provenance](demo_data/README.md).

## Help build Exculpata

This is the maintainer-led community beta, v0.1.0-beta.1. It does not publish any
organization's private deployment, records, or endorsement. Contributions use
synthetic examples only, including issue descriptions, screenshots, and logs.

Start with the [contributor backlog](docs/CONTRIBUTOR_BACKLOG.md) and
[contribution guide](CONTRIBUTING.md). Installation feedback, browser tests,
accessibility improvements, and focused workflow fixes are welcome. Coordinate
an issue before starting so contributors do not duplicate in-flight work.
See the [public-alpha boundary](docs/PUBLIC_ALPHA.md) for scope and limitations.

## What you can explore today

Saved investigations and source checks can become editable Reports retaining
findings, gaps, source support, and human review. Investigations show their
recorded work limits; **Check export** identifies saved Reports needing attention
before a complete bundle download. See the [selected acceptance scope](docs/SELECTED_ACCEPTANCE_2026-09-09.md)
for the component revisions and deferred contributions.

| Workflow | Current capabilities |
| --- | --- |
| **Bring records together** | Review selected files, folders, and DAT/OPT/LFP/DII production load files before transfer, verify production index references and producer DAT field mappings, retain every confirmed item and skipped reason in a durable selection receipt, then use resumable uploads, malware scanning, text extraction, and bounded OCR. See [load files](docs/LOAD_FILES.md). |
| **Review documents and media** | Source and [discovery review list](docs/DISCOVERY_REVIEW.md) browsing, companion native/text and indexed image opening, word and meaning-based search, recording checks before transcription, timestamped transcripts, playback, and clips. Videos without audio remain playable; uncertain checks offer listen/continue/retry. |
| **Ask the Case Assistant** | One matter-scoped, tool-using assistant searches discovery, reads sources, queries spreadsheets/tables, reads email threads and transcript windows, runs exact `Decimal`/date calculations with receipts, shows its expandable work trace, cites every source, flags record gaps, and proposes reviewer-gated case-memory entries. |
| **Screen a collection** | Apply a criterion to a frozen source population, then inspect coverage and human-review status. |
| **Organize the work** | Shared matters with owner/admin renaming, attributed activity, review codes, notes, people, places, dates, and report drafting. |
| **Take work product with you** | Export conversations, transcripts, summaries, clips, notebooks, and reports. Complete matter bundles include every saved draft/final Report and every selected-file receipt. |

Selected-file receipts identify selection reasons as browser-reported observations,
stay available after reload, and distinguish received bytes
from processing or source availability. Retry interrupted uploads without losing
previous bytes, open exact source support, and download individual receipts or the
complete matter bundle. Receipt metadata has cumulative capacity limits; owners
can discard receipts with no received data to recover space, cancelling any
pending uploads for that selection. See
[selection receipts and recovery](docs/INTAKE_RECEIPTS.md).
New confirmed uploads retain separate [source occurrences](docs/UPLOAD_OCCURRENCES.md)
when another selection uses the same path or bytes; retries keep the original source.

The [People & things workspace](docs/ENTITY_WORKSPACE.md) keeps distinct
matter identities, aliases, original supported mentions and reviewer history.
[Entity discovery](docs/ENTITY_DISCOVERY.md) incrementally visits inventoried
text-review units and proposes occurrences for reviewer reconciliation. Its
rule-based recognizer has limited recall; processing a unit does not establish
that every name was found. [Manual events and assertions](docs/EVIDENCE_ASSERTIONS.md)
retain typed roles, uncertain dates, supporting and competing originals, and
reviewer corrections. Entity records do not yet feed automatic Report compilation.

[Full-text synthesis](docs/FULL_TEXT_SYNTHESIS.md) connects saved machine findings
from one terminal full-text review to the shared hierarchy for its complete
criterion. Reviewers can inspect originals, frozen human context, coverage gaps
and omissions, then export JSON, Markdown or Word. Complete originals over
6,000 characters are omitted in full. Multiple runs, new questions, greater
capacity and copying this adapter into a Report remain outside the first substep;
production model defaults are unchanged.

Sources also offers [folder navigation](docs/SOURCE_FOLDER_NAVIGATION.md), with
child-folder counts, parent navigation and the existing collection, source-set
and review filters. Folder browsing preserves source identities and originals.
Sources with [identical received bytes](docs/EXACT_BYTE_MATCHES.md) show a count
and a link to inspect those files together across collections. A matching-files
filter and saved source groups help review copies while preserving every source
and exact reference. Group actions return to the current comparison and filters.

Email review shows attachment names and types while keeping attachment contents
outside newly extracted parent-message text. Search, answers, investigations and saved exports
explain that attachments are not fully searched and need separate review. Older
saved passages retain their citation basis. If sources or the selected source group change while a question
retrieves evidence or prepares an answer, its saved result explains that newly
available material may be absent and the question should be run again. Sources
still uploading or processing are identified as excluded. Recovered
investigations repeat their searches when the sources or selected group have changed. Coverage links include ready email
alongside sources needing attention; see [email attachment coverage and
recovery](docs/EMAIL_ATTACHMENT_COVERAGE.md).

Case-note edits preserve unsaved fields when another team member has changed
the note. Confirm/Delete actions also require the version just reviewed. See
[shared note editing and recovery](docs/CASE_NOTE_EDITING.md).

Shared Report editing also preserves unsaved text after a conflicting edit.
Final status, reordering and deletion require the work just reviewed, while
teammates can edit different sections independently. See
[Report editing and recovery](docs/REPORT_EDITING.md).

Every-source validation also protects shared reviewer decisions and notes from
older forms, shows the current reviewer, and keeps machine results separate.
See [team source validation](docs/REVIEW_VALIDATION.md).

Matter owners and administrators can rename a matter in **Settings**. Stale edits
keep the proposed name for review; existing source links and saved work remain
available. See the [matter settings contract](docs/MATTER_SETTINGS.md).

AI availability depends on the selected deployment profile. OCR, transcription,
source screening, and generated answers require human review. A completed task
does not establish that every page was read or every relevant fact was found.

## Review with one assistant, without choosing a mode

| Task | Use it when you need to… |
| --- | --- |
| **Ask the Case Assistant** | Prompt in plain words. One matter-scoped assistant decides how to search discovery, read originals, query tables, inspect email threads and transcript windows, compute exact `Decimal`/date math with receipts, cite every source, flag record gaps, and propose reviewer-gated case-memory entries—all with an expandable work trace and live stage labels. |
| **Check every source** | Apply the same criterion to every source in a frozen collection and validate the results. This is a specialized source-screening task, not another chat mode. |
| **Review Sources** | Browse production review lists and folders, open DAT companion files and OPT/LFP/DII indexed images, filter by type or collection, apply review codes, upload separately, and review media with one-click Playback, Summary, Export, and Clips tabs beside an independently usable transcript. |

Checkpoint continuation with explicit budgets also remains available for
[evidence-driven investigation](docs/EVIDENCE_DRIVEN_INVESTIGATION.md). Checking
every source stays separate because it freezes a population and produces one
decision per source.

## How it works

1. **Intake:** Exculpata previews selected loose-file and DAT/OPT/LFP/DII
   production metadata without copying bytes, checks production index
   references, records the complete selection on confirmation, then revalidates
   the included subset as it enters a private staging area and applies any
   required malware scan.
2. **Processing:** It extracts usable text, applies bounded OCR where needed,
   and checks audio/likely speech before sending media through an operator-configured
   speech API (or a legacy bundled transcription queue).
   [Recording checks](docs/MEDIA_PREFLIGHT.md) preserve original timestamps and
   keep uncertain results reviewable.
3. **Indexing:** It divides the resulting text into traceable passages and
   makes them available to word and meaning-based search.
4. **Evidence selection & Case Assistant reasoning:** A multi-stage retrieval
   pipeline finds candidate passages, combines keyword and semantic results, and
   reranks candidates. The Case Assistant uses matter-scoped read-only tools
   (`list_sources`, `search_discovery`, `read_source`, `get_case_knowledge`,
   `calculate`, `query_table`, `read_email_thread`, and `read_transcript_window`)
   to connect evidence across documents, show exact math receipts, check premise
   and derivation consistency, and state plainly where the record has gaps.
   Automated checks do not establish that generated claims are correct;
   [review each claim against the originals](docs/GENERATED_ANSWER_REVIEW.md).
5. **Human work product:** The reviewer inspects originals, accepts or dismisses
   proposed case-memory entries, edits Reports, exports bundles, or corrects
   machine-generated transcript labels before anything is treated as finished work.

A failed source remains visible and is excluded from searching until repaired;
it does not prevent the rest of the matter from being used.

This revision includes seven portable corrections covering answer confidence,
authentication, retention safety, source-text presentation, PDF OCR coverage,
cited-excerpt comparison, and a frozen claim-evaluation harness. The
[independent review correction plan](docs/REVIEW_CORRECTION_PLAN.md) links their
implementation and review records and tracks the remaining installed, model,
recovery, and policy acceptance work. These changes do not close
[issue #109](https://github.com/Exculpata/exculpata/issues/109).

## What Exculpata uses

Fresh installations on `main` run the application stack without downloading or
bundling model containers. Operators connect up to seven independent HTTPS model
APIs (such as self-hosted vLLM, Ollama, llama.cpp, LM Studio, or speech APIs).
Legacy bundled reference model revisions and licenses live in
[`config/models.json`](config/models.json).

| Component | What it does in Exculpata |
| --- | --- |
| **Python, FastAPI, Jinja, and JavaScript** | Run the multi-user web application, Case Assistant, background work, and review interface. |
| **PostgreSQL, pgvector, and SQLite** | Store matter control data, full-text indexes, semantic vectors, queues, sessions, and audit history. |
| **ClamAV** | Scans uploads before they can become reviewable matter sources. |
| **Tesseract and Poppler** | Render and OCR scanned PDF pages and image evidence locally on CPU. |
| **FFmpeg** | Probes media, prepares browser-compatible playback, and creates requested clips. |
| **Operator-managed HTTPS model APIs (up to 7)** | Optional endpoints for answer generation (`GENERATOR`), 768-dim embeddings (`EMBEDDING`), passage reranking (`RERANK`), speech transcription (`ASR`), word alignment (`ALIGNMENT`), translation (`TRANSLATION`), and speaker labeling (`DIARIZATION`). |
| **Legacy bundled GPU services (`v0.1.0-beta.1` / existing nodes)** | Existing bundled nodes keep their offline Granite embedding, reranker, vLLM, and WhisperX / Nemotron workers on `--resume`/`update` until migrated in place with `--retire-bundled-models`. |
| **nginx, local accounts, OIDC, or Kerberos** | Provide HTTPS and selectable identity options for different organizations. |
| **Docker Compose, CONNECT egress relay, and restic** | Package isolated services, restrict outbound model/OIDC traffic to allowlisted pins, and provide encrypted backup and restore tooling. |

On fresh installations, `--models` defaults to `none` (bundled `--models review`,
`transcription`, and `all` are retired for new nodes). Each optional AI capability
stays disabled until an operator configures its HTTPS endpoint and an owner or
administrator approves a **per-matter grant** naming the capability, endpoint,
and model.

The installer can configure an answer server directly via
`--external-generator ollama|llamacpp|lmstudio|vllm` with its HTTPS origin
(for example `https://answers.example.test:8443`) and model name, or operators
can configure `CASE_INTELLIGENCE_GENERATOR_BACKEND` (`ollama` or `openai`) and
any of the seven API prefixes in `config/exculpata.env`:

- `ollama` uses native `/api/tags` and `/api/chat` (answer schema in `format`).
- `openai` (`vllm`, `llamacpp`, `lmstudio`, or OpenAI-compatible APIs) uses
  `/v1/models` and `/v1/chat/completions` with `response_format.json_schema.schema`.
  There is no Open WebUI adapter or test; pointing the `openai` backend at
  another product works only when that product implements this exact contract.
- All seven external model slots route through the constrained CONNECT egress
  relay; private RFC1918/ULA destinations require explicit reviewed `*_EGRESS_PINS`,
  and hosts outside the local network require `--confirm-remote-generator` (or
  `*_REMOTE_CONFIRMED=1`) because matter text leaves the local network.
- Host loopback servers, including `127.0.0.1:11434`, are unreachable from the
  containerized app (no host-gateway alias is installed). External HTTP URLs and
  `$` characters in model configuration values are rejected.
- Startup and `doctor` send a small schema check to configured answer servers
  and report whether the server enforced it. Answers from a model with no
  recorded claim-evaluation pass are labeled `unvalidated model`.
- Existing bundled nodes can migrate in place to operator-managed model APIs
  with `./install update --root <path> --retire-bundled-models` while preserving
  private transcript history.

Full settings, limits, per-matter grants, and speech/retrieval API contracts are
in [model configuration](docs/MODEL_CONFIGURATION.md).

## Hardware planning targets

These are evaluation configurations, not guaranteed minimums or collection-size
and concurrency limits. Larger matters, longer recordings, and more simultaneous
users need additional storage and capacity.

**Fresh endpoint-based nodes (current `main` default)** run the Exculpata
application stack on CPU and connect to separate operator-managed model APIs
(running on your own GPU host(s) or approved API provider):

| Intended use | CPU | RAM | NVIDIA GPU on app host | Free SSD before matter data |
| --- | ---: | ---: | --- | ---: |
| **Fresh application node (endpoint-based or zero-endpoint CPU)** — intake, DAT/OPT/LFP/DII load files, OCR, exact search, source review, exports, and optional operator HTTPS model APIs | 8 cores | 16 GB | None (models run on operator API host) | 150 GB production, or 13 GB with `--storage-profile evaluation` |

**Legacy bundled GPU reference targets (`v0.1.0-beta.1` tag / existing bundled nodes only):**
Cited answers with the portable bundled model were admitted from 16 GB of NVIDIA
VRAM, and the `v0.1.0-beta.1` reference GPU journey (portable answers, English
and Spanish transcription, and Nemotron speaker labels) ran on 24 GB (driver and
CUDA versions not recorded; no separate 16 GB reference run recorded).

| Legacy `v0.1.0-beta.1` bundled profile | CPU | RAM | NVIDIA GPU | Free SSD before matter data |
| --- | ---: | ---: | --- | ---: |
| **Bundled document review and cited answers** | 12 cores | 32 GB | 1 GPU with 16 GB VRAM minimum | 200 GB |
| **Bundled transcription and speaker labels** | 16 cores | 64 GB | 1 GPU with 24 GB VRAM, tested reference | 300 GB |

File hardware results on the
[hardware report](https://github.com/Exculpata/exculpata/issues/new?template=hardware_report.yml)
form. Collected rows live in [compatibility](docs/COMPATIBILITY.md). Consult
[release readiness](docs/RELEASE_READINESS.md) before treating a configuration
as supported.

Use a modern x86-64 Linux host with Docker Engine (26.0.0+) and Docker Compose
v2 (2.20.2+). Legacy bundled GPU profiles also require an NVIDIA card with
compute capability 7.5 or newer and the NVIDIA Container Toolkit.

Matter storage may be a dedicated local path or a host-mounted NAS path.
Free-space targets cover application images, temporary processing, and the
configured safety reserve; matter contents require additional local or NAS
capacity.

## What Exculpata is not

Exculpata is not:

- the authoritative evidence warehouse or a legal-hold system;
- a case-management or filing system;
- a validated redaction platform;
- a guarantee that OCR, transcription, retrieval, or generation is complete
  or correct; or
- a substitute for a lawyer, investigator, analyst, or other human reviewer.

Transcripts describe what the machine heard, not independently established
facts. Generated answers are review aids and should always be checked against
their cited sources.

## Quick start

### Run a node

To try the product or work on its UI, follow the short
[Ubuntu 24.04 CPU node path](docs/INSTALL.md#cpu-node-quick-start-ubuntu-2404):
dedicated service account and Docker → `./install` with `--models none` and
`--storage-profile evaluation` → `doctor` →
[first-admin sign-in and a synthetic practice matter](docs/FIRST_RUN.md#cpu-practice-matter).
Evaluation is for synthetic or sample data. The workbench shows
"Evaluation node, not for real case data". Measured on 2026-10-15, the four
CPU runtime images from that install total 2,223,313,193 bytes (2.071 GiB).
With the default 10 GiB evaluation reserve, the advisory target is 13 GiB
free before matter data. The production profile stays at the 150 GB target
and the 100 GiB reserve. The path keeps HTTPS on loopback and uses local
accounts. Stop, start, upgrade, uninstall, and a clean reinstall are in
the [node lifecycle](docs/INSTALL.md#node-lifecycle). Ubuntu 24.04 is the
accepted host. The guide lists Arch Linux package equivalents; that path
has not been accepted as a working node.

### Contribute code

Follow [development checkout setup](CONTRIBUTING.md#set-up-a-development-checkout),
then run `make bootstrap` and `make check`. This path does not require an
installed node. Use the node path when you need the running product for UI work.

### Operator reference

Read the full [installation playbook](docs/INSTALL.md) before staff access.

The installer also provides commands for node diagnostics, optional encrypted
backup, isolated restore testing, and versioned updates. Operators can use
their existing backup system or the bundled restic method with a destination
available through their host filesystem; Exculpata does not mount shares or
choose a storage provider. Follow the [consistency and recovery requirements](docs/STORAGE_AND_BACKUP.md)
for either method. An office evaluating this prerelease can follow
[pilot acceptance](docs/PILOT_ACCEPTANCE.md). That guide lists synthetic checks
and the evidence already in this repository. It is not a security certification
and it does not promise a result.

```bash
./install doctor --root /srv/exculpata
./install backup --root /srv/exculpata --repository /mnt/backup/exculpata \
  --recovery-key-output /mnt/recovery/exculpata-password
./install restore --root /srv/exculpata \
  --restore-target /srv/exculpata-restore-test
./install update --root /srv/exculpata
```

## Data and repository boundaries

Matter bytes live under the operator-selected managed storage path. A stored
date or an elapsed check-in does not remove a workspace. Export and verify
anything that must be kept outside the workbench. The complete matter bundle
includes saved Reports, preserving their edits, order, draft/final state, and
source appendices in Word and Markdown.
If saved work exceeds export limits or Report sources cannot be verified,
no complete bundle is returned. Use **Check export** from Work product or the
final-export step to inspect current saved work and open Reports that need
attention while the matter is open. After a failed close, it retains Report details
and returns to Close matter recovery. The check does not save an export; downloading
validates again.
[Export readiness](docs/EXPORT_READINESS.md) explains the check, and
[Report export and recovery](docs/REPORT_EXPORTS.md)
describes limits and interrupted-close recovery. Bundles are ordinary portable
documents, not a package that can be imported back into Exculpata.
Matters remain ongoing until explicitly closed, with a 90-day in-app check-in.
Owner/admin **Close matter** freezes changes, creates a verified full preservation
archive including retained originals, and requires a separate fresh confirmation
after a minimum seven-day cancellation window. Dates never authorize deletion.
Existing stored dates are preserved for review and automatic purge is disabled.
See [matter lifecycle and recovery](docs/CASE_LIFECYCLE.md) for the export boundary,
legacy migration, cancellation, interrupted deletion and backup/restore steps.
Final deletion preserves external originals and content-minimized audit and
closure records. The full archive is not an automated importer or legal
retention guarantee.

No case data, organization secrets, internal accounts, private deployment
coordinates, certificates, or credentials belong in this repository.

## Where this is going

Exculpata is building a free, local-first review workspace for case teams:
excellent manual review, complete exact search, accountable collection-wide
processing, sourced investigation, and a shared memory of what the team learns.
Administrators should be able to choose storage and supported model combinations,
from a small local server toward larger GPU installations.

The [product north star](docs/PRODUCT_NORTH_STAR.md) describes that experience,
including production-folder intake, background material separate from discovery,
people and events with original support, and reviewer-controlled matter memory.
The [current assessment and delivery plan](docs/PRODUCT_DIRECTION_2026-09-12.md)
identifies earlier baselines and a staged path forward; active work is tracked
in GitHub issues and milestones.

These are development goals. They are not all available in the current prerelease.

## Help build it

Useful contributions include synthetic discovery examples, clear descriptions
of review tasks, reproducible bugs, accessibility feedback, documentation,
and clean-host installation results. A small, well-described problem is a good
place to start. See [CONTRIBUTING.md](CONTRIBUTING.md) for the development
workflow (`make bootstrap` with Python 3.12, then `make check`), and
[SECURITY.md](SECURITY.md) for private vulnerability reporting.

Use invented examples. Keep case material, private logs, and deployment
details out of issues, pull requests, and screenshots.

## Project status

Exculpata [v0.1.0-beta.1](https://github.com/Exculpata/exculpata/releases/tag/v0.1.0-beta.1)
is the published community beta prerelease. The tagged source is available for
evaluation and contribution. Checksummed source artifacts and recorded image and
model identities are follow-ups for a later beta. Supported production-release requirements remain
open. Sharing the source and supporting confidential casework each require the
evidence described in [release readiness](docs/RELEASE_READINESS.md) and the
[publication checklist](docs/PUBLICATION_CHECKLIST.md).

## Documentation

- [Installation](docs/INSTALL.md)
- [First run and practice matter](docs/FIRST_RUN.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Configuration](docs/CONFIGURATION.md)
- [Model configuration (operator HTTPS APIs and per-matter grants)](docs/MODEL_CONFIGURATION.md)
- [Models and origin policy](docs/MODELS.md)
- [Authentication: Local accounts, OIDC, and Kerberos](docs/AUTHENTICATION.md)
- [Network egress and CONNECT relay](docs/NETWORK_EGRESS.md)
- [Discovery review list and viewer](docs/DISCOVERY_REVIEW.md)
- [Load files (DAT, OPT, LFP, DII)](docs/LOAD_FILES.md)
- [OCR and media](docs/OCR_AND_MEDIA.md)
- [Matter lifecycle and recovery](docs/CASE_LIFECYCLE.md)
- [Storage, backup, and restore](docs/STORAGE_AND_BACKUP.md)
- [Security model](docs/SECURITY_MODEL.md)
- [Pilot acceptance](docs/PILOT_ACCEPTANCE.md)
- [Release readiness](docs/RELEASE_READINESS.md)
- [Visual identity: logo, type, color, and positioning](docs/BRAND.md)
- [Brand assets and project copy](docs/BRANDING.md)

## License

Exculpata is licensed under [Apache-2.0](LICENSE). Dependencies, containers,
and models retain their respective licenses and terms. Their notice inventory
remains part of the [publication review](docs/PUBLICATION_CHECKLIST.md).
The launch videos, including their soundtracks, are also licensed under
Apache-2.0. Only the previous teaser's original soundtrack is excluded from
the software license; see [media rights](docs/assets/MEDIA_RIGHTS.md).

Outside contributors sign the [CLA](CLA.md) once, via the CLA Assistant
GitHub app. The Exculpata name and logo are not licensed under Apache-2.0; see
[TRADEMARKS.md](TRADEMARKS.md).
