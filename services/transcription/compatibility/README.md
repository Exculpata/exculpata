# WhisperX compatibility wheel

This recipe builds `whisperx==3.8.6+exculpata.1` from upstream revision
`3ccc17b8de34f305300f8a3fd3c9f76ba820c0d0`. It verifies the upstream archive
SHA-256 before bounded extraction, verifies the original `pyproject.toml`, and
changes dependency metadata only. All 16 `whisperx/*.py` modules remain unchanged.
The upstream BSD license and the bundled VAD's CNRS MIT notice remain in the wheel.

The metadata selects Torch 2.14.0, TorchAudio 2.11.0, TorchVision 0.29.0,
TorchCodec 0.16.0, Transformers 5.17.0 and Hugging Face Hub 1.x. The worker lock
selects official CUDA 12.6 Torch/TorchVision wheels and **CPU TorchAudio and
TorchCodec**. The ordinary TorchAudio wheel requires a different CUDA runtime;
substituting it is not an equivalent installation. Nemotron retains its separate
interpreter and lock.

Build tools, Python 3.11 and a fixed wheel timestamp are required. From the
repository root, in a new Python 3.11 virtual environment:

```sh
python -m pip install --require-hashes --no-build-isolation \
  -r services/transcription/requirements-api-build.lock
python services/transcription/compatibility/build_whisperx.py --output /tmp/exculpata-whisperx-wheels
python -m pip install --require-hashes --no-build-isolation \
  --find-links=/tmp/exculpata-whisperx-wheels \
  -r services/transcription/requirements-worker.lock
python -m pip check
```

The output directory must be new. `--source-archive /path/to/archive.tar.gz`
uses a separately acquired source archive and performs no source download;
its exact hash is still mandatory. `build-receipt.json` records source, metadata,
build tools, unchanged Python source hashes and wheel digest, without build paths.
The dependency lock independently verifies the wheel before installation.

Two fresh source extractions under different umasks produced the same wheel:
`b4a4b3672e868234f7149423d6db0684757fdbbe233dff994adb94e622272555`.
This is Python-wheel evidence, not a bit-for-bit container-image claim.
Docker's worker target runs this same recipe and installs with normal dependency
checking; it does not use `--no-deps` to hide WhisperX conflicts.

Before changing the patch, verify upstream source identity and notices, regenerate
locks, install into a fresh environment, audit packages and run native audio,
EN/ES alignment/transcription, offline and GPU workflow acceptance. Publish a
new local version for changed package contents; do not reuse this version for a
different patch. Report upstream API fixes separately if inference-code changes
become necessary. This recipe does not declare the GPU workflow accepted.

The [runtime qualification record](../../../docs/TRANSCRIPTION_RUNTIME_QUALIFICATION.md)
separates the patched-runtime synthetic GPU trial from the remaining final-candidate
and representative-quality acceptance gates.
