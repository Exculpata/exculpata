# Try Exculpata on your hardware

**Use only the synthetic demo data when testing or reporting. Do not load client or case files to produce a report.**

Use a fresh, dedicated test machine or VM with an empty node root. Public reports
must contain generic hardware descriptions and results only: no hostnames,
usernames, employer names, credentials, private paths, or raw environment dumps.
A report is an observation on one configuration, not a certification.

## 1. Choose a test configuration

Start with the [CPU node prerequisites](INSTALL.md#cpu-node-quick-start-ubuntu-2404)
and [model requirements and tiers](MODELS.md). Those pages own the current
resource requirements. The CPU path exercises local accounts and source review
without downloading models. For a GPU/model trial, use the separate
[GPU reference journey](BETA_REFERENCE_INSTALL.md), including acquisition terms,
drivers and toolkit setup, and record the actual model revisions and precision.
The CPU results below do not establish GPU or model compatibility.

For the CPU path, complete steps 1–3 of the installation quick start: install
host packages and Docker, create the dedicated non-root service account and empty
node root, then clone the repository. Run the remaining commands in that checkout
as the service account. Do not install into another node's root.

## 2. Select an immutable release and install

Choose a published tag from [Releases](https://github.com/Exculpata/exculpata/releases).
Replace `<release-tag>` below with that exact tag, leaving the quotes in place.
If the intended beta tag is not published, stop and ask for the reviewed release
reference; do not substitute a moving branch or describe an alpha as a beta.

```sh
EXCULPATA_TEST_TAG='<release-tag>'
git fetch origin "refs/tags/$EXCULPATA_TEST_TAG:refs/tags/$EXCULPATA_TEST_TAG"
git switch --detach "$EXCULPATA_TEST_TAG"
git rev-parse HEAD
./install preflight --root /srv/exculpata --models none --auth local \
  --enable-account-management --server-name localhost --bind-address 127.0.0.1 \
  --storage-profile evaluation
COMPOSE_PARALLEL_LIMIT=1 ./install --root /srv/exculpata --models none --auth local \
  --enable-account-management --server-name localhost --bind-address 127.0.0.1 \
  --storage-profile evaluation
```

Resolve every blocking preflight result before installation. Use the hidden
password prompt; never put passwords in commands or reports. Record elapsed time
and the actual exit result. On failure, stop, record fail or partial and the last
completed step; do not mark subsequent steps passed.

## 3. Run doctor and load Harbor Street

```sh
./install doctor --root /srv/exculpata
```

Record pass/fail/partial, or not-run if installation prevented the check.
Keep detailed diagnostics local. Follow [first run](FIRST_RUN.md) to trust the
local certificate, sign in, and create a synthetic practice matter. In
**Sources → Upload sources**, select the checked-in
[Harbor Street folder](../demo_data/harbor-street), enter a collection name,
confirm the selection, and upload. If the browser is on another machine, copy
only the checked-in synthetic folder from the same release to that browser
machine first. Wait for processing, open **Review uploaded
sources**, inspect a readable source and its citation, then reload. Record
readable, attention and failed counts as actually observed. An unreadable fixture
is a coverage limit to report, not evidence that every source processed.

For a separate disposable preview on a contributor machine, follow
[development setup](DEVELOPMENT.md#set-up-a-development-checkout), then run:

```sh
.venv/bin/python scripts/demo.py
```

Open the printed loopback URL and choose the synthetic sign-in offered there.
This loads Harbor Street automatically into temporary state with no model calls.
It is a preview exercise and does not test the installed node, its authentication,
malware scanner or GPU. Report the preview separately if you use it.

## 4. Run the synthetic evaluation

TODO [#267](https://github.com/Exculpata/exculpata/issues/267): switch to the
workflow scorecard command once its harness is merged and included in the tested
release. Until then, use the existing [claim evaluation](CLAIM_EVALUATION.md)
probes. They use their own frozen synthetic evidence, not the uploaded matter.

Have the host administrator prepare Python 3.12 and the host dependencies from
[development setup](DEVELOPMENT.md#set-up-a-development-checkout). Return to the
service-account session in the same pinned checkout, then:

```sh
make bootstrap
EXCULPATA_EVAL_DIR="$(mktemp -d)"
PYTHONPATH=src .venv/bin/python scripts/evaluate-claims.py probes \
  --output "$EXCULPATA_EVAL_DIR/probes.json"
```

Keep the output locally. Summarize false-acceptance and false-rejection counts
and denominators for both verifier paths from the metrics. Include the fixture
fingerprint and tested commit. Exit zero means the harness completed, not that
quality targets passed. These offline probes do not run a model, measure retrieval,
or qualify the installed node. If an older release lacks the harness, report
not run and its version; do not silently evaluate a different checkout.

## 5. Collect versions and submit results

```sh
git rev-parse HEAD
.venv/bin/python --version
docker --version
docker compose version
```

Record OS release and architecture from the OS settings; generic CPU/core count,
RAM, free disk, GPU model/count and per-GPU VRAM; selected profile; backend/version;
model IDs, exact revisions and quantization. For GPU runs, record driver and CUDA
versions from local diagnostics without copying a full diagnostic dump. Use none,
not run, or unknown where appropriate. Do not invent missing measurements.

Open the [hardware-report form](https://github.com/Exculpata/exculpata/issues/new?template=hardware_report.yml).
Its fields map to the [compatibility table](../COMPATIBILITY.md); the resulting
issue URL supplies the Report column. In Notes include Docker/Compose and applicable
driver/CUDA versions, timings with units, synthetic reproduction steps and what
was not tested. Distinguish installed-node results, preview results, deterministic
probes and any separately authorized real-model evaluation. Inspect every field
and attachment for identifying content and metadata before checking the required
synthetic-only attestation. Submit summaries rather than raw logs or receipts.

## 6. Clean up

Stop the disposable preview with Ctrl-C; its temporary demo state is removed.
For the installed test node, inspect the removal plan first:

```sh
./install uninstall --root /srv/exculpata --dry-run
./install uninstall --root /srv/exculpata
```

Confirm only the dedicated test root when prompted. This removes that node's
containers and networks but preserves its data. To delete synthetic test data,
follow the explicit deletion procedure in
[teardown](INSTALL.md#teardown--uninstall), reviewing the plan and root before
confirming. Do not prune shared Docker resources. After reviewing your report,
delete only the temporary evaluation directory you created and any local synthetic
receipts you no longer need; they are not uploaded automatically.
