# Hardware compatibility

This table collects community hardware reports. It is not a support promise
and it does not make a configuration production-ready. Use synthetic data
only. Add a row by filing a
[hardware report](https://github.com/Exculpata/exculpata/issues/new?template=hardware_report.yml).

| Tier | Status | OS | CPU | RAM | GPU | VRAM | Driver / CUDA | Model and quantization | Profile | Exculpata version | Key timings | Issue |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Document review and cited answers | Reference-tested | Ubuntu 24.04 x86-64 | 16 cores | 64 GB | NVIDIA | 24 GB | not recorded | Qwen3.5-4B portable, Whisper large-v3, pinned English and Spanish alignment, Nemotron | portable | 0.1.0-beta.1 | not recorded | [GPU reference journey](BETA_REFERENCE_INSTALL.md). The same run covered document review. |
| Transcription and speaker labels | Reference-tested | Ubuntu 24.04 x86-64 | 16 cores | 64 GB | NVIDIA | 24 GB | not recorded | Qwen3.5-4B portable, Whisper large-v3, pinned English and Spanish alignment, Nemotron | portable | 0.1.0-beta.1 | not recorded | Same reference run as document review. |

Portable cited answers are admitted from 16 GB of NVIDIA VRAM (20 GB when
transcription shares that GPU). The 24 GB rows above are the tested reference.
A separate 16 GB reference run is not recorded. The Ubuntu 24.04 CPU try-it
(8 cores, 16 GB RAM, no GPU) is the installation playbook path. It is not a
reference-tested row in the GPU journey.

## Known not working / untested

- Arch Linux packages are listed and have not been accepted as a working node.
- Non-NVIDIA GPUs are untested.
- A macOS host is untested.
- OIDC sign-in is outside the reference identity scope and is untested for this beta.

## Maintenance

The maintainer adds rows from issues labeled `hardware-report`. A row that is
older than two releases is marked stale when a newer release is published.
