# Installation playbook

For a first CPU node, follow the short path below, then
[sign in and create a practice matter](FIRST_RUN.md#cpu-practice-matter).
The later sections are operator reference and recovery guidance.

For a source checkout used to write code and run tests, start with
[contributor setup](../CONTRIBUTING.md#set-up-a-development-checkout). The playbook
below installs an application node; `make bootstrap` prepares a development
environment and does not deploy the application.

Fresh nodes use an `exculpata` service account and default to `/srv/exculpata`,
`config/exculpata.env`, an `exculpata-<hash>` Compose project/image namespace,
and `.exculpata-managed-storage.json`. The administrator prepares the service
account as shown below; the installer runs without root privileges.

Existing nodes keep their names. A retained `config/recordbench.env`,
`.recordbench-managed-storage.json` (including separate managed storage), or
the existing `recordbench` account selects the legacy generation. Use the
node's original `--root` for every command, especially with a custom root or
multiple nodes. P2's `EXCULPATA_*` environment aliases do not identify a renamed
node. Resume/update retain saved paths, project/image namespace and environment
key spelling; an unchanged configuration remains byte-identical. Updates change
only the release coordinate as before. PostgreSQL's user/database remain
`recordbench` in both generations, as does `recordbench_principal_enabled`.
Container-internal paths and existing service credentials also retain their
compatibility names.

`./install doctor --root <node-root>` reports the naming generation.
`--names exculpata` explicitly selects the new generation on a fresh node;
on a detected legacy node it refuses before changing state. This installer does
not migrate or rename a node. Do not create a new service account, move state,
or copy the fresh-node paths below onto an existing installation. The historical
`scripts/recordbench_install.py` entry point continues to invoke the same installer.

The Python distributions are `exculpata` and `exculpata-transcription-v2`.
After `make bootstrap`, check the installed commands from the repository root:

```console
.venv/bin/exculpata --help
.venv/bin/exculpata-workbench --help
.venv/bin/exculpata-retrieval-worker --help
.venv/bin/transcription-v2 --help
```

The `recordbench`, `recordbench-workbench`, `recordbench-retrieval-worker`,
`case-intelligence-workbench`, and `case-review-bench` commands remain available
and print a one-line deprecation notice to stderr. The last command retains its
standalone synthetic review bench; its replacement is
`python -m case_intelligence.review_bench`. `transcription-v2` keeps its name.

For a new local-account node, `./install --auth local --enable-account-management`
enables the **People** account editor and first-administrator setup checklist.
The [browser account playbook](LOCAL_ACCOUNT_BROWSER.md) covers its narrow
writable account mount, existing-node relocation, backup and rollback. Without
the explicit option, local sign-in retains the operator-managed account layout.

## CPU node quick start (Ubuntu 24.04)

This path is for trying the product or doing UI work on a **fresh dedicated
Ubuntu 24.04 x86-64 host**. Use synthetic material. Start with an ordinary
sudo-capable operator account, internet access, 8 CPU cores, 16 GiB RAM and
at least 150 GiB free SSD space. Keep more than the unchanged 100 GiB reserve
free after image builds; 220 GiB of disk was allocated for the acceptance VM.
Docker image storage needs headroom too. These are evaluation targets, not a
supported-release or collection-capacity claim. Other Linux distributions are
unproven; the portable capabilities are listed below.

1. Install the host packages and start Docker:

   ```console
   sudo apt-get update
   sudo apt-get install --yes git ca-certificates python3 openssl docker.io docker-compose-v2
   sudo systemctl enable --now docker
   ```

2. Create the dedicated non-root service account, its real owner-only HOME,
   and an empty node root. Run these once on the fresh host:

   ```console
   sudo groupadd --system exculpata
   sudo useradd --system --gid exculpata --groups docker \
     --home-dir /var/lib/exculpata-home --shell /bin/bash exculpata
   sudo install -d -m 0700 -o exculpata -g exculpata \
     /var/lib/exculpata-home /srv/exculpata
   sudo -iu exculpata
   ```

   Docker-group membership grants privileged engine access. Keep the socket's
   normal permissions. This operating-system account owns the node; it is
   separate from the administrator you will create for browser sign-in.

3. In that new service-account session, check Docker and clone the source:

   ```console
   docker info --format '{{.ServerVersion}}'
   docker compose version
   umask 077
   git clone --single-branch --branch main --no-tags \
     https://github.com/Exculpata/exculpata.git exculpata
   cd exculpata
   ```

4. Check exactly the CPU/local-account choices, then install:

   ```console
   ./install preflight --root /srv/exculpata --models none --auth local \
     --enable-account-management --server-name localhost --bind-address 127.0.0.1
   COMPOSE_PARALLEL_LIMIT=1 ./install --root /srv/exculpata --models none --auth local \
     --enable-account-management --server-name localhost --bind-address 127.0.0.1
   ```

   Resolve any `BLOCK` before installing. Accept the proposed administrator
   username or choose your own, and enter a new password of at least 14
   characters at the hidden prompt. Accept the default matter-storage path.
   Retain that username and password for the browser. The installer builds the
   application, creates its first administrator, obtains malware signatures and
   starts the private companion services. No model weights are selected.
   Keep passwords out of shell arguments, history and receipts.
   The Compose limit serializes shared-image builds; see the reproduced
   [image-build remedy](#shared-image-build-collision).

5. Verify the running node from the same checkout and account:

   ```console
   ./install doctor --root /srv/exculpata
   ```

   Expect exit status 0, **Node diagnostic complete**, and complete running,
   login-reachable, basic-review and selected-capability states. An unselected
   AI capability can remain unavailable with `--models none`. Browser sign-in
   is still unverified. For an interrupted install, follow the printed resume
   command; for startup failures, use [startup troubleshooting](#startup-troubleshooting).

6. Follow [CPU practice matter](FIRST_RUN.md#cpu-practice-matter) to trust the
   loopback certificate, sign in at `https://localhost:8443`, create one matter
   and add a synthetic source. Record the result and elapsed time.

No DNS, LAN listener, organization identity provider, GPU or model setup is
needed for this journey. Staff access and recovery planning belong to the
operator sections below; this isolated practice node is not adoption acceptance.

### Portable package and capability map

Package names below describe the Ubuntu path. Equivalent capabilities on other
distributions have not been accepted as a working node path.

| Where | Ubuntu packages / capability | Purpose |
| --- | --- | --- |
| Node host | `git`, `ca-certificates` | Obtain the public source and verify HTTPS downloads |
| Node host | `python3` (Ubuntu 24.04 supplies 3.12), `openssl` | Run the existing stdlib installer and prepare loopback TLS |
| Node host | `docker.io`, `docker-compose-v2` | Docker Engine, daemon access and Compose v2 builds/services |
| Node host | Dedicated account, writable mode-0700 HOME and storage; working clock/DNS/HTTPS egress | Own state, build images and refresh malware signatures |
| Containers, supplied by `./install` | Application Python dependencies, document/media tools, English/Spanish OCR, PostgreSQL/pgvector, ClamAV, HTTPS gateway | CPU intake, extraction, word search, source review and exports |
| Browser computer | Current browser, trusted loopback certificate; SSH client for a remote node | Sign in and use the UI through loopback HTTPS |

Host OCR/media tools and a Python development environment are for the separate
[code contribution path](../CONTRIBUTING.md#set-up-a-development-checkout);
they are not prerequisites for this containerized node. NVIDIA, learned search,
generated answers and transcription are outside this CPU path.

## Hardware and capability profiles

Exculpata targets a modern x86-64 Linux host with Docker Engine and Docker
Compose v2. The figures below are starting targets for alpha evaluation, not
validated minimums:

| Intended use | CPU | RAM | NVIDIA GPU | Free SSD before matter data |
| --- | ---: | ---: | --- | ---: |
| CPU evaluation | 8 cores | 16 GB | none | 150 GB |
| Local document review | 12 cores | 32 GB | 1 GPU with 16 GB VRAM | 200 GB |
| Every-source checks and media | 16 cores | 64 GB | 1 GPU with 24 GB VRAM | 300 GB |

These targets do not guarantee collection size, speed, or concurrency. More
sources, longer recordings, and more simultaneous users need more capacity.
Consult [release readiness](RELEASE_READINESS.md) before treating a
configuration as supported. Free-space targets cover application images,
models, temporary processing, and the configured safety reserve; matter
contents require additional local or NAS capacity.

The installer exposes four capability profiles:

| CLI value | What it enables |
| --- | --- |
| `none` | CPU evaluation with intake, extraction, OCR, word search, source review, and exports |
| `review` | learned retrieval, reranking, and cited local generation |
| `transcription` | media transcription without the review-model services |
| `all` | document review plus transcription and optional diarization |

GPU profiles require NVIDIA compute capability 7.5 or newer and the NVIDIA
Container Toolkit. The intended deployment model supports one suitable GPU,
with additional GPUs separating generation, transcription, and retrieval work.
Each configuration still needs deployment validation. Advanced placement
controls are documented in [Models](MODELS.md).

The installer never opens a public port by default. Its initial listener is
`127.0.0.1:8443`; a LAN bind requires an explicit address and an existing
organization-trusted certificate.

## Before installation

1. Choose a dedicated node state root. Do not use `/`, a home directory, or the
   root of a shared NAS export.
2. Choose matter storage. A host-mounted NAS path is supported when it is stable,
   writable by the Exculpata UID, and dedicated to this installation.
3. Decide between local accounts, OIDC, or Kerberos/Windows Integrated
   Authentication.
4. Obtain DNS and a trusted TLS certificate for staff access.
5. Choose transcription modules. Ungated ASR and English/Spanish alignment can
   be staged independently. `--enable-diarization` selects ungated Nemotron;
   it needs no Hugging Face account or token. The optional legacy
   `--diarization-backend community-1` still requires upstream access approval.
6. Decide where the encrypted restic repository and independent recovery key
   will live.

## Inspect prerequisites without installing

Run this first as the dedicated non-root service account on the intended Linux
host. It does not prompt, create directories, write probe files, download models,
start containers, or change permissions:

```bash
./install preflight --root /srv/exculpata --models none --no-color
./install preflight --root /srv/exculpata --models review --json
```

Supply the same `--storage-root`, `--server-name`, `--bind-address`, TLS, model
and GPU-selection options you intend to use for installation. Quote paths
containing spaces.
Omitted options select `/srv/exculpata`, storage beneath it, CPU evaluation,
and loopback HTTPS. Interactive installation collects the hostname before running
these checks, so invalid answers also stop before creating node state. `none`
enables intake, extraction, OCR, word search, direct source review and exports. `review` adds learned search and cited generated
answers; `transcription` adds recording transcription; `all` selects both AI
workflows. CPU evaluation does not require NVIDIA hardware.

Each checklist row reports what was observed, which task it enables, whether it
blocks installation, and a next action. Resolve every `BLOCK` and rerun the same
command. `NOTE` rows are capacity planning advice. Exit status is 0 when no
blocking prerequisite fails, 1 otherwise. An unsupported OS/architecture stops
before any runtime or storage probe. The Linux launcher currently targets x86-64;
this is its deployment contract, independent of the contributor's computer.

`--json` emits only one versioned document with `schema_version`, `ready`, and
`checks`. Every check has `name`, `state` (`pass`, `fail`, or `unknown`),
`observed`, `required_capability`, `blocking`, and `remedy`. An unknown blocking
check also prevents readiness. Consumers should use these fields, tolerate new
check names, and reject unsupported schema versions. JSON output excludes paths,
account names, device names and raw Docker/driver output.

For missing prerequisites:

- Install [Docker Engine](https://docs.docker.com/engine/install/) and the
  [Compose plugin](https://docs.docker.com/compose/install/linux/) for the host
  distribution. Arrange approved service-account engine access; Docker access is
  privileged. Never make its socket world-writable to pass a check.
- Prepare a real owner-only service-account HOME as described below before forge.
  Have an administrator create only the dedicated node and matter-storage paths
  and assign them to the non-root service account. The account's primary group
  must also be non-root. Do not use a home directory, symlink, or shared export
  root. Existing selected directories must belong to that account. A first-install
  node root must be empty. `--resume` permits a nonempty root only when it has a
  valid Exculpata installation record and available release capsule; it does
  not claim unrelated directories. A missing or empty root still follows fresh
  installation prerequisites when `--resume` is supplied.
  Both preflight and installation require absolute storage paths without control
  characters; invalid paths are rejected before creating state. Matter storage
  cannot equal or contain the node root, or occupy its configuration, secrets,
  runtime, transcription, state, model, TLS, account or release paths. This also
  excludes `compose.env` and `installation.json`, including their descendants.
  The default nested `matter-storage` and custom children outside those reserved
  paths remain supported, as do separate dedicated storage directories.
- For unattended local installation, supply `--password-stdin`. Preflight checks
  the input choice and administrator username/display-name syntax without reading
  a password. Display names use the same NFC normalization and Unicode control
  and formatting-character rejection as account creation.
  Unattended OIDC and Kerberos installation require readable regular credential
  source files without symbolic links, limited to 1 MiB. OIDC preflight performs
  a bounded read, decodes UTF-8, strips trailing CR/LF, and requires 16–4,096
  characters with no embedded CR/LF or NUL, matching the runtime. A newline after
  a valid 4,096-character secret is accepted; line endings cannot pad a short
  secret into a valid one. Secret values and source paths are not reported.
  Kerberos preflight checks nonempty file metadata without reading its contents.
  Kerberos also requires the host SSSD and Kerberos paths.
  Non-secret OIDC settings require an HTTPS issuer, non-loopback external server
  name, and a nonempty client ID of at most 512 characters. OIDC group names can
  contain at most 256 characters. Kerberos requires a dotted realm, valid group
  names (letters, digits, spaces, dots, underscores, dashes, @ or backslashes,
  starting with a letter or digit, at most 255 characters), and at least one
  allowed or administrator group. Both modes allow up to 100 allowed groups and
  50 administrator groups, reject NUL, carriage returns and newlines in configuration fields,
  and validate the same non-secret settings accepted by the runtime.
  These checks do not verify credentials or a working identity-provider exchange.
- Leave more than the installer's 100 GiB storage safety reserve free on the
  selected filesystems. The larger profile targets above are advisory alpha
  planning figures, not validated minimums. Container-engine image storage may
  be on a different filesystem and needs separate capacity planning.
- Retain loopback HTTPS for evaluation, or supply a readable certificate/key
  pair trusted by the organization for explicit LAN access. Preflight checks the
  choice, IP bind-address syntax, server-name syntax and file access. The bind
  address must be an IPv4 or unbracketed, unscoped IPv6 literal; specify the port
  separately with `--https-port`. Supplied certificate and key paths must not
  contain control characters. Relative TLS paths are made absolute from the
  launch directory before checking and saving them, so the staged release uses
  the same files; symbolic-link inputs remain rejected. Preflight does not
  certify the pair's matching keys, identity,
  expiry or trust chain. Verify those before staff access.
- For AI tasks, install the NVIDIA driver and
  [Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
  The checklist checks registered Docker runtime support and the selected model
  plan against currently reported GPU capability and free memory. It also
  validates supported transcription languages and the diarization/profile combination.
  Unattended diarization staging also requires both `--accept-model-terms` and
  `--hf-token-stdin` before any installation writes. Preflight checks those
  choices without reading, validating or printing the token. It neither
  pulls a test image nor proves container/device or offline model readiness.

Directory writability is a metadata/access check with no write attempt; NAS
ACLs, quotas, read-only mounts and later capacity changes can still prevent
installation. A passing preflight is permission to attempt installation, not a
successful install or confidential-workload acceptance. The `doctor` and
synthetic acceptance steps below remain necessary.

## Service account HOME

Compose/build tools need a writable HOME even when application data lives under
`/srv`. A system account created with `/nonexistent` can pass storage checks and
then fail forge with `mkdir /nonexistent: permission denied`. Preflight now blocks
an unset, relative, missing, non-directory, symlink, foreign-owned, non-0700 or
inaccessible HOME without writing client state. Parents must also be accessible,
root/service-owned, free of symlinks and protected from replacement (the existing
trusted sticky-directory exception applies). This checks the launch environment;
set the account database home too so future login sessions retain the correction.

For a new account, an administrator can run this Debian-style example (adapt
account creation to the distribution; do not recreate an existing account):

```bash
sudo groupadd --system exculpata
sudo useradd --system --gid exculpata --home-dir /var/lib/exculpata-home \
  --shell /bin/bash exculpata
sudo install -d -m 0700 -o exculpata -g exculpata /var/lib/exculpata-home
sudo install -d -m 0700 -o exculpata -g exculpata /srv/exculpata
```

For an existing system account, retain its account name and home; repair only
the ownership and permissions of that existing home when needed.
Use a usable shell for the operator-driven installation session. Keep HOME
separate from node/matter storage. Arrange approved Docker access, then start a
fresh login session (for example `sudo -iu exculpata`) so HOME and supplementary
groups take effect. Run `docker info` and `docker compose version` as that account
before preflight. Membership in `docker`, where used, grants privileged engine
access; never loosen socket permissions. HOME preflight checks metadata/access,
not quotas or a successful client write.

## Atypical Docker hosts

A dedicated Linux host with its distribution-supported Docker service is the
normal starting point. Containerized evaluation hosts may need administrator
setup before Exculpata preflight:

- **No systemd PID 1:** `systemctl` cannot start Docker. Follow Docker's
  [manual daemon instructions](https://docs.docker.com/engine/daemon/start/)
  (`sudo dockerd` in a supervised administrator session). The host administrator
  owns daemon supervision and restart persistence. A failed `docker-access` BLOCK
  now points here; absence of systemd alone does not block a usable engine.
- **Nested overlay fails:** inspect `docker info` and daemon logs. Prefer
  `fuse-overlayfs` over `vfs` where the outer host, FUSE access and installed
  engine support it. The administrator must install/configure the compatible
  driver before building. Preflight emits a nonblocking storage-driver NOTE for
  `vfs` or an unavailable driver probe. Docker's
  [VFS documentation](https://docs.docker.com/engine/storage/drivers/vfs-driver/)
  explains its full layer copies and extra disk consumption. Driver changes need
  planned downtime and preservation of existing images/volumes; do not switch a
  shared daemon or delete its data directory as an install retry.
- **Bridge connectivity fails:** if gateway cannot reach `app:8786`, ask the host
  administrator to inspect forwarding and bridge/firewall policy in the relevant
  network namespace. Some nested hosts need `net.ipv4.ip_forward=1` and
  `net.bridge.bridge-nf-call-iptables=0` (and, when applicable,
  `net.bridge.bridge-nf-call-ip6tables=0`). These are host-specific remedies, not
  universal defaults: disabling bridge netfilter changes firewall processing.
  The administrator must reconcile them with the outer host policy, verify
  inter-container connectivity, and persist only the approved settings.

Exculpata does not install Docker, change drivers/sysctls, or certify nested
host networking. Successful engine access is not a container-connectivity test.
Keep the gateway on loopback HTTPS and companion services private.

## Startup troubleshooting

### Shared-image build collision

On the fresh Ubuntu 24.04 Docker/Compose path, parallel builds of `app` and
`account-admin` reproduced `failed to solve: image ... already exists`. They
publish the same application image. Use `COMPOSE_PARALLEL_LIMIT=1` for the
installation command, as shown in the quick start. If already interrupted,
retain the node and run from the same checkout and service-account session:

```console
COMPOSE_PARALLEL_LIMIT=1 ./install install --root /srv/exculpata --resume
./install doctor --root /srv/exculpata
```

Resume asks for the initial password again if the account was not yet created.
This remedy does not require image deletion, a new installer or node reset.

### Runtime readiness

The clean-host B-lite run also reproduced an unstyled workspace with an inert
upload control: absolute static URLs used the gateway's internal HTTP origin
and omitted port 8443. The shared workspace now emits static asset paths so
the browser retains its HTTPS origin and port. Use this corrected revision;
do not allow mixed content or broaden trusted proxy headers to work around it.

A passing preflight is a point-in-time check. A completed CPU clean-host receipt
on `ccba441` passed install/resume, doctor, synthetic team setup and encrypted
backup/isolated restore after operator host corrections. It exposed these setup
frictions; it does not establish GPU, transcription, supported-release or full
browser-suite acceptance.

Use the installed node's Compose coordinates, not an unrelated checkout's default
project. The release path is recorded in `installation.json`; for diagnostics:

```bash
release_path=$(python3 -c 'import json; print(json.load(open("/srv/exculpata/installation.json"))["release_path"])')
docker compose --env-file /srv/exculpata/compose.env \
  -f "$release_path/compose.yaml" ps -a
docker compose --env-file /srv/exculpata/compose.env \
  -f "$release_path/compose.yaml" logs --tail 100 clamav-updater clamav app gateway
```

Keep logs private; report only content-free symptoms. Use the node's additional
Compose overlays/profiles when changing service state. The diagnostic commands
above only read the base services.

**ClamAV signatures:** the updater healthcheck requires a `daily.*` file larger than one byte
in its shared `/var/lib/clamav` volume modified within 4,320 minutes (72 hours).
Missing/stale daily signatures block scanner startup and can block app/gateway
before the HTTP health loop is reachable. Startup-command failures and health
timeouts now point to these checks. Inspect updater logs for DNS, TLS, timeouts,
403/429, permissions and signature validation errors; also confirm clamd loads
its databases. Do not touch file timestamps or disable scanning to pass health.

FreshClam sends its ClamAV User-Agent. A generic curl/wget download is not an
equivalent test; changing only the UA does not establish a supported updater.
Datacenter/shared egress may encounter CDN blocks or rate limits. Follow the
[FreshClam error guidance](https://docs.clamav.net/faq/faq-freshclam.html): check
engine support, DNS/TLS and proxy behavior, honor cooldowns, and avoid tight retry
loops. For managed mirroring use the upstream
[FreshClam/CVDUpdate mirror procedure](https://docs.clamav.net/appendix/CvdPrivateMirror.html).
Proxy/mirror configuration and persistent updater access remain operator work
outside the portable tree; keep scanner/model networks private. Confirm a real
signature refresh and healthy scanner before retrying acceptance.

**Build erodes reserve:** 112 GiB free can pass the 100 GiB check but fall below
reserve during image construction, especially with `vfs`. The CPU 150 GiB target
is advisory, not a guarantee. Budget peak image layers/build cache in addition to
models, data and the protected reserve on both the engine and node/matter
filesystems. Check `df -h /srv/exculpata` and the selected matter path after
build; inspect `docker system df` and the engine host's Docker Root Dir filesystem.
A remote engine's disk is not measured by local `df`. Runtime
`storage.reserve_satisfied=false` means restore capacity above the configured
reserve; repeated rebuilds can worsen it. Add capacity or have the administrator
remove only identified disposable data/cache, preserving rollback images and
volumes. Do not lower the reserve or run indiscriminate prune commands to pass.

After correcting the cause, run `./install doctor --root /srv/exculpata` if
services are already running, or the printed `./install install --root
/srv/exculpata --resume` command for interrupted startup, then doctor and the
synthetic acceptance steps. Health diagnoses do not repair host configuration.

## Interactive installation

The launcher collects the selected identity mode, local administrator names or
provider configuration, and any required Kerberos keytab path before preflight.
It validates those exact choices and reuses them during configuration without
asking again. These initial questions collect no passwords or tokens. A supplied
OIDC file is validated during preflight; password and token entry remain in the
later provisioning steps. Standalone
`preflight` remains non-prompting and checks the options supplied on its command line.
For a new browser-managed local-account node, include both `--auth local` and
`--enable-account-management` in that preflight command. With `--resume`, preflight
restores the existing node's saved authentication mode, account mount, storage,
model selection and offline staging status before checking prerequisites. This
includes relocated managed-account directories; an old account file in `secrets`
does not replace the canonical managed account file. Supplied flags do not change
the saved account mode or paths. Invalid saved coordinates are reported as a
blocking checklist item, including with `--json`.
For local resume, the canonical account store is checked before Compose builds
or service changes: safe no-follow directory traversal, service ownership, mode 0600,
bounded valid account JSON and password-hash structure, and an enabled
administrator. Browser management additionally requires its dedicated mode-0700
directory and a version-2 store; migrate a version-1 store explicitly with a
backup before enabling it. An unsafe or malformed existing store is a blocking
saved-node check. An absent account file during interrupted setup still requires
the initial administrator and password input; missing saved mount directories
remain blocked. These checks do not modify accounts, read a password, or report
account contents.

OIDC and Kerberos resume likewise validate the retained provider choices before
Compose builds or service changes, including in a dry run. New provider flags do
not override the saved application environment (`config/exculpata.env` on new
nodes, `config/recordbench.env` on legacy nodes) or replace canonical credentials.
OIDC checks the saved issuer, client, groups, scopes, claim names, token method,
matching external origin and canonical secret reference, then validates the
owned mode-0600 `secrets/oidc-client-secret` using the same UTF-8 rules as a fresh
installation. Kerberos checks the saved realm, admission groups/principals,
matching HTTP service identity, canonical proxy-secret reference, a nonempty
owned mode-0600 `secrets/recordbench.keytab`, and the owned mode-0600 proxy secret's
32–256-character ASCII syntax. Both refuse symbolic-link credential files and
keep their contents out of output. Missing SSSD or Kerberos configuration paths
block resume; these presence checks do not establish a working domain exchange.
The proxy still checks that the keytab contains its configured HTTP principal
when it starts.

```bash
./install
```

Run the installer as a dedicated, non-root service account. If `/srv` or a NAS
mount requires administrative setup, create the narrow node and storage paths,
assign them to that account, then run `./install` as the account. The installer
refuses to silently record UID/GID 0 for application containers.

The phases are deliberately explicit:

1. host and GPU preflight;
2. state/storage boundary creation;
3. allowlisted release-capsule hashing and staging;
4. authentication and TLS configuration;
5. versioned container build;
6. storage and administrator initialization;
7. pinned model staging and offline manifest sealing;
8. launch and health acceptance.

Health and sign-in acceptance connect to the configured HTTPS bind address while
retaining the configured hostname in the HTTP `Host` header. IPv4 and IPv6
wildcard binds use `127.0.0.1` and `::1` respectively for these local probes;
concrete IPv4/IPv6 bindings use their selected address. These checks do not prove
remote browser reachability, certificate trust, or a completed user sign-in.

Use `--prepare-only` to build and configure without starting services. Use
`--dry-run --no-color` to inspect the exact command sequence without writes.
Dry-run prerequisite discovery is real and read-only: it rejects unsupported
hosts, missing runtime access, unsafe storage/TLS choices, and missing,
incompatible, undersized or currently occupied selected GPU hardware. Use
`preflight` to inspect prerequisites without going through configuration prompts.

## Unattended local-account example

Passwords and Hugging Face tokens are read only from standard input, never a
command argument. Because both are needed, use separate controlled staging
steps rather than concatenating secrets into a shell history. The interactive
installer is recommended for the initial release. Automation should invoke the
same commands from a secret manager and inspect exit codes.

## Model staging

The model vault uses exact revisions from `config/models.json`. It downloads to
the selected model root, hashes the transcription artifacts, writes the
offline approval manifest, and discards the token variable. Runtime AI
containers use offline flags and networks without external egress.

If a gated download fails, confirm the operator account accepted the model’s
terms, create a read-only Hugging Face token, and rerun preparation. Never put a
hub token in `.env`, Compose YAML, service environment, logs, or Git.

Transcription defaults to ungated ASR plus English alignment. Add Spanish with
`--transcription-languages en,es`. Add speaker clustering only with
`--enable-diarization`; new installations stage the pinned Nemotron model
without a token or access-approval prompt. To retain Community-1, explicitly use
`--diarization-backend community-1 --accept-model-terms`; only that gated backend
requests a one-time read-only hub token. Resume preserves the saved backend and
never silently converts an existing installation. Whisper translation uses the staged ASR artifact
and does not add a separate model download. A deployment that omits diarization
still produces transcripts and timestamps, with speaker separation explicitly
reported as unavailable.

## Acceptance

Run:

```bash
./install doctor --root /srv/exculpata
```

Doctor compares the installation's expected authentication mode with the saved
configuration and the running app's protected diagnostic. Missing diagnostics
or disagreement cannot pass health acceptance; see
[installed authentication diagnostics](AUTHENTICATION_DIAGNOSTICS.md).

Then use only synthetic fixtures to verify login, matter creation, document
OCR, search, cited answers, video playback, transcription/diarization, exports,
matter closure, and cleanup. Validate GPU memory and concurrent workloads
outside Exculpata before allowing confidential material.

## Kerberos installations

Join the Linux host to the organization domain with SSSD first. Create an HTTP
service principal and keytab for the final Exculpata hostname, then supply the
keytab to the installer. The Kerberos Compose overlay mounts only the host NSS
socket and `nsswitch.conf` into the app so group lookup behaves like the joined
host; the authentication proxy receives the keytab and strips the browser’s
Negotiate token before forwarding an authenticated principal plus a shared
secret. Follow [Authentication](AUTHENTICATION.md) exactly.

## Upgrade

The installer stages an immutable, content-addressed release capsule under the
node root and gives every locally built image a release-specific tag. It does
not fetch or merge source code: review and update the clone first, then run:

```bash
./install update --root /srv/exculpata
```

The capsule hash and copied files use the same generated-file exclusions:
`__pycache__`, `*.pyc`, `.pytest_cache`, `*.egg-info` and `build`. Running
contributor tests or building Python packages must not change release identity; changes to included source files do. The source hash
identifies capsule content, not the installed image or runtime dependencies.

An update requires a newly successful bundled encrypted backup unless no
bundled backup is configured and the operator supplies `--no-backup`. Operators
using their own backup method can use that flag after verifying their external
recovery copy against the [backup consistency requirements](STORAGE_AND_BACKUP.md).
It skips the bundled requirement; it does not validate an external backup or
ignore a configured bundled backup's failed/deferred receipt.
Resume and update validate the saved model profile, selected GPU devices,
generator precision, memory reservation and transcription free-memory threshold.
They do not substitute an automatically chosen GPU or a smaller model to pass
these checks. Resume requires sufficient current free memory before provisioning.
After a prior boot reached its provisioning seal but failed during startup or
health checks, its model services may still hold GPU memory. A normal resume of
that prepared GPU node first checks physical capacity and saved compatibility,
then observes this node's enabled running services. If a selected GPU model
service is running, resume stops only that observed service set with a 120-second
grace period and checks actual free memory before continuing. If no model service
is running, actual free memory must still pass before provisioning. A service
probe failure blocks the resume. A failure after a stop attempt restarts only
the previously observed services and explicitly leaves health unconfirmed; no
volumes are deleted. Fresh installs, resumes before the provisioning seal, and
`--prepare-only` resumes retain strict initial free-memory admission. A prepared
GPU resume dry run reports the deferred actual-free check without probing service
state, stopping services, or running Compose mutations.
Update first checks compatibility and physical total capacity before backup or
release commands; its running models may still occupy their existing allocation. Saved `bfloat16` precision requires compute capability 8.0 or newer on
every selected generator GPU, even when a new automatic plan could use `half`.
A completed offline resume or update does not require a new diarization
token; an unfinished resume that must stage models still checks its staging
options before continuing.

The new capsule is built without overwriting the prior image set while the old
runtime remains available. After a successful build and Compose configuration
validation, GPU updates stop only this node's Compose project, allow up to 120
seconds for graceful shutdown, and check actual free GPU memory again before
starting replacement services. This maintenance interruption releases the node's
own model allocation without treating other workloads' memory as available.
Competing allocations, an unavailable GPU probe, or a failed stop prevent the
replacement from starting and invoke the existing rollback. No volumes are deleted.
An update dry run previews this sequence using physical capacity; it never stops
the node and explicitly defers the actual free-memory check.

If replacement admission or startup acceptance fails, configuration is returned
to the previous release and its images are relaunched. A rollback that cannot
restore health is reported separately for operator recovery. Old releases and
images are retained for deliberate operator cleanup; the updater never prunes them.


Before fresh managed-storage initialization, the reserved names `matters`,
`.matter-purging`, and `ingestion-staging` must be absent, including broken links.
Existing initialized storage must retain its valid Exculpata marker and three
writable directories on the same filesystem. Preflight inspects that metadata
without writing or probing hardlinks; it does not delete or rename collisions.
Choose another dedicated storage root when unrelated data already uses those names.

Storage preflight requires the nearest existing creation directory to belong to
the service account and deny group/other writes. Every parent must belong to
root or that service account and prevent replacement of its child. The service
account also needs read and search access to the existing creation directory and
every ancestor, matching the portable descriptor walk used during preparation.
Execute-only ancestors are rejected by preflight; select an accessible dedicated
path or arrange the required service-account access before retrying. A trusted
sticky system directory may contain an existing private service-owned directory;
it is never accepted as the creation directory itself. Root-owned protected
parents such as `/srv` are supported when the operator first creates the
service-owned directory beneath them. Non-sticky shared writable ancestors and
parents owned by another non-root account are refused before installation.
Installation creates each missing directory component with mode `0700`, even
when the caller's umask removes owner permissions. Directory creation runs in a
short-lived isolated child that inherits only the held parent directory
descriptor and sets its own umask; the installer never changes the calling
process's umask. It rechecks ownership and replacement protection
while walking held directory descriptors, refuses symbolic links, and applies
owner-only permissions to the selected node and storage directories without
changing existing ancestor modes. If those paths change after preflight,
preparation stops before following an unsafe replacement. Only newly created
protected directories may remain after such a failure; inspect them before
resuming.

Synthetic regression tests cover invalid server names stopping before state
creation, nested node and separate matter-storage paths under umasks `000`,
`0700` and `0777` as a non-root account, and
concurrent symlink, writable-directory and foreign-owner replacements. The
backup suite also round-trips synthetic control and managed SQLite stores from
these prepared nested directories into a clean restore target. That regression
uses simulated service/backup commands; an operator still needs the real encrypted
backup and isolated recovery acceptance in [Storage and backup](STORAGE_AND_BACKUP.md).

Retained OIDC CA configuration must point to a readable valid certificate in the
canonical read-only secrets mount. Resume checks that file before provisioning;
restore a missing or invalid CA rather than replacing the saved provider options.
For a local installation that still needs its first account, installation reads
and validates password input after read-only preflight and before any node writes
or provisioning commands. Standalone preflight and dry runs do not consume it.
The one-use input is cleared at account initialization and on error.

A successful doctor check recovers prepared-phase progress from a matching
provisioning seal even if the current progress receipt is absent or from an older
release. A successful update records preparation under its new seal and prints
the same handoff. A healthy, sealed node is not directed to rebuild with resume
merely because its progress receipt predates the current release. Browser sign-in
remains a separate unverified step.

Resume never initializes a replacement account store when a provisioning seal exists, including a seal from an older release or a damaged seal. Restore the canonical account backup first. Retained application account-file and management-root settings must exactly match the selected managed or read-only account layout before any Compose command runs.

## Dependency reproducibility

See [runtime dependency locks](DEPENDENCIES.md) for the pinned Python environments,
verified tokenizer staging, and remaining whole-image qualification requirements.
These preparations do not change the development-alpha support status.
