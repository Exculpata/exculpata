# Hardware compatibility reports

These are bounded observations, not a supported-hardware list or a promise of
installation, model quality, or suitability for confidential workloads.
[Try it on your hardware](docs/TRY_IT_ON_YOUR_HARDWARE.md) and submit a
[synthetic hardware report](https://github.com/Exculpata/exculpata/issues/new?template=hardware_report.yml).

**Use only the synthetic demo data when testing or reporting. Do not load client or case files to produce a report.**

## Reference evidence

The CPU row summarizes the dated [clean-node receipt](docs/LINUX_CPU_NODE_ACCEPTANCE_2026-09-19.md).
The GPU row summarizes the [candidate reference journey](docs/BETA_REFERENCE_INSTALL.md).
The GPU resource figures are qualification targets, not measurements of the trial
hardware. Unrecorded values stay unrecorded; neither receipt ran the current claim
probe scorecard. The seed rows link to the tester-program tracking issue because
there is no separate hardware-report issue for these historical receipts.
Replace that link only when a corresponding report exists.

| Hardware (generic) | OS | Backend | Model + quantization | Profile | Exculpata version | Install | doctor | Eval summary | Report | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| VM, 8 virtual CPUs, 16 GiB RAM, 220 GiB virtual SSD, no GPU | Ubuntu Server 24.04.5 LTS, x86-64 | CPU-only | None | none | d232e1ec66ddcfd38d38ff4a553bb80d3f639d02; installed capsule 0.1.0-alpha.2-a5d0fce3ca5e | pass | pass | Not run; seven synthetic browser assertions passed in the historical receipt | [Tracking #274](https://github.com/Exculpata/exculpata/issues/274) | Docker 29.1.3; Compose 2.40.3+ds1-0ubuntu1~24.04.1. Clean-node journey 313.26 s; no model generation or transcription exercised. |
| GPU reference target: 16 CPU cores, 64 GiB RAM, 300 GiB free SSD, one NVIDIA GPU with at least 24 GiB VRAM; actual trial hardware not recorded | Linux x86-64; distribution not recorded | Not recorded | large-v3 ASR, EN/ES alignment, Nemotron; exact trial revisions and precision not recorded | portable | Exact trial commit not recorded | partial | not-recorded | Not run; synthetic review and EN/ES transcription workflow reported, independent language-quality review unavailable | [Tracking #274](https://github.com/Exculpata/exculpata/issues/274) | New application state with warm artifacts; no clean-OS or cold-download claim. Target resources are not tested minimums. |

## Maintaining the table

Use `pass`, `fail`, or `partial` for Install. Use `pass`, `fail`, `partial`,
`not-run`, or `not-recorded` for doctor. A partial install means only some intended
steps completed; explain the boundary in Notes. Report skipped evaluations as
not run, and distinguish deterministic probes from model or installed-node tests.
Never infer a quality pass from a zero command exit status.

Each submitted report has ten data fields, one for each column except Report:
GitHub supplies that issue URL after submission. The privacy checkbox is an
attestation, not an additional data column. Hardware combines CPU, RAM, disk,
GPU count and per-GPU VRAM; Backend includes its version; Notes includes driver,
CUDA and Docker/Compose versions, timings and untested steps. Keep names and
identifiers out of every field. Review reports manually before adding rows.

Run `python3 scripts/check-compatibility-table.py` and
`python3 scripts/check-tester-doc-links.py` from the repository root.
The validator checks structure and obvious identifiers in Hardware and Notes;
it cannot recognize every person's name, employer, or sensitive detail.
External links are not fetched by the offline link check.
