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

Exculpata is designed to run on infrastructure you control. Its bundled AI
workflows use local models for retrieval, answers, and media transcription.

> **Community beta candidate:** Follow the [GPU reference installation](docs/BETA_REFERENCE_INSTALL.md)
> with synthetic material. The beta has not been released yet; final reference
> acceptance and maintainer approval are pending. This is not production
> certification for confidential casework. See the [candidate release notes](docs/BETA_RELEASE_NOTES.md).

## See Exculpata in action

Follow a fictional Harbor Street matter from adding records to source-linked
answers, media review, notes, timelines, reports, and export. These desktop and
mobile walkthroughs use synthetic records and demonstrate the development alpha.

### Desktop walkthrough · 4:47

https://github.com/user-attachments/assets/e14f468e-7ea6-46de-84e0-d9c865e1db53

[Watch or download the desktop walkthrough](docs/assets/recordbench-desktop-walkthrough.mp4?raw=true)

### Mobile walkthrough · 5:34

https://github.com/user-attachments/assets/2506daba-2922-4f8a-b2a0-b5e518b10a27

[Watch or download the mobile walkthrough](docs/assets/recordbench-mobile-walkthrough.mp4?raw=true)

On-screen labels explain each step. The case is fictional; the app and its
actions are real. These walkthroughs are previews, not production validation.

Want to help shape it? [Help build Exculpata](#help-build-it)—workflow feedback,
synthetic test material, and clear bug reports are as useful as code.

## Help build Exculpata

This is a maintainer-led community beta candidate. It does not publish any
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
| **Bring records together** | Review selected files and folders before transfer, retain every confirmed item and skipped reason in a durable selection receipt, then use resumable uploads, malware scanning, text extraction, and bounded OCR. |
| **Review documents and media** | Source browsing, word and meaning-based search, recording checks before transcription, timestamped transcripts, playback, and clips. Videos without audio remain playable; uncertain checks offer listen/continue/retry. |
| **Ask questions with sources in view** | Local AI answers and follow-up conversations with citations and coverage information. |
| **Screen a collection** | Apply a criterion to a frozen source population, then inspect coverage and human-review status. |
| **Organize the work** | Shared matters with owner/admin renaming, attributed activity, notes, people, places, dates, and report drafting. |
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

## Review without choosing a processing mode

| Task | Use it when you need to… |
| --- | --- |
| **Review** | Ask a focused question, investigate through several searches, and continue with follow-ups in one cited conversation. The composer explains the time and coverage difference. |
| **Check every source** | Apply the same criterion to every source in a frozen collection and validate the results. This is a specialized source-screening task, not another chat type. |
| **Review Sources** | Browse up to 100 sources immediately, navigate exact folder subtrees, filter by type or collection, open filenames directly, upload separately, and review media with one-click Playback, Summary, Export, and Clips tabs beside an independently usable transcript. |

Broader investigations now follow source-backed names, dates, phrases, and identifiers,
show their search reasons, and support checkpoint continuation with explicit
additional budgets. See [evidence-driven investigation](docs/EVIDENCE_DRIVEN_INVESTIGATION.md).

Focused answers and broader investigations share conversational continuity;
the processing strategy stays behind task language. Checking every source stays
separate because it freezes a population and produces one decision per source.

## How it works

1. **Intake:** Exculpata previews selected loose-file metadata without
   copying bytes, records the complete selection on confirmation, then revalidates
   the included subset as it enters a private staging area and applies any required malware scan.
2. **Processing:** It extracts usable text, applies bounded OCR where needed,
   and checks audio/likely speech before sending media through the bundled
   transcription queue. [Recording checks](docs/MEDIA_PREFLIGHT.md) preserve
   original timestamps and keep uncertain results reviewable.
3. **Indexing:** It divides the resulting text into traceable passages and
   makes them available to word and meaning-based search.
4. **Evidence selection:** A multi-stage retrieval pipeline finds candidate
   passages, combines keyword and semantic results, and reranks the candidates
   for the question being asked.
5. **Answering:** A local generation model answers from the selected material.
   Exculpata checks citation references and text consistency, shows citations
   and collection coverage, and reports important limitations instead of hiding
   them. These checks do not establish that generated claims are correct;
   [review each claim against the originals](docs/GENERATED_ANSWER_REVIEW.md).
6. **Human work product:** The reviewer can inspect the source, save useful
   material, export it, or correct machine-generated transcript labels before
   anything is treated as finished work.

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

These are the current bundled defaults. Exact model revisions and license
information live in [`config/models.json`](config/models.json).

| Component | What it does in Exculpata |
| --- | --- |
| **Python, FastAPI, Jinja, and JavaScript** | Run the multi-user web application, background work, and review interface. |
| **PostgreSQL, pgvector, and SQLite** | Store matter control data, full-text indexes, semantic vectors, queues, sessions, and audit history. |
| **ClamAV** | Scans uploads before they can become reviewable matter sources. |
| **Tesseract and Poppler** | Render and OCR scanned PDF pages and image evidence. |
| **Granite Embedding English R2** | Converts passages and questions into vectors for meaning-based retrieval. |
| **GTE ModernBERT reranker** | Reorders candidate passages so the strongest evidence reaches the answering model. |
| **Qwen3.5 4B or 9B through vLLM** | Generates local answers, summaries, and review assistance from selected passages for source review. |
| **WhisperX and faster-whisper large-v3** | Transcribe media and align transcript text to timestamps. |
| **Optional speaker diarization** | Ungated Nemotron separates up to eight anonymous speakers; Community-1 remains an explicit gated alternative. |
| **FFmpeg** | Probes media, prepares browser-compatible playback, and creates requested clips. |
| **nginx, local accounts, OIDC, or Kerberos** | Provide HTTPS and selectable identity options for different organizations. |
| **Docker Compose and restic** | Package the services and provide encrypted backup and restore tooling. |

Models are run locally after a one-time staging step. Runtime model containers
are configured for offline operation. Diarization is optional because its
upstream model terms require separate acceptance.

## Hardware planning targets

These are starting targets for prerelease evaluation, not validated minimums or
collection-size and concurrency guarantees. Larger matters, longer recordings,
and more simultaneous users need additional storage and capacity. Consult
[release readiness](docs/RELEASE_READINESS.md) before treating a configuration
as supported.

| Intended use | CPU | RAM | NVIDIA GPU | Free SSD before matter data |
| --- | ---: | ---: | --- | ---: |
| **CPU evaluation** — intake, OCR, word search, source review, and exports | 8 cores | 16 GB | None | 150 GB |
| **Local document review** — evaluation features plus semantic retrieval, reranking, and local answers | 12 cores | 32 GB | 1 GPU with 16 GB VRAM | 200 GB |
| **Every-source checks and media** — document review plus transcription and optional diarization | 16 cores | 64 GB | 1 GPU with 24 GB VRAM | 300 GB |

Use a modern x86-64 Linux host with Docker Engine and Docker Compose v2. GPU
profiles require an NVIDIA card with compute capability 7.5 or newer and the
NVIDIA Container Toolkit. The intended deployment model supports one suitable
GPU, with additional GPUs separating generation, transcription, and retrieval
work. Each configuration still needs deployment validation.

Matter storage may be a dedicated local path or a host-mounted NAS path.
Free-space targets cover application images, models, temporary processing,
and the configured safety reserve; matter contents require additional local
or NAS capacity.

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
dedicated service account and Docker → `./install` with `--models none` →
`doctor` → [first-admin sign-in and a synthetic practice matter](docs/FIRST_RUN.md#cpu-practice-matter).
It keeps HTTPS on loopback and uses local accounts. Other distributions are
unproven; the guide lists the portable host and container capabilities.

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
for either method.

```bash
./install doctor --root /srv/recordbench
./install backup --root /srv/recordbench --repository /mnt/backup/recordbench \
  --recovery-key-output /mnt/recovery/recordbench-password
./install restore --root /srv/recordbench \
  --restore-target /srv/recordbench-restore-test
./install update --root /srv/recordbench
```

## Data and repository boundaries

Exculpata uses temporary review workspaces. Matter bytes live under the
operator-selected managed storage path. Manual closure or scheduled expiry can
permanently remove that workspace, so users should export and verify anything
they need to retain. The complete matter bundle includes saved Reports, preserving their
edits, order, draft/final state, and source appendices in Word and Markdown.
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
Manual **Close matter** refuses active work and requires the exact matter name
plus a permanent-deletion acknowledgement. Scheduled retention follows a separate
path: new matters default to 30 days of review plus seven days of export grace,
after which enabled maintenance can delete them without another confirmation.
Maintenance defers deletion while work is active and runs its first pass at
startup. Restoring a backup preserves the original deadlines, which may already
be due; follow [expired-matter recovery](docs/EXPIRED_MATTER_RECOVERY.md) before
starting the restored application. Both deletion paths preserve originals outside
Exculpata and leave only content-minimized attributed audit and closure records.

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
identifies what exists, the remaining gaps, and a staged path forward. The
[exit-alpha cruise](docs/EXIT_ALPHA_CRUISE.md) selects the active build step.

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

Exculpata is preparing its first community beta. The reviewed source is
available for evaluation and contribution; the beta tag and final reference
acceptance remain pending. Supported production-release requirements remain open. Sharing the source and supporting confidential casework each require the evidence described
in [release readiness](docs/RELEASE_READINESS.md) and the
[publication checklist](docs/PUBLICATION_CHECKLIST.md).

## Documentation

- [Installation](docs/INSTALL.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Configuration](docs/CONFIGURATION.md)
- [Authentication: Local accounts, OIDC, and Kerberos](docs/AUTHENTICATION.md)
- [Models](docs/MODELS.md)
- [OCR and media](docs/OCR_AND_MEDIA.md)
- [Storage, backup, and restore](docs/STORAGE_AND_BACKUP.md)
- [Security model](docs/SECURITY_MODEL.md)
- [Release readiness](docs/RELEASE_READINESS.md)
- [Brand](docs/BRAND.md)
- [Brand assets and project copy](docs/BRANDING.md)

## License

Exculpata is licensed under [Apache-2.0](LICENSE). Dependencies, containers,
and models retain their respective licenses and terms. Their notice inventory
remains part of the [publication review](docs/PUBLICATION_CHECKLIST.md).
The new desktop and mobile walkthroughs, including their audio, are also
licensed under Apache-2.0. Only the previous teaser's original soundtrack is
excluded from the software license;
see [media rights](docs/assets/MEDIA_RIGHTS.md).
