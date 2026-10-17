# Open/local model policy

This policy applies to the bundled service rooted at the operator-selected
transcription data path. It does not authorize access to, modification of, or
model downloads into any unrelated application environment.

## Runtime boundary

- Transcription, alignment, diarization, and optional translation run locally.
- Hosted or commercial transcription APIs are not implemented in this project.
- `TRANSCRIPTION_V2_PIPELINE=mock` is the safe default. Mock output is synthetic,
  visibly degraded, and prohibited for evidentiary use.
- Real inference requires the explicit `whisperx` backend, an assigned GPU,
  a pinned worker environment, and pre-staged approved weights.
- API, UI, export, and test processes must remain importable without Torch,
  CUDA, WhisperX, pyannote, NeMo, or model weights.
- The runtime worker must not download weights. Stage them separately, set
  `HF_HUB_OFFLINE=1`, block runtime network egress, and leave
  request `local_files_only=True`.

The model cache is configured with `TRANSCRIPTION_V2_MODEL_CACHE`. It must not
point at another application's environment or a writable shared cache whose
contents can change without release control. Set it to the same Hugging Face hub cache
selected by `HF_HOME` (the example uses
`/var/lib/recordbench/model-cache/huggingface/hub`) so every offline loader
resolves the same staged snapshots.

## Approved initial components

| Component | Use | License | Conditions |
|---|---|---|---|
| [OpenAI Whisper weights](https://github.com/openai/whisper) | `large-v3` quality/balanced ASR; `turbo` fast ASR; `large-v3` English translation | MIT | Pin exact model revision and file hashes. Turbo is not approved for translation. |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | CTranslate2 inference runtime | MIT | Pin package/container and CTranslate2 versions. |
| [WhisperX](https://github.com/m-bain/whisperX) | orchestration and source-language forced alignment | BSD-2-Clause | Pin release/commit; alignment checkpoints are governed separately below. |
| WhisperX packaged pyannote VAD checkpoint | speech activity detection | model-specific; approval required | WhisperX 3.8.6 includes `whisperx/assets/pytorch_model.bin`. Record its package hash, checkpoint hash, origin, and approved license before real use. The registered profiles use this local checkpoint because WhisperX's Silero adapter calls Torch Hub at runtime. |
| [pyannote.audio](https://github.com/pyannote/pyannote-audio) | diarization toolkit | MIT | Disable optional metrics/telemetry in the worker environment. |
| [Community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) | anonymous speaker diarization | CC-BY-4.0, gated | Accept access terms once, retain attribution, pin the snapshot, and use a read-only staging token. A token must not be embedded in code, manifests, logs, or delivery files. |
| [Nemotron 3 Diarization](https://huggingface.co/nvidia/Nemotron-3-Diarization) | anonymous speaker diarization | OpenMDW-1.1, ungated | New installer selection; exact revision, separate hash-locked runtime and acceptance boundary in [NEMOTRON.md](NEMOTRON.md). No hub token required. |

“Approved” means eligible for an isolated evaluation. It does not mean the
model is accurate enough for users or approved for every language and domain.

### Alignment checkpoint rule

WhisperX selects a language-specific torchaudio or Hugging Face acoustic model.
Those checkpoints do not inherit WhisperX's BSD license. Before enabling a
language, record and approve the exact aligner repository, revision, artifact
hashes, license, training-data constraints, and required attribution. If no
approved local aligner exists, alignment must fail visibly and diarization must
be skipped; the service may deliver the source ASR only as a degraded result.
It must never silently claim word timing or speaker attribution.

WhisperX 3.8.x also uses NLTK `punkt_tab` sentence data during alignment and can
try to download it when missing. Pre-stage and license-review the required Punkt
language resources in matching API and worker `NLTK_DATA` directories. Language
admission checks the four required regular files without importing NLTK or ML
libraries. Missing or incomplete resources suppress that language and reject new
requests before durable job creation. The worker rechecks before alignment and
fails offline before WhisperX can invoke its downloader.

## Initial profiles

All profiles follow the same stage order:

1. source-language transcription;
2. source-language forced alignment;
3. selected Nemotron or Community-1 diarization and WhisperX speaker assignment;
4. optional, separate English translation artifact.

| Profile | Source ASR | Decoder | Intended use |
|---|---|---|---|
| `balanced` | Whisper `large-v3` | beam/best-of 5, batch 8 | Initial reference configuration pending evaluation. |
| `high_accuracy` | Whisper `large-v3` | beam/best-of 8, batch 4 | Evaluation hypothesis; retain only if it materially beats balanced. |
| `fast` | Whisper `turbo` | greedy-style beam/best-of 1, batch 16 | Draft/throughput lane requiring review. |

If English translation is requested, all three use a separate `large-v3`
translation pass. Translation never replaces the source transcript. Whisper's
approved translation path outputs English only, and translated words are not
forced-aligned to source-language audio. The artifact and manifest state this
limitation.

Per-job speaker minimum/maximum values and hotwords are hints. They are retained
in provenance, are never represented as ground truth, and must be included in
the evaluation matrix.

## Experimental models: disabled by default

The following permissively licensed NVIDIA models are documented in
`profiles.py` but are not registered user profiles and have no active adapter:

- [Parakeet-TDT 0.6B v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3),
  CC-BY-4.0: candidate high-throughput ASR for 25 European languages.
- [Canary 1B v2](https://huggingface.co/nvidia/canary-1b-v2), CC-BY-4.0:
  candidate ASR and English-paired speech translation for 25 European languages.
- [Canary-Qwen 2.5B](https://huggingface.co/nvidia/canary-qwen-2.5b),
  CC-BY-4.0: English accuracy candidate whose model card limits trained input
  duration to 40 seconds.

NeMo framework licensing does not automatically determine the license of every
checkpoint. Each model must pass the same artifact-level review. These models
remain disabled until an adapter, pinned environment, long-file strategy,
provenance mapping, and representative quality evaluation are complete.

Models restricted to non-commercial use, including the published
CC-BY-NC SeamlessM4T and NLLB checkpoints, are excluded. A general model hub
listing, “open weights” label, or research paper is not sufficient approval.

## Staging and supply-chain record

For every runtime artifact, maintain outside the delivery package:

- project/model URL and owner;
- exact release, commit, or model snapshot revision;
- license text and review decision;
- weight/config/tokenizer hashes and total bytes;
- dependency lock/container digest;
- staging date and operator;
- required attribution and access terms;
- supported language/domain declaration;
- evaluation result and approval status.

Download only in an approved staging window. Scan the snapshot, reject unsafe
pickle/custom-code artifacts unless separately reviewed, copy approved content
into the release-controlled cache, then operate offline. Runtime services must not contain
a reusable Hugging Face token after the gated snapshot is resolved locally.

## Privacy, telemetry, and delivery

- The service is ephemeral delivery, not a transcript repository.
- Exporters require a caller-supplied per-job transient output directory. They
  never select global storage, never copy source media, and never clean outside
  that job directory. The durable job runner deletes inputs, outputs, review
  rows, and metadata after download-and-delete or the short expiry window.
- Model telemetry must be disabled. Set `PYANNOTE_METRICS_ENABLED=0` and block
  unapproved egress for confidential processing.
- Logs and metrics contain job IDs and stage/status codes only—not filenames,
  transcript text, hotwords, roster names, model tokens, or audio-derived data.
- Speaker labels are anonymous clusters. They are not real-world identities.
  Voiceprint enrollment or cross-recording matching requires a separate privacy,
  authorization, calibration, and deletion decision.
- Delivery manifests include source SHA-256, selected profile/component
  declarations, stage warnings/errors, delivery-artifact hashes, and license
  attribution. Before real inference can be activated, provenance must also
  record the exact approved model/aligner snapshot revisions and verified weight,
  configuration, and tokenizer hashes. Manifests do not include source media and
  do not imply long-term storage.

## Activation and quality gates

No model/profile becomes a release default based on vendor benchmarks. It must:

1. pass license, security, artifact-integrity, and offline-startup review;
2. run against the consent-cleared gold corpus without touching production;
3. report WER/CER, critical name/number/term accuracy, DER/JER, speaker-count
   error, overlap-included speaker-attributed WER/cpWER, and translation review;
4. report real-time factor, p50/p95 duration, peak VRAM/RAM, OOM/failure rate,
   and correction minutes per audio hour;
5. show no material regression in any required language, recording type, noise,
   speaker-count, or overlap cohort;
6. expose every missing alignment, diarization failure, unsupported translation,
   and fallback as a degraded/failed stage;
7. pass long-file, restart/retry, cancellation, deterministic-export, artifact
   deletion, and no-egress tests; and
8. complete a human blind review before use beyond synthetic evaluation.

Acceptance thresholds should be set from a frozen, approved benchmark baseline before
the bake-off. Aggregate WER alone is not a release gate. A profile must improve
speaker-attributed usability and critical-entity accuracy without unacceptable
latency, resource, privacy, or failure regressions.
