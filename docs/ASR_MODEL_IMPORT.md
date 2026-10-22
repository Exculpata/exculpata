# Operator-selected speech models and offline import

Administrators can bind the existing transcription profiles to their own
Whisper-compatible CTranslate2 model snapshots. This is an advanced worker
configuration interface. The reference installer still acquires its catalog;
it does not yet provide an interactive custom-model preparation wizard.

This does not accept arbitrary ASR architectures, Python plugins or model paths
from job requests. File inventory checks establish identity and required files,
not model accuracy or successful execution on a particular GPU. Evaluate each
new model with synthetic audio before using it for work.

## Selection contract

The worker and API accept `transcription-v2-model-manifest-v1`. It
retains the reference large-v3/Turbo mappings. Version 2 adds a required
`asr_bindings` object to the existing manifest:

```json
{
  "schema_version": "transcription-v2-model-manifest-v2",
  "asr_bindings": {
    "primary": {
      "adapter": "faster-whisper",
      "model_id": "example/compatible-speech",
      "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "supports_translation": true
    }
  },
  "artifacts": []
}
```

This illustrates the selection fields only: the empty artifact list is
intentionally invalid. Supply the real artifact entries described below. The
example identity and revision are synthetic, not downloadable models.

- `primary` supplies Balanced, Highest accuracy and the separate English
  translation pass. These names describe decoding settings, not measured
  quality of an operator's model.
- `fast` supplies Fast source transcription. It is optional. Omitting a slot
  disables its profiles; version 2 never falls back to a reference repository.
- `adapter` must be `faster-whisper`.
- `model_id` is a namespaced identifier, not a path or credential-bearing URL.
- `revision` is an exact 40-character lowercase hexadecimal upstream commit or
  `sha256-` followed by a 64-character lowercase content digest for a local
  conversion. Record a meaningful immutable conversion identity; per-file
  hashes below are the actual byte verification boundary.
- `supports_translation` is a required boolean capability declaration. Set it
  to true only after confirming English translation support; source-only/Turbo
  models must not gain translation merely because their files are present.

Each binding must match exactly one `artifacts` entry with role `asr`, the same
`model_id` and `revision`, a `license` description, and a nonempty `files` list.
Each file entry has `path` (cache-relative), `size_bytes` (integer), and
`sha256` (64 hexadecimal characters). Include every file the snapshot contains.
For ASR symlink caches, also provide `snapshot_path`: the exact cache-relative
logical name under `models--<owner>--<model>/snapshots/<revision>/`. Its `path`
identifies the regular blob whose hash is approved. Both manifest versions bind
each ASR snapshot name to that exact verified file; approving a blob does not
approve other filenames pointing to it. Conflicting filename bindings are invalid.
Regular snapshot files use `path` itself as the logical name and need no extra
field. Diarization snapshots also support this binding; alignment and other
artifact roles do not accept `snapshot_path`.

Older symlink-cache manifests containing only blob paths must be regenerated
through staging after verifying the intended layout against the pinned upstream
revision. Stop admission and drain work first; restart both API and worker after
activation. Do not regenerate approval over an unexplained modified cache.

The canonical snapshot layout is:

```text
models--example--compatible-speech/
  snapshots/
    aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/
      config.json
      model.bin
      tokenizer.json
      vocabulary.json
approved-model-manifest.json
```

`vocabulary.txt` may replace `vocabulary.json`. The importer described below
requires regular snapshot files, not symlinks, for both v1 and v2 inventories.
Mapped blob layouts are rejected before a destination is created. Every ASR
artifact is checked before copying and again before publishing the destination
manifest, including v1 defaults and artifacts without a v2 processing-slot binding.
Existing runtime caches may
retain supported Hugging Face links to verified blobs within the same cache.
Materializing such a cache for import is a separate preparation step; this
command does not recreate link topology or follow arbitrary links.

Alignment and diarization artifacts keep their existing role/file contracts.
Include their independently acquired resources in the inventory if those
features are needed. The importer does not supply missing alignment checkpoints,
Punkt data, Nemotron weights or software dependencies. Missing alignment
inventory prevents new jobs through the API even when ASR files are available.
Punkt is a separate staged resource, outside the imported model manifest. Set
matching `NLTK_DATA` roots in API and worker. Language admission requires the
four regular files `collocations.tab`, `sent_starters.txt`, `abbrev_types.txt`
and `ortho_context.tab` under `tokenizers/punkt_tab/english/` or `spanish/`.
An empty or incomplete directory is insufficient; no runtime download is attempted.
Do not put tokens, private acquisition URLs or user identities in metadata.
Keep upstream terms and conversion/acquisition records with operator documents.
A declared license or a verified hash does not grant additional model rights.

## Import a prepared inventory without downloads

Use the service Python environment, or run from the repository with the service
source on `PYTHONPATH`. Both input and destination paths below are illustrative:

```console
PYTHONPATH=services/transcription/src python -m transcription_v2.model_import \
  /srv/model-transfer/prepared-cache /srv/exculpata-models/candidate-cache \
  --max-bytes 21474836480
```

The destination parent must exist; the destination itself must not exist, even
as an empty directory or symlink. The example imposes a 20 GiB total-copy limit,
including the manifest. Choose the bound for the actual approved inventory.
The importer:

1. Parses the bounded inventory, checks capacity and verifies source hashes.
2. Copies only declared regular files into a new owner-only directory, checking
   hashes and file identity during copying. It invokes no downloader or model
   loader and copies no unlisted credentials or ancillary files.
3. Verifies destination hashes and custom ASR snapshot structure, then publishes
   its approved manifest last. Output contains inventory metadata and a manifest
   digest, without local paths.

A failed copy may leave a partial destination for diagnosis. Without an approved
manifest it cannot admit inference. Keep it for inspection and choose a different
new target for a retry; do not overwrite an existing cache to recover.
The command's `ready` output means inventory verification, not GPU, alignment,
diarization, quality or whole-service readiness.

## Activate, update and roll back

Keep the original cache and service configuration. Stop admission and drain all
queued/running work before changing model selection: queued jobs currently store
processing profiles, not an immutable selection of a future worker's inventory.
Completed transcripts retain their recorded model identity and manifest digest.

Configure both the API and worker with the same `TRANSCRIPTION_V2_MODEL_CACHE`
and approved manifest. `TRANSCRIPTION_V2_MODEL_MANIFEST` defaults to
`approved-model-manifest.json` inside that cache. Mount model files read-only
for inference and restart both components through the deployment procedure.
Check `/v1/profiles`, then a synthetic source/translation workflow. API inventory
checks and worker snapshot checks must agree; neither substitutes for actual
model execution and output review.

The reference `stage-models.py` refuses to stage over a version 2 or unknown
inventory before acquisition. Use a separate root for a reference download;
do not rerun reference staging as a custom-model update procedure. Installer
portfolio reporting and saved reference receipts are not yet custom-model aware.

For rollback, stop admission and drain work again, restore the previous service
configuration pointing at the retained previous cache, restart API/worker and
repeat availability and synthetic checks. No existing cache is overwritten by
import. Reimporting the same inventory into another empty destination provides
a separate-target integrity check; it does not constitute a full service/data
backup restore or GPU acceptance test.

See [model configuration](MODEL_CONFIGURATION.md) for remaining alignment,
portfolio and reference-installer work, and the
[transcription README](../services/transcription/README.md) for admission limits.
