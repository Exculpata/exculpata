# Linux CPU contributor node (B-lite) — 2026-09-19

**Clean-node journey: passed. Quality: passed.** The corrected documented
path reached first-admin sign-in and a usable synthetic source in **313.26
seconds (5 minutes 13 seconds)**, from host-package setup through browser reload.
The bounded B-lite substep is Done; no further product feature is selected.

## Public image and candidate

- Ubuntu Server 24.04.5 LTS, x86-64, cloud-image serial `20260911`:
  [public image](https://cloud-images.ubuntu.com/noble/20260911/noble-server-cloudimg-amd64.img).
  SHA-256 checked before boot:
  `612b2c0cc1bc413a6cb8c38fd611794caf0f2b436c50013d8b3794db12ad7354`.
- Fresh QEMU/KVM disk: eight virtual CPUs, 16 GiB RAM, 220 GiB virtual SSD.
  Cloud-init supplied only an ordinary sudo-capable evaluator and disposable
  SSH access. No package cache, Docker images or node state were copied in.
- Public source base: `7c8b5c02d66857ad3a96e79a5c82324f87bdcc7b`, with the
  B-lite documentation and shared-template correction applied before install.
  The only runtime source change is `src/case_intelligence/templates/workbench_base.html`,
  SHA-256 `8296375201daf9a1193266c1f1447ea63d2fe8a8b894993e935c406487c70169`.
  Its installed capsule was `0.1.0-alpha.2-a5d0fce3ca5e`; the installed template
  hash independently matched the candidate. The reviewed implementation commit is
  `d232e1ec66ddcfd38d38ff4a553bb80d3f639d02`; subsequent completion edits only
  update the documentation and leave that installed runtime unchanged.
- Ubuntu Docker Engine `29.1.3`, Compose `2.40.3+ds1-0ubuntu1~24.04.1`.
  No GPU, models, transcription or organization identity provider was selected.
- The service account had a real mode-0700 HOME and owned the dedicated empty
  node root. Installation ran as that non-root account with Docker access.
  Post-install free space was **206.1 GiB**, above the unchanged 100 GiB reserve.

VM disks, access keys, account credentials, certificates, raw logs and browser
artifacts remained outside Git. All matter/source content was synthetic.

## Commands and timings

The host-package, service-account and clone commands were exactly the ordered
[INSTALL quick start](INSTALL.md#cpu-node-quick-start-ubuntu-2404). Candidate
selection then checked out the public base above and applied this patch; that
step selected the unpublished candidate, not additional node prerequisites.

As the dedicated service account, in its checkout:

```console
./install preflight --root /srv/exculpata --models none --auth local \
  --enable-account-management --server-name localhost --bind-address 127.0.0.1
COMPOSE_PARALLEL_LIMIT=1 ./install --root /srv/exculpata --models none --auth local \
  --enable-account-management --server-name localhost --bind-address 127.0.0.1
./install doctor --root /srv/exculpata
```

The harness supplied the documented default matter-storage path and first-admin
names to interactive prompts, and a fresh random password to the hidden password
and confirmation prompts. `--no-color` affected logging only. No password was
passed in arguments or copied into this receipt.

| Phase | Wall time | Outcome |
| --- | ---: | --- |
| Host packages, Docker, account, clone and preflight | 43.93 s | Exit 0; all blocking prerequisites passed |
| `./install`, with serialized Compose builds | 257.17 s | Exit 0; first admin created and node ready |
| `./install doctor` | 1.32 s | Exit 0; Node diagnostic complete |
| Browser certificate import | 0.39 s | Explicit trust in the evaluator's fresh NSS store |
| Browser sign-in through source reload | 5.53 s | All seven browser assertions passed |

The overall interval was **23:39:09.875–23:44:23.138 UTC** and includes candidate
transfer and orchestration between phases. Browser-only runtime packages and the
fresh Selenium environment took 66.94 s in parallel with the install, under the
separate evaluator account. They were not service-account or node prerequisites.
The final fresh-node run needed no resume, image removal or application repair.

## Actual browser result

Chrome for Testing and matching driver `153.0.8010.36`, the repository's pinned
Linux browser, exercised the installed gateway at `https://localhost:8443`.
The generated public certificate was explicitly imported with the FIRST_RUN
NSS commands. `acceptInsecureCerts` stayed false; no insecure-certificate switch
was used. Browser automation scrolled to and clicked the real controls.

1. The HTTPS sign-in page loaded with certificate validation enabled.
2. The installer-created `exculpata.admin` signed in and reached `/admin/setup`.
3. The authenticated session cookie was Secure and HttpOnly.
4. **Create matter** created `Synthetic CPU practice` through its browser form.
5. **Sources → Upload sources**, collection `Synthetic practice sources`, file
   selection preview and **Upload 1 ready file** submitted `synthetic-practice.txt`.
6. **Review uploaded sources** opened the processed source with the documented
   fictional Aurora text visible.
7. Reload preserved the source and visible text.

Doctor separately confirmed prepared, running, login-reachable, basic-review
and selected-capability phases complete, including required scanner readiness.
This is a real installed local account and source, not a preview identity or
stubbed source result. Generated answers and transcription remain unselected.

## Friction reproduced before the accepted run

The first disposable node hit a shared-image publication race while Compose
built `app` and `account-admin` concurrently. Its initial install failed after
152.23 s. The documented `COMPOSE_PARALLEL_LIMIT=1` resume succeeded in 78.86 s.
A second fresh disk confirmed a serialized cold install passed in 251.09 s.
The accepted run above started again from a fresh disk with that setting.

The first browser attempt could sign in and create a matter, but workspace
asset URLs used `http://localhost` and lost the external HTTPS port. CSS and
JavaScript failed to load, blocking the upload controls. The approved six-line
shared-template fix emits static paths, retaining the browser's origin without
changing trusted proxy headers. The synthetic internal-HTTP-hop regression
fails on the original template and passes on the correction.

Exploratory browser runs also clarified the actual sequence: enter a collection
name, confirm the selection, then choose **Review uploaded sources** after
processing. FIRST_RUN includes those steps. These exploratory runs are separate
from the uninterrupted accepted run above.

## Quality and bounds

Focused gateway-asset, local-account and onboarding tests: **35 passed**.
Bundled transcription suite: **194 passed**. Compilation, Compose validation
and candidate-only tree/reachable-history publication inspection passed.
An exploratory full local run started before the asset correction was complete:
**3,170 passed, 9 skipped, one SQLite OperationalError** in the unchanged
`test_legacy_job_mutations_cannot_bypass_full_text_fences[0-checkpoint]`.
The immediate isolated rerun passed, then the complete fence file passed all
**60 tests**. No later-19 code or tests were changed. This exploratory run does
not establish full-suite acceptance for the final candidate.

The first hosted run tested the complete candidate and caught an existing
branding assertion tied to the old favicon template expression: **3,171 passed,
9 skipped, one failed**. The expectation now follows the approved path-only
asset correction; no additional runtime or visual change was needed. Hosted
Quality on that corrected candidate passed on 2026-09-20 UTC.

All six jobs passed in [Quality run 35478490846](https://github.com/neilofneils404/exculpata-oss/actions/runs/35478490846)
for `d232e1ec66ddcfd38d38ff4a553bb80d3f639d02`: application, PostgreSQL
integration, synthetic-browser, transcription, deployment-contract and
secret-scan. The full application suite passed with **3,172 passed and 9
expected skips**. The installed pre-push guard inspected the complete outgoing
history; independent secret scanning and local review also passed. Hosted review
was not required under PUBLIC_ALPHA. The completion commit changes documentation
only; its final PR checks must also pass before landing.

No new installer was created. Code-only contribution retains Make. This receipt
does not establish other distributions, GPU/transcription, staff/LAN access,
two-user isolation, backup/restore adoption acceptance or a supported release.
Mac/#34, visual/slice 21, later-19 and #87 were not changed.
