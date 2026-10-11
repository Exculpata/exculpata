# Third-party notices

This is a source-alpha inventory, not a substitute for upstream license texts.
No model weights or third-party runtime binaries are bundled in this repository.
Operators installing optional packages or downloading models must retain and
review their upstream terms. A supported image release still requires a complete
resolved software-composition and model-license review; source publication does
not claim that review is complete.

## Interface fonts

The workbench bundles Geist and JetBrains Mono as variable WOFF2 files under
`src/case_intelligence/static/fonts/`. Both are licensed under the SIL Open
Font License 1.1. The license texts ship beside the font files
(`OFL-Geist.txt` and `OFL-JetBrainsMono.txt`). Pages load them from relative
URLs and do not call a font delivery network. See
[dependency notes](docs/DEPENDENCIES.md#bundled-interface-fonts).

## Runtime foundations

RecordBench builds on Python, FastAPI/Starlette, Uvicorn, PostgreSQL, pgvector,
nginx, ClamAV, Tesseract, Poppler, FFmpeg, ImageMagick, Apache HTTP Server,
`mod_auth_gssapi`, restic, vLLM, PyTorch, Transformers, sentence-transformers,
WhisperX, faster-whisper, pyannote.audio, and their transitive dependencies.
Container and Python package metadata retain the authoritative upstream license
and notice material. Operators must review the resolved image and package lock
inventory for the release they deploy.

## Recording-check dependency

The recording check pins `webrtcvad-wheels` 2.0.14. The Python wrapper uses the
MIT license; the bundled WebRTC implementation has its own BSD notice. Retain
the [upstream license texts](https://github.com/roman-nekrasov/py-webrtcvad-wheels/blob/master/LICENSE)
shipped with the dependency. This CPU VAD adds no downloaded model weights or
network service.

## Default model artifacts

The immutable IDs and revisions are recorded in `config/models.json`. The
current declarations are:

- Qwen3.5 4B, Qwen3.5 9B, Granite Embedding English R2, and GTE ModernBERT reranker:
  Apache-2.0 as declared upstream;
- faster-whisper large-v3: MIT as declared upstream;
- torchaudio English and Spanish alignment artifacts: BSD-2-Clause as declared
  upstream;
- NVIDIA Nemotron 3 Diarization: [OpenMDW-1.1](https://huggingface.co/nvidia/Nemotron-3-Diarization/tree/0f087031414a6616bda8228f447d915a70a25720), ungated;
- optional pyannote Community-1 diarization: CC-BY-4.0 and gated upstream terms;
- Community-1 dependency artifacts: upstream-terms review is still required
  before any redistribution or supported deployment claim.

No model weights are committed to this repository. The installer retrieves
exact revisions only after the operator reviews applicable terms. A Hugging
Face token is used for staging and is not retained by runtime services.

The NLTK `punkt_tab` tokenizer resource is revision- and hash-pinned in
`config/models.json`. Its upstream data terms remain under review; do not infer
the model-data license from the NLTK software license. See
[dependency qualification](docs/DEPENDENCIES.md#tokenizer-resource).

## WhisperX bundled voice-activity checkpoint

WhisperX 3.8.6 includes `whisperx/assets/pytorch_model.bin`. Its SHA-256,
`0b5b3216d60a2d32fc086b47ea8c67589aaeb26b7e07fcbe620d6d0b83e209ea`,
matches the published LFS object in
[pyannote/segmentation at the recorded revision](https://huggingface.co/pyannote/segmentation/tree/660b9e20307a2b0cdb400d0f80aadc04a701fc54).
The model repository's MIT license is Copyright (c) 2022 CNRS; the complete
[notice is retained here](services/transcription/licenses/pyannote-segmentation-MIT.txt).
The service wheel includes the notice in its license metadata, and service
images copy it to `/usr/share/licenses/recordbench-transcription/`.

This checkpoint detects speech activity; it is separate from the selected
Nemotron speaker diarizer. Its upstream repository has a download gate, but
the selected checkpoint is already bundled by the pinned WhisperX wheel.
No runtime model-hub access or additional end-user token step is introduced.
This exact-artifact attribution does not approve different model revisions or
resolve the separate NLTK tokenizer-data terms.

The RecordBench compatibility distribution `whisperx==3.8.6+recordbench.1` builds
from pinned upstream source with dependency metadata changes. Its wheel preserves
the upstream BSD text and CNRS MIT notice, including the same VAD checkpoint.
The [build recipe](services/transcription/compatibility/README.md) records the
source identity, unchanged inference code and reproducibility checks.
