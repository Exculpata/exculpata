# Runtime dependency locks

The application, retrieval worker, transcription API/UI and ASR worker,
model stager and separate Nemotron
interpreter install hash-locked Python dependencies. These locks target Linux
x86-64: Python 3.12 for the application/stager, Python 3.11 for transcription.
They are included in the installer's source capsule. A successful source hash
check does not establish the identity of an installed container.

| Component | Runtime lock | Build tools |
| --- | --- | --- |
| Application + PostgreSQL client | `requirements/application.lock` | `requirements/build.lock` |
| Transcription API/UI | `services/transcription/requirements-api.lock` | `services/transcription/requirements-api-build.lock` |
| Model stager | `deploy/model-stager/requirements.lock` | `deploy/model-stager/requirements-build.lock` |
| Retrieval add-on | `requirements/retrieval.lock` | Application build tools |
| WhisperX ASR worker | `services/transcription/requirements-worker.lock` | Transcription API build tools |
| Nemotron | `services/transcription/requirements-nemotron.lock` | `services/transcription/requirements-nemotron-build.lock` |

Docker installs these dependencies with `--require-hashes --no-build-isolation`, installs the local
application with `--no-deps --no-build-isolation`, and runs `pip check`.
The separate Nemotron interpreter uses the official Torch 2.14.0 CUDA 12.6
wheel and the exact Transformers archive recorded in its `.in` file. The
Transformers source revision is necessary because the reviewed 5.17.0 release
wheel does not contain Nemotron diarization. Development versions require
source-level advisory review; a package scanner's skipped dependency is not a
clean result.

The base images and Dockerfile frontend are bound to immutable manifest
digests. Those digests establish artifact identity, not vulnerability clearance.
OS package acquisition, whole-image SBOMs and advisory dispositions remain
incomplete before a supported release. The ASR worker now builds the explicitly
versioned [WhisperX compatibility wheel](../services/transcription/compatibility/README.md).
Its reviewed metadata patch permits the locked runtime without changing upstream
inference code. Package resolution and native CPU probes do not qualify GPU
inference or establish that a whole image has no vulnerabilities.

## Reproduce the Python resolutions

Resolution used uv 0.11.16 with hashes, CPython targets and the GNU Linux x86-64
platform. Run from the repository root:

```sh
uv pip compile pyproject.toml --extra postgres --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --generate-hashes --no-header --no-annotate -o requirements/application.lock
uv pip compile services/transcription/pyproject.toml --python-version 3.11 --python-platform x86_64-unknown-linux-gnu --generate-hashes --no-header --no-annotate -o services/transcription/requirements-api.lock
uv pip compile deploy/model-stager/requirements.in --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --generate-hashes --no-header --no-annotate -o deploy/model-stager/requirements.lock
uv pip compile requirements/retrieval.in -c requirements/application.lock --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --generate-hashes --no-header --no-annotate -o requirements/retrieval.lock
# First build the compatibility wheel using the pinned tools and recipe linked above.
uv pip compile services/transcription/pyproject.toml services/transcription/requirements-worker.in --extra ml -c services/transcription/requirements-api.lock --find-links /tmp/exculpata-whisperx-wheels --python-version 3.11 --python-platform x86_64-unknown-linux-gnu --generate-hashes --no-header --no-annotate -o services/transcription/requirements-worker.lock
uv pip compile services/transcription/requirements-nemotron.in --python-version 3.11 --python-platform x86_64-unknown-linux-gnu --generate-hashes --no-header --no-annotate -o services/transcription/requirements-nemotron.lock
```

Test locks add the `dev` extra while constraining the runtime dependencies:

```sh
uv pip compile pyproject.toml --extra postgres --extra dev -c requirements/application.lock --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --generate-hashes --no-header --no-annotate -o requirements/application-test.lock
uv pip compile services/transcription/pyproject.toml --extra dev -c services/transcription/requirements-api.lock --python-version 3.11 --python-platform x86_64-unknown-linux-gnu --generate-hashes --no-header --no-annotate -o services/transcription/requirements-api-test.lock
```

To reproduce application tests in a new Python 3.12 virtual environment:

```sh
python -m pip install --no-build-isolation --require-hashes -r requirements/build.lock
python -m pip install --no-build-isolation --require-hashes -r requirements/application-test.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m pip check
python -m pytest -q
```

Regeneration intentionally consults current package indexes and may change the
resolved versions. Review every lock diff, audit the resolved dependencies,
and validate fresh environments before publishing updates. GPU execution,
image builds and recovery need their own evidence; resolver success is not
runtime acceptance.

## Bundled interface fonts

The workbench self-hosts two variable fonts. Stylesheets load them with relative
`@font-face` URLs under `src/case_intelligence/static/fonts/`. The application
does not contact a font service at runtime.

| Font | Files | License |
| --- | --- | --- |
| Geist | `Geist-Variable.woff2` | SIL Open Font License 1.1, [OFL-Geist.txt](../src/case_intelligence/static/fonts/OFL-Geist.txt). Copyright 2024 The Geist Project Authors. |
| JetBrains Mono | `JetBrainsMono-Variable.woff2`, `JetBrainsMono-Variable-Italic.woff2` | SIL Open Font License 1.1, [OFL-JetBrainsMono.txt](../src/case_intelligence/static/fonts/OFL-JetBrainsMono.txt). Copyright 2020 The JetBrains Mono Project Authors. |

Geist is the interface and heading face. JetBrains Mono is the face for code,
identifiers, and citations. Both include system-font fallbacks. The Latin-subset
files are the Fontsource variable builds of the upstream OFL fonts.

## Tokenizer resource

`config/models.json` records the exact NLTK `punkt_tab` archive revision and
SHA-256. Staging verifies the hash before extraction, rejects unsafe archive
entries, and limits the compressed download to 8 MiB, expanded data to 16 MiB
and inventory to 256 entries. The staging receipt binds extracted bytes and
supports verification without network access.

A differing or extra existing tokenizer artifact requires a fresh staging
root; staging preserves it instead of overwriting it. This also applies to
legacy downloader ZIPs. Follow the normal model staging procedure using a new
root and verify its receipt before selecting it for a deployment.

The reviewed archive README and package metadata do not supply a clear license
statement. Its catalog entry remains `UPSTREAM-TERMS-REVIEW-REQUIRED` until the
resource terms are established. The NLTK software license is not automatically
the tokenizer data's license.

See [model acquisition](MODEL_ACQUISITION.md) for a no-network acquisition preview,
explicit acknowledgement, and supplying the exact tokenizer ZIP locally.

The [transcription runtime qualification record](TRANSCRIPTION_RUNTIME_QUALIFICATION.md)
tracks the current package audit, scoped NLTK disposition and remaining GPU/image evidence.
