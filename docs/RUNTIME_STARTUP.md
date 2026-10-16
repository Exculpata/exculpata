# Prepared-node and model startup

Resume accepts an empty Compose running-service listing, including a blank line,
as no running service. Nonempty malformed names still stop the operation, and
actual free GPU capacity must pass before provisioning. Existing observed
services are handled as described in [installation](INSTALL.md).

The retrieval service loads its pinned embedding and reranker artifacts during
startup with offline-only loading. Its health check requires both loaded flags
before the application starts. Missing or incompatible artifacts fail startup;
readiness does not become true merely because HTTP is listening. This avoids
waiting for a first application request that cannot arrive until Compose
readiness passes. Allow startup time for both models to load.

The reference retrieval image includes `gcc` and `libc6-dev`: the pinned PyTorch
runtime may compile native Triton launchers during ordinary GPU operations as
well as explicit graph compilation. `TMPDIR` and `TRITON_CACHE_DIR` are scoped
to `/tmp/retrieval-native`, a 512 MiB executable tmpfs retaining `nosuid` and
`nodev`. The rest of `/tmp` remains `noexec`, and model artifacts stay read-only.
Allowing only eager execution does not remove this native-kernel requirement.

The transcription worker uses the same compiler packages and a separate
`/tmp/transcription-native` cache with the same 512 MiB bound. This applies to
both its ASR/alignment interpreter and its Nemotron interpreter. Its network
mode remains `none`; native compilation uses local package/toolchain files.
Nemotron gives each subprocess a fresh Triton cache beneath that bounded
executable scratch directory. It does not inherit arbitrary parent cache paths
or fall back to the non-executable home directory; subprocess exit removes the
per-call cache along with the other temporary files.

## Generator compiler cache

The pinned generator runtime builds native Triton launchers during startup. Its
dedicated `/tmp/vllm-cache` tmpfs permits execution, retains `nosuid` and `nodev`,
and is bounded to 8 GiB within the container memory limit. Model artifacts
remain mounted read-only. Adding `noexec` to this compiler cache makes native
loading fail with “failed to map segment from shared object.” Other temporary
mounts retain their own restrictions.

With the pinned generator image already available locally, run a model-free
check against the actual Compose cache options:

```bash
EXCULPATA_GENERATOR_CONTAINER_TEST=1 python -m pytest tests/test_generator_cache_container.py -q
```

The check uses no GPU, network, model cache, or image pull. It does not replace
model startup and inference acceptance.

The retrieval and worker toolchain/cache regression also runs without a GPU,
network, model mount or image pull. Point it at already built local images:

```bash
EXCULPATA_RETRIEVAL_TEST_IMAGE=exculpata/retrieval:dev EXCULPATA_WORKER_TEST_IMAGE=exculpata/transcription-worker:dev python -m pytest tests/test_generator_cache_container.py -q -k native_runtime
```

It compiles and loads an independently constructed constant-returning C library
inside the actual Compose temporary-cache options, with a read-only root and
non-root user. GPU model inference remains a separate acceptance requirement.

## Transcription queue and worker readiness

The authenticated API `/ready` response has `scope: queue`. It checks writable
queue storage, the disk reserve, media tools, the local model manifest/cache
and offline configuration. It does not claim that the API container contains
GPU drivers or ML packages. Those belong to the separate worker.

The worker verifies approved model artifact bytes when starting and checks
actual reserved-device capacity before claiming each job. Work remains queued
while GPU capacity is insufficient; an unavailable worker cannot finish queued
work. Inspect worker status and job progress separately from API admission.
The worker-local CLI readiness diagnostic retains its GPU/package/VAD checks
and accepts only upstream WhisperX 3.8.6 or the qualified 3.8.6+exculpata.1
build, with unknown patched versions rejected.

WhisperX's packaged VAD initializes `TORCH_HOME` before opening its bundled
checkpoint. The worker points that setting at the already staged
`/models/huggingface/hub` directory, which remains read-only. It must not point
at a nonexistent directory under the read-only model mount. The optional
`test_worker_packaged_vad_loads_with_read_only_staged_cache` container test loads
that packaged VAD on CPU, offline, with an otherwise empty read-only staged
cache and verifies that the cache contents are unchanged.

## Cleanup process status

The one-shot transcription cleanup and cleanup daemon reuse the API image but
serve no HTTP endpoint. Their Compose definitions disable the image's HTTP
health check; the API retains its own probe. A running cleanup container reports
process liveness, not proof that its most recent cleanup succeeded.

Each successful `purge-expired` invocation prints content-free `purged_jobs`
and `wal_checkpointed` fields. The daemon waits five minutes only after a
successful invocation. An error exits the shell with a nonzero status, which
is visible in the container's exit/restart state. Check that state and the
cleanup result when diagnosing retention; do not infer success from `running`
alone. This status correction does not change expiry, deletion or retry policy.

Companion images are built and updated by the installer; see
[managed services](MANAGED_SERVICES.md) for host prerequisites, model acquisition
and the security-update boundary.
