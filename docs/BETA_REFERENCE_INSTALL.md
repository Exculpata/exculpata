# GPU community beta reference journey

Status: **v0.1.0-beta.1**, the current community beta prerelease.
The intended beta promise is a reproducible installation for evaluation and
contribution: local accounts, document review with cited answers, English and
Spanish transcription/alignment, and ungated Nemotron diarization. It is not
production certification for confidential workloads. Other identity providers,
languages, GPUs and operator model combinations need their own evidence.

See [what the installer manages](MANAGED_SERVICES.md) for the division between
host prerequisites, managed service containers and operator-selected models.

## Start from the exact reviewed revision

Use a new dedicated Linux x86-64 node, an empty state root, and synthetic data.
The fresh-node examples use Exculpata names. Existing nodes keep their original
account, root, configuration, images, marker and backup units. Follow the
[naming compatibility guidance](BRANDING.md) rather than moving them to the
paths below. `--names exculpata` refuses an existing legacy node because this
release includes no naming migration.
Start with the account, Docker and storage preparation in
[the installation playbook](INSTALL.md#cpu-node-quick-start-ubuntu-2404).
For GPU work, install a suitable NVIDIA driver and the
[official Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
on that new node. Toolkit setup can require engine configuration and restart;
coordinate separately on any shared host. A shell GPU environment variable is
not a device-isolation boundary.

The tested reference is 16 CPU cores, 64 GiB RAM, at least 300 GiB free SSD,
and one NVIDIA GPU with 24 GiB VRAM. Portable cited answers are admitted from
16 GiB VRAM. A separate 16 GiB reference run is not recorded. Driver and CUDA
versions for the 24 GiB run were not recorded. These figures describe the
machines already exercised, not a guaranteed minimum. Capacity for images,
caches and the storage reserve must remain after installation; shared GPUs need
a separately bounded plan. Record the actual hardware with your result.

```sh
git clone --branch v0.1.0-beta.1 --depth 1 https://github.com/Exculpata/exculpata.git exculpata
cd exculpata
git rev-parse HEAD
python3 scripts/stage-models.py plan --catalog config/models.json \
  --groups review,transcription-asr,transcription-alignment-en,transcription-alignment-es,transcription-diarization \
  --review-profile portable --diarization-backend nemotron
```

Review [acquisition and resource terms](MODEL_ACQUISITION.md). The reference
uses large-v3 ASR, the two pinned alignment bundles and Nemotron; no token is
needed. Compatible operator-selected ASR is supported through the
[prepared-inventory import interface](ASR_MODEL_IMPORT.md), but it is a separate
configuration to test. The Fast profile is unavailable unless its artifact is
separately approved and staged.

## Install and exercise the node

As the dedicated service account, in the exact checkout:

```sh
./install preflight --root /srv/exculpata --models all --auth local \
  --enable-account-management --gpu-layout shared --review-model-profile portable \
  --transcription-languages en,es --enable-diarization --diarization-backend nemotron \
  --server-name localhost --bind-address 127.0.0.1
COMPOSE_PARALLEL_LIMIT=1 ./install --root /srv/exculpata --models all --auth local \
  --enable-account-management --gpu-layout shared --review-model-profile portable \
  --transcription-languages en,es --enable-diarization --diarization-backend nemotron \
  --server-name localhost --bind-address 127.0.0.1
./install doctor --root /srv/exculpata
```

The install console uses `PREPARE RUNTIME`, `CONFIGURE SERVICES`, and
`START SERVICES`. `NODE ONLINE` prints only after application health and
sign-in readiness succeed. While those checks are pending, the console
reports elapsed seconds at most once every 30 seconds. Transient probe
errors are held for the final timeout diagnostic. The checks and the
15-minute deadline are unchanged.

Resolve blocking preflight results. Enter the initial administrator password
at the hidden prompt, and acknowledge the displayed acquisition plan after
reviewing it. Do not put passwords or tokens in command lines or reports.
Follow [first run](FIRST_RUN.md) for certificate trust, browser sign-in and a
practice matter. Reach a remote node through the documented SSH tunnel.

Use this concrete synthetic exercise, recording each result:

1. Sign in, create a second local account and a practice matter. Confirm that
   the second account cannot open the matter before an explicit grant; grant
   access, confirm it works, then revoke it and confirm access is denied.
2. Create two text files: `proposal.txt` with “The synthetic Elm project has a
   proposed budget of 120 dollars. The proposal date is 3 March 2026.” and
   `approval.txt` with “The synthetic Elm project was approved for 90 dollars
   on 8 March 2026.” Upload both. Search for “Elm”, open each source, and ask
   “What amount was approved?” Check the answer against the approval source
   and open its citation. Preserve a content-free pass/fail result.
3. Record two consenting voices reading a wholly fictional one-minute English
   conversation about the Elm proposal and approval. Record a separate Spanish
   conversation with the same facts. Include alternating turns and a pause.
   Save the exact scripts and audio hashes as your local reference. Generated
   voices can check the pipeline but do not establish real-speaker accuracy.
4. Transcribe each language with the primary profile and Nemotron enabled.
   Check original-language text, aligned timestamps, speaker turns, playback,
   and the offered transcript/subtitle exports. Confirm result metadata names
   the actual ASR revision and diarizer. Report word, timing and speaker errors;
   do not mark all quality dimensions passed merely because a job completes.
5. Repeat inference after acquisition with the model networks isolated from
   outbound access. Save model/image identities and the network isolation
   evidence. Missing artifacts must produce a useful failure, not a download.
   Do not infer network isolation solely from an “offline” environment flag.
6. Submit unsupported/synthetic malformed media, confirm a bounded failure,
   then submit valid media successfully. Restart only this disposable node
   using the documented stop/start commands and verify sign-in, retained
   synthetic sources and a new job. Follow [backup and restore](STORAGE_AND_BACKUP.md)
   into a **new** state root; never restore over your working node. Record which
   state is restored and which ephemeral transcription outputs are excluded.

## Acceptance receipt and feedback

Keep private diagnostics locally. Before sharing, inspect content and metadata
and remove credentials, hostnames, identities, paths and source material.
A useful public report contains the exact commit, distribution/CPU/GPU/driver
versions, image digests, model revisions, selected CLI options with generic
paths, elapsed install time, each exercise result, and a minimal independent
synthetic reproduction of any failure. Use the repository's existing issue
and contribution templates; do not upload an environment dump or production logs.

Use the beta hardware form to
[report your hardware](https://github.com/Exculpata/exculpata/issues/new?template=hardware_report.yml),
including results and timings from synthetic workloads. Share only minimal,
sanitized log excerpts from a synthetic reproduction, and check the form's
privacy attestation before submitting. Big-GPU and multi-GPU setups are welcome;
the reference profile is a starting point, not a cap. CPU-only testers can report
no GPU with a count of 0 and VRAM marked not applicable.

| Evidence | Current candidate status |
| --- | --- |
| Deterministic patched wheel, unchanged upstream Python and retained notices | Two equal fresh CPU builds; exact digest in compatibility guide |
| Installer/stager and service regressions | Locally checked; full required CI belongs to the published final commit |
| Fresh combined dependency install/native audio probes | Fresh 155-package environment, dependency check, 11 imports and synthetic audio probes passed; no GPU/model inference |
| Isolated GPU install; browser, review, EN/ES/Nemotron exports | Passed synthetic reference workflow from new application state and verified warm model artifacts; no clean-OS/cold-download claim |
| Offline inference; malformed input; worker restart | Passed synthetic workflow with observed outbound model-network denial |
| Isolated restore and replacement-node boot | Encrypted snapshot/data verification, fresh PostgreSQL import, restored sign-in/access and identical EN/ES exports passed; new-job result belongs to the exact candidate acceptance receipt |
| Actual image identities/SBOM and advisory dispositions | Initial image set inventoried and audited; available companion-service fixes are being qualified. Remaining native-parser and dependency findings require explicit release disposition |
| Independent unfamiliar operator and human language-quality review | Not yet available; explicit beta limitation |
| Tag `v0.1.0-beta.1` | Published [community beta prerelease](https://github.com/Exculpata/exculpata/releases/tag/v0.1.0-beta.1). Checksummed source artifacts and recorded image identities are follow-ups |

A completed reference trial plus reviewed final code/CI and release approval
allows a community beta decision. Broad hardware support, universal model
compatibility, high availability and production recovery certification remain
separate work. Contributors can review and improve this candidate before all
those broader goals are complete.

## Read processing outcomes and complete recovery

Check [transcript processing notices](TRANSCRIPT_PROCESSING_NOTICES.md) before
accepting speaker labels or sharing exports. A usable transcript with failed
speaker attribution is a partial result, not a passed Nemotron workflow.
After the backup file-verification drill, follow
[isolated node recovery](ISOLATED_NODE_RECOVERY.md) for actual database import,
restored sign-in and a new synthetic transcription job.
