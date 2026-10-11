# Preview and acquire reference models

Exculpata supplies adapters and a pinned reference configuration. Operators
choose and obtain compatible models under their upstream terms. The application
license does not grant rights to separately acquired models or tokenizer data.
A different model architecture may need a different adapter; see
[model configuration](MODEL_CONFIGURATION.md) and [ASR import](ASR_MODEL_IMPORT.md).

The acquisition container keeps both the Hugging Face Hub cache and its Xet
download cache under the writable model mount. Its root filesystem stays
read-only. A missing auxiliary-cache location can otherwise interrupt a fresh
download even when the model snapshot directory is writable. Runtime model
services retain their separate offline and read-only model boundaries.
Acquisition defaults to streaming HTTP with `HF_HUB_DISABLE_XET=1` to avoid the
larger memory demand of concurrent Xet reconstruction. This changes the download
transport, not selected revisions, artifact verification or runtime inference.
Operators may enable Xet in their own staging override after qualifying its
memory requirements; its auxiliary cache must remain writable.

After building the acquisition image, contributors can exercise this boundary
without downloading models or allowing network access:

```sh
RECORDBENCH_STAGER_TEST_IMAGE=recordbench/model-stager:dev \
  python -m pytest tests/test_model_stager_container.py -q
```

Use the locally built image's actual tag when it differs. This optional test
requires Docker and never pulls an image; it uses a fresh synthetic model mount.

Before downloading, from the checkout run this standard-library-only command:

```sh
python3 scripts/stage-models.py plan --catalog config/models.json \
  --groups review,transcription-asr,transcription-alignment-en,transcription-alignment-es,transcription-diarization \
  --review-profile portable --diarization-backend nemotron
```

This prints selected model IDs, exact revisions, declared licenses, direct
artifact URLs and the pinned `punkt_tab` resource. It needs no model packages,
credentials, network or existing model directory. Hugging Face model cards and
license files are available at `https://huggingface.co/<model_id>/tree/<revision>`;
direct-file origins appear in the plan. Review those sources for your use.

The `punkt_tab` catalog entry deliberately says
`UPSTREAM-TERMS-REVIEW-REQUIRED`: the NLTK code license does not establish this
data archive's terms. Choosing to acquire it is an operator decision, not a
Exculpata certification that its terms are suitable. If unsuitable, do not
acquire it or claim the reference alignment path is qualified; a compatible
replacement requires separate implementation and validation.

The installer shows the acquisition plan before model staging. Interactive
transcription setup asks for one acknowledgement; unattended setup requires
`--accept-model-terms`. The acknowledgement covers the selected models and
resource data. Nemotron remains ungated: no hub account, token or separate
upstream access approval is needed. Community-1 additionally requires its
upstream access approval and `--hf-token-stdin`.

Direct stager users pass the same acknowledgement. An already acquired exact
Punkt archive can replace that resource download:

```sh
python3 scripts/stage-models.py stage --catalog config/models.json \
  --model-root /srv/recordbench/models \
  --groups transcription-asr,transcription-alignment-en,transcription-alignment-es,transcription-diarization \
  --diarization-backend nemotron --accept-model-terms \
  --punkt-archive /path/to/punkt_tab.zip
```

This direct command needs the stager's locked Python environment and still
acquires selected models. The supplied ZIP must match the catalog hash; it
cannot silently replace the tokenizer with arbitrary data. The containerized
installer uses the catalog download. To stage locally before installation,
use exactly the installer selection and preserve its complete receipt. For a
fully offline prepared ASR inventory, follow the separate ASR import guide.

`verify` remains read-only and offline. A resumed installation with an unchanged,
verified selection skips acquisition and acknowledgement. Changed/missing
artifacts need acquisition again. The receipt binds the catalog, selected
modules and every staged file. An acknowledgement does not waive hash checks,
replace model provenance, or permit reference staging over a custom inventory.

For offline model loading, compiler-cache requirements and prepared-node resume,
see [runtime startup](RUNTIME_STARTUP.md).
