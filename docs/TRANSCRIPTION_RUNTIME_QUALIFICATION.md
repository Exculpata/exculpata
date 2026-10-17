# Transcription runtime qualification

The beta candidate uses the versioned
[WhisperX compatibility recipe](../services/transcription/compatibility/README.md).
Source identity, deterministic wheel builds and normal dependency resolution are
separate from acceptance of an actual GPU transcription job.

The September 28, 2026 package audit checked all 153 resolved worker/API
coordinates with no skipped packages. It reported one remaining finding:
`PYSEC-2026-3740` / `GHSA-8mgp-746c-j5xp` in NLTK 3.10.3. Direct CUDA/CPU wheel
versions were mapped to their upstream package versions for advisory lookup;
that audit does not inspect the native binaries or replace an image scan.

The fresh environment installed 155 packages including build tools with a passing
dependency check. Eleven native adapter imports and one-second synthetic audio
decode/resample probes passed with IPv4/IPv6 socket creation denied. No model
constructors or GPU inference ran in those checks.

## NLTK applicability disposition

The affected functions are `TransitionParser.train/parse`,
`AveragedPerceptron.save/load`, `PerceptronTagger.save_to_json` and
`save_maxent_params`. The assessed transcription entry point accepts profile,
language and job options; it does not expose those training/persistence paths.
Its WhisperX alignment call uses the Punkt tokenizer's tabular parameters.

The scoped beta disposition is **affected dependency present; affected
functionality not exposed by the assessed transcription route**. This is not a
claim that NLTK is fixed or that every possible use of it is safe. Retain the
finding in reports and reassess when NLTK/WhisperX, model-import endpoints,
plugin access or training/export capabilities change. Model-independent tests
and synthetic tokenizer probes are bounded evidence, not exhaustive proof for
all inputs. The upstream advisory remains the authority on the affected code.

## September 29 synthetic GPU evidence

The later isolated synthetic trial **did exercise the patched runtime**. Its
retained runtime image identities were matched to checksum-verified image SBOMs.
Those inventories identify WhisperX `3.8.6+recordbench.1`, Torch `2.14.0+cu126`,
TorchAudio `2.11.0+cpu`, TorchVision `0.29.0+cu126`, TorchCodec `0.16.0+cpu` and
Transformers `5.17.0` in the worker. The separate Nemotron environment identifies
Torch `2.14.0+cu126` and Transformers `5.18.0.dev0`.

The trial's compatibility builder, worker lock, Nemotron lock, API build lock and
transcription Dockerfile match the files published at commit
`81c30e9d4c61a7d1221245392164ab40536909bd`. They also remain unchanged in this
review follow-up. Recorded source checks matched the running worker modules to
the trial candidate. This connects the exercised images to the compatibility
recipe; it does not imply that subsequent application changes were GPU-tested.

Non-mock English and Spanish jobs produced nonempty timestamped JSON, text, SRT
and VTT exports and successful Nemotron speaker attribution. The synthetic
recordings were approximately 39 and 36 seconds long. Reference-word checks and
two-speaker smoke checks passed, as did authenticated browser playback. A repeat
English job after restart and a new Spanish job after clean-root restore passed
the same checks. Worker network isolation and blocked outbound TCP were recorded.
Models were already staged: this was a warm-cache offline workflow test.

These are functional smoke results, **not representative language-quality
acceptance**. There was no independent human accuracy review, broad audio corpus,
cold-host installation, or unfamiliar-operator acceptance. Private execution
receipts are retained outside this repository; no deployment metadata or raw
operational records are published here. Contributors can collect their own
receipts using the [reference-install guide](BETA_REFERENCE_INSTALL.md).

## Remaining evidence before the GPU beta claim

The September 28 checks and September 29 trial provide partial evidence for the
items below. They do not close the final release-candidate gates:


- Fresh installation of the API/worker locks with build tools installed first,
  normal dependency checks, and native decoder imports.
- Rebuilt final-candidate EN/ES transcription, alignment and Nemotron with exact
  source/model/image identities and network isolation recorded, plus representative
  audio and human review of transcription, timestamps and speaker accuracy.
- End-to-end browser playback/export, malformed input, successful retry, restart
  and clean-root recovery of the documented retained state.
- Actual image/OS inventory and advisory dispositions; the package audit above
  does not cover the final base image, OS packages or separate Nemotron image
  contents. The Dockerfile's pinned base still uses distribution package indexes.

Record pending evidence explicitly in the
[reference-install receipt](BETA_REFERENCE_INSTALL.md#acceptance-receipt-and-feedback).
A successful build or import must not be reported as GPU or transcription-quality
acceptance. Additional hardware/model combinations remain unqualified until tested.
