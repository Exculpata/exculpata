# Model choice, acquisition and compatibility

A fresh installation starts with no model endpoints. A team can import and tag
documents, review sources, run local scanned-PDF OCR and search by exact text.
Operators can connect the capabilities below independently. The installer
offers an optional answer server and skips bundled model staging and hardware
tier selection on a fresh node. [Installation guide](INSTALL.md#hardware-and-capability-profiles)

The application remains responsible for access controls, source citations,
exports, job handling and clear failure messages regardless of model choice.
Accuracy and resource requirements depend on the selected models and inputs.

## What can be configured today

In the selected application environment file, append `URL` and `MODEL` to
the prefix for each capability you configure. For example, speech recognition uses
`CASE_INTELLIGENCE_ASR_URL` and `CASE_INTELLIGENCE_ASR_MODEL`. Review the
endpoint-specific credential, host, HTTPS relay and pin settings in the linked
contracts. Run `./install doctor --root <node-root>` after applying changes.

| Capability | Environment prefix | What the app sends and receives |
|---|---|---|
| Answer generation | `CASE_INTELLIGENCE_GENERATOR_` | Sends the question and cited passages to Ollama, llama.cpp, LM Studio or vLLM and validates the structured answer. [Answer endpoints](#existing-answer-endpoints) |
| Embeddings | `CASE_INTELLIGENCE_EMBEDDING_` | Sends passage or query text to `/v1/embeddings`. The index requires 768-dimensional vectors and a compatible recorded identity. [Retrieval endpoints](#optional-embedding-and-rerank-endpoints) |
| Reranking | `CASE_INTELLIGENCE_RERANK_` | Sends a query and candidate passages to `/v1/rerank` and ranks matches using the returned relevance scores. [Retrieval endpoints](#optional-embedding-and-rerank-endpoints) |
| Speech recognition | `CASE_INTELLIGENCE_ASR_` | Sends the recording to `/v1/audio/transcriptions` and receives source-language text with timestamped segments. [Transcription contract](#check-a-speech-recognition-api) |
| Word alignment | `CASE_INTELLIGENCE_ALIGNMENT_` | Sends the recording, source language and timed text when word alignment is needed. Returned word times refine segment citations. [Alignment contract](SPEECH_ENRICHMENT_APIS.md#alignment) |
| English translation | `CASE_INTELLIGENCE_TRANSLATION_` | Sends source-language segment text and language information for English translation. Reviewers see the translation alongside the original. [Translation contract](SPEECH_ENRICHMENT_APIS.md#translation) |
| Speaker labeling | `CASE_INTELLIGENCE_DIARIZATION_` | Sends the recording and segment times and receives anonymous speaker labels. Reviewers can assign names in the matter. [Speaker contract](SPEECH_ENRICHMENT_APIS.md#speaker-labels) |

Scanned-PDF OCR runs locally with Poppler and Tesseract, including with all
seven endpoints unset. [Installed OCR check](OCR_AND_MEDIA.md#check-scanned-pdf-ocr-on-an-installed-node)

Use the [local synthetic setup](LOCAL_SPEECH_APIS.md) to check all four speech
contracts through the installed app and relay. Approval to send case material
is separate for each matter, capability, endpoint and model.
[Matter approval](#matter-approval-for-external-model-services)

Existing nodes can resume their saved bundles. Their private speech worker uses
WhisperX/faster-whisper with approved CTranslate2 snapshots, English/Spanish
Torchaudio alignment checkpoints and the saved Nemotron or Community-1 diarizer.
For migration, qualify the replacement APIs, finish or cancel pending private
jobs and, if retaining semantic search, verify or rebuild the embedding index
before retiring the bundles.
Follow the [saved-node migration procedure](INSTALL.md#move-an-existing-bundled-node-to-model-apis).
The [prepared ASR inventory guide](ASR_MODEL_IMPORT.md) describes offline import
for a retained worker.

The model-portfolio display assumes reference generator IDs and retrieval
metadata. Use the installed contract checks to verify the endpoints configured
for this node.

## Existing answer endpoints

The answer client is selected in `generator_from_environment`
([`src/case_intelligence/generation.py`](../src/case_intelligence/generation.py),
lines 771–792). An empty `CASE_INTELLIGENCE_GENERATOR_BACKEND` selects the
unavailable generator: search and source review still work, and answering does
not. The two configured backends are:

| Backend | Contract the code calls | Required settings |
|---|---|---|
| `ollama` | `GET {URL}/api/tags` and `POST {URL}/api/chat` with the answer JSON schema as `format` | `CASE_INTELLIGENCE_GENERATOR_URL`, `CASE_INTELLIGENCE_GENERATOR_MODEL`. Set `CASE_INTELLIGENCE_GENERATOR_SERVER=ollama`. |
| `openai` | `GET {URL}/v1/models` and `POST {URL}/v1/chat/completions` with nested `response_format.json_schema.schema` and `strict` true | The same URL and model keys. `CASE_INTELLIGENCE_GENERATOR_SERVER` is `vllm`, `llamacpp`, or `lmstudio`. llama.cpp must receive that nested object; a top-level `response_format.schema` is not sent. Optional `CASE_INTELLIGENCE_GENERATOR_API_KEY`, or `CASE_INTELLIGENCE_GENERATOR_API_KEY_FILE`, sends `Authorization: Bearer`. `classify_source` and internal extraction always send thinking false with `CASE_INTELLIGENCE_GENERATOR_DISABLE_THINKING` set to either `0` or `1`; answer calls enable thinking. |

`{URL}` is `CASE_INTELLIGENCE_GENERATOR_URL` after trimming a trailing slash.
The URL is an origin or path prefix. It is not a URL that already ends in
`/v1/chat/completions` or `/api/chat`. External endpoints must use HTTPS,
for example `https://answers.example.test:8443`. Only the bundled
`http://generator:8000` endpoint permits HTTP. The host must be an exact name in
`CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS`
([`src/case_intelligence/service_endpoints.py`](../src/case_intelligence/service_endpoints.py),
lines 16–49 and 53–77). Usernames, passwords, queries, and fragments are
rejected. The model name must be non-empty and at most 200 characters, and it
must appear in the service's tag or model list before `available` is true.

Request limits from the same module and
[`src/case_intelligence/review_budget.py`](../src/case_intelligence/review_budget.py)
lines 14–17:

- Timeout defaults to 90 seconds (`CASE_INTELLIGENCE_GENERATOR_TIMEOUT`) and is clamped to 1–300 seconds.
- Ordinary answers request at most 1,200 output tokens. Ollama also sends `num_ctx` 8192.
- Each cited passage is capped at 6,000 characters, with at most 12 synthesis inputs and 48,000 evidence characters.
- A response larger than 1 MiB is rejected.
- Question drafting uses `CASE_INTELLIGENCE_GENERATOR_QUESTION_PROFILE` and is a separate policy. See [question drafts](QUESTION_DRAFTS.md).

Fresh installs leave generation disabled until an operator configures an answer
server. Use `--external-generator ollama|llamacpp|lmstudio|vllm` with
`--external-generator-url` and `--external-generator-model`. Omit `--models`
or use `--models none`. Existing nodes with the historical review bundle keep
their saved private vLLM origin and model when resumed or updated.
[Installer](../scripts/exculpata_install.py)

Compose sets `CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS` from
`EXCULPATA_GENERATOR_ALLOWED_HOSTS`, falling back to an older
`RECORDBENCH_GENERATOR_ALLOWED_HOSTS` value only when the Exculpata name is
unset, and otherwise defaulting to `generator`. The external-generator option
writes the host it just checked into `EXCULPATA_GENERATOR_ALLOWED_HOSTS`, so a
stale value cannot replace it. The app uses the internal companion transport for
`http://generator:8000`. All external requests tunnel through the explicitly
configured `CASE_INTELLIGENCE_GENERATOR_PROXY` relay. Ambient proxy variables
and `NO_PROXY` cannot bypass it; redirects are refused. TLS hostname and
certificate verification stay in the app, end to end. The relay never decrypts.
Generator, embedding, and rerank tunnels each allow up to 32 MiB across both
directions combined and last up to 310 seconds.

A server bound to host loopback (`127.0.0.1:11434`, `localhost`, or `::1`) cannot
be reached by the containerized app. There is no host-gateway alias or alternate
route. Expose an operator-managed HTTPS endpoint reachable by the relay, with a
trusted certificate. HTTP is rejected in interactive and non-interactive install,
endpoint parsing, and environment settings construction.

To use a private vLLM server with `--models none`, replace the example HTTPS
origin and served model name below. Enter the exact private IP you reviewed for
that origin. The relay must be able to reach the address and the application
must trust the server's TLS certificate.

```bash
vllm_url=https://answers.example.test:8443
read -r -p 'Reviewed private IP for the vLLM server: ' vllm_ip
vllm_pins=$(printf '{"issuer":"%s","addresses":["%s"]}' "$vllm_url" "$vllm_ip")
./install --models none --external-generator vllm \
  --external-generator-url "$vllm_url" --external-generator-model synthetic-answer \
  --generator-egress-pins "$vllm_pins"
```

Private RFC1918/ULA destinations require reviewed pins bound to the complete
generator URL. Supply all reviewed addresses if DNS returns more than one.
Public destinations require `--confirm-remote-generator` or interactive
confirmation because questions, matter text, and cited passages can leave the
installation. This consent applies across the installation.

Pins are saved as `CASE_INTELLIGENCE_GENERATOR_EGRESS_PINS`. Installer Compose
commands derive `GENERATOR_EGRESS_URL` and `GENERATOR_EGRESS_PINS` from the selected
app env file, ignoring shell overrides. Empty configuration denies the generator
slot. Recreate the relay and app through the installer after changing the saved
URL or pins. Each relay connection resolves DNS and validates the entire answer
set before dialing only those numeric addresses. Every answer must match explicit
pins when supplied; otherwise only public addresses are permitted. Loopback,
link-local, metadata, multicast, unspecified, reserved and transition addresses
remain denied even with pins or remote consent. The app retains consent and
destination checks using literal addresses and reviewed pins. The relay resolves
external names because app DNS is isolated.

Generator values containing `$` are rejected before writing, when reading retained
env files, and at app settings construction. This intentionally uses resolved
literals instead of adding Compose escape/decode semantics that differ between
generated and operator-maintained files. API key file contents are not Compose
environment values. Relay URL and pin values also reject `$`.

Ordinary assistant answer calls default to thinking on, independently of
`CASE_INTELLIGENCE_GENERATOR_DISABLE_THINKING`, including when `thinking=None`.
The remaining purpose of that variable is documented in
[question drafts](QUESTION_DRAFTS.md). Internal extraction, including source
classification (`classify_source`) and saved review criteria, always sends thinking
false with `CASE_INTELLIGENCE_GENERATOR_DISABLE_THINKING` set to either `0` or `1`.
Extraction requests use at most 400 output tokens, retaining any smaller frozen
review limit, with no reasoning allowance. Neither a constructor nor a call setting
can enable extraction thinking. An explicit `thinking` argument can override the
answer default. OpenAI-compatible requests carry
`chat_template_kwargs.enable_thinking`; Ollama requests carry `think`.
These defaults are code settings, with no model-profile override.
Only `message.content` is parsed as the result; reasoning fields are not included
in answers or saved review decisions. The visible answer allocation remains
1,200 tokens, with a separate fixed 4,096-token reasoning allowance added to
the request limit when thinking is enabled.

Startup sends a strict-schema request with answer thinking enabled. Schema
status is cached separately for each model and thinking state. Extraction's
state is checked on first use, and periodic refreshes retain separate state.
Thinking probes add the same reasoning allowance to their 64-token output limit.
`./install doctor` sends its synthetic, non-thinking schema request from the
running app container. It uses the configured relay and credentials for an
external server and the companion transport for a bundled server. Use
`--dry-run` to skip the request. Ollama
is checked with native `format`. llama.cpp,
LM Studio, and vLLM are checked with nested `json_schema.schema`. If the body
does not match, generation validates locally and retries, then refuses the
answer. The first probe asks for a value the schema rejects, so a model that
only follows the prompt is not reported as enforced. The doctor line and the
administrator page say `verified`, `not enforced / validated locally`,
`refused`, or `not checked`. Every response is also validated locally, including
recorded-context dispatches and replies from a verified server. Each recorded
repair has its own admission and dispatch receipts. Periodic schema refreshes
run once in the background and return cached status to pages and health checks.
HTTP rejection or malformed response bodies report refusal, not network failure.

A saved review bundle can continue to run its private retrieval worker.
`CASE_REVIEW_MODEL_DEVICE=cpu` runs its Granite embedding model and ModernBERT
reranker on CPU. When an index already contains
vectors and the embedding model or revision changes, `doctor` and startup say
that existing indexes must be rebuilt. Exculpata does not rebuild them
automatically. An API key for the answer server is read from
`/run/recordbench-secrets/generator-api-key` on the existing read-only secrets mount.

A populated index with missing, unreadable, incomplete or non-populated
provenance stays **unknown**. The app does not stamp today's configuration onto
existing vectors. Doctor warns when provenance cannot be established; startup
uses the actual global vector count when available. A single document upload,
including an empty document, cannot relabel or clear global index provenance.
This tracking code does not attest a new or rebuilt index automatically: until
a trusted rebuild records the producing model/revision, an unknown index keeps
its warning. No automatic rebuild or migration is performed.

Ollama and OpenAI-compatible clients use the same configured inline-key-before-
file precedence. Ollama forwards the credential on availability checks, schema
probes, answers, source classification and question drafting; it is never put in
the URL. Generator redirects and ambient proxies remain disabled.

The unconditional metadata deny set also covers the documented
[Alibaba ECS metadata service](https://www.alibabacloud.com/help/en/ecs/user-guide/view-instance-metadata/),
including IPv4-mapped IPv6 forms and hostnames resolving to that destination.
Remote consent cannot permit it.

Qualification requires a passing record for both the model ID and a runtime-attested
immutable SHA-256 revision. A mutable name or tag cannot inherit a pass. Current
BYOM adapters do not attest the served revision, so their answers remain labeled
`unvalidated model` in the workspace and exports, including rejected answers.
Every-source review records also lack immutable model attestation: all their
CSV, JSON, Markdown and Word exports conservatively retain that label, independent
of the currently configured model. This does not claim historical qualification
that was never recorded. No storage migration is introduced.

[`config/model_qualifications.json`](../config/model_qualifications.json) retains
revision-scoped records. A future attested adapter may supply the exact revision
to `qualification_for(model_id, revision=...)` or `qualification_label`; the harness
records passes through `apply_harness_result` with the same digest. A pinned
catalog ID alone is not a measured pass.

The wheel includes the registry and its schema under `case_intelligence/config`,
loaded with package resources. The repository `config/` copies are editable
mirrors: update both copies together when recording a reviewed qualification;
the parity regression rejects divergent contents. Installed applications do not
depend on the source checkout. Current BYOM adapters still lack revision
attestation and therefore cannot inherit a recorded pass.

Compose passes the same effective embedding model and revision to the app and
retrieval worker so populated-index mismatch warnings reflect the worker's
configuration. Saved unsupported answers and compiled
reports retain the qualification notice from their generating client even if
the active model later changes.

There is no Open WebUI setting or adapter. An Open WebUI process is usable
only if it serves the `openai` contract above at `{URL}/v1/...` and the host
is allowed. This repository does not establish that. MLX, a bundled llama.cpp
container, automatic model downloads, and whisper.cpp are not part of this
version. User-chosen models keep their own license terms.

## What Exculpata distributes or acquires

The source repository and reference model catalog contain configuration and
download references, not the main ASR/generator/embedding/diarization weights.
For an existing bundled installation, staging obtains selected artifacts from
upstream into the operator's model cache. Their terms remain separate from Exculpata's Apache-2.0 license.
Operators should consult the exact upstream model card and terms for their use,
including any required access approval. A download instruction does not grant
additional rights or establish suitability for every use.

Some dependencies contain artifacts inside their packages. In particular,
WhisperX includes a VAD checkpoint. Exculpata preserves its verified MIT
notice in the service package/image inputs; see [third-party notices](../THIRD_PARTY_NOTICES.md).
Describing a deployment as using operator-supplied models does not remove
notices for components actually included in an image or package.

Alignment preparation for the bundled worker also downloads the pinned NLTK `punkt_tab`
resource automatically. Its catalog entry explicitly says
`UPSTREAM-TERMS-REVIEW-REQUIRED`. Upstream lists Punkt resources among those with
[unclarified dataset licensing](https://github.com/nltk/nltk_data/blob/gh-pages/DATASET-LICENSES.md).
This is separate from the NLTK software license. Review that dependency before
selecting alignment; no broad permission or automatic approval is implied.
A clearer resource acquisition/consent flow and local import path are tracked
below. This does not prevent contributing to the source or evaluating workflows
that do not require that resource.

## Reference configurations and operator-selected models

Record a model's role, adapter, upstream identity/revision or local content
digest, file inventory, terms link and acquisition method. Integrity checks
establish which bytes execute; a hash is not a judgment about licensing or
quality. A model supplied by an operator still needs compatible files, enough
resources and an administrator-approved inventory.

Publish observed results for exact reference configurations, with their
limitations. Other configurations can be experimental without pretending that
the project has tested every model. Do not advertise accuracy, hardware minimums
or throughput based only on unit tests or upstream model benchmarks. Preserve
source playback and human review of generated transcripts and answers.

## Focused contribution work

1. Extend the implemented [ASR bindings and prepared-cache import](ASR_MODEL_IMPORT.md)
   to operator-friendly raw-model preparation and installer integration, then
   alignment configuration. Keep model files verified and inference offline.
   The application portfolio still needs to reflect configured generator/retrieval
   choices; ASR API/result metadata already records selected identities. Job
   requests must not supply arbitrary model paths or executable code.
2. Separate acquiring reference artifacts from importing an existing local
   model inventory. Show the selected resources and upstream terms before
   acquiring them; unresolved terms must be explicit. Do not claim a universal
   importer beyond its tested prepared-inventory scope. Keep credentials out of
   manifests and logs, and keep runtime inference downloads disabled.
3. Run actual reference and alternative-model workflows. Synthetic ASR binding,
   rejection, restart, provenance and separate-target import tests establish the
   configuration contract, not model inference or language quality. A new
   architecture gets separate adapter tests.

Changing embeddings may require a new index; changing a transcription model
must not relabel historical transcripts as if the new model produced them.
See [installation](INSTALL.md), [release readiness](RELEASE_READINESS.md) and
the [contribution guide](../CONTRIBUTING.md).

Evaluation capture remains distinct from accepting an answer. The fixed synthetic
claim-evaluation harness records a schema-rejected JSON object for independent
grading after the normal validator rejects it. That capture performs one attempt
and does not hide repair drafts. Ordinary generation validates and bounds repair
attempts, and the workflow evaluator checks model identity around each actual
request, including schema repairs.

## Matter approval for external model services

Installation settings establish which endpoints the app can contact. Before
sending case material, open the matter's setup page and review the capability,
endpoint and model. The matter owner or a workspace administrator can approve
generation, embeddings, reranking, transcription, word alignment, translation
and speaker labeling independently for that matter. New matters start without grants.
Existing grants remain scoped to their approved capability and destination.
Changing the endpoint or model requires fresh approval, including a change to the generation server's path prefix.
Once Exculpata observes a change, restoring an earlier endpoint also requires
fresh approval. Matter archives exclude these deployment-specific grants.

Revoke approval in matter setup to stop subsequent requests to that destination.
This includes requests made by a job queued before revocation. Intake, source
review and word search remain available. An upload indexed without embeddings
can be found by word search. To fill missing vectors later, use the separately
approved installation-wide rebuild described below. The startup and diagnostic
probes use synthetic inputs and can run before any matter has granted access.
The standalone question-drafting and defense-workflow evaluations can send
their verified, fixed synthetic corpora to the configured generator. Use a
bundled generator for custom evaluation inputs that have no matter approval.

## Check a speech recognition API

To check an operator-managed speech service, add its HTTPS origin and served
model name to the selected app environment file. For example, a private speech
service with a reviewed address could use the following settings. Replace the
example values with your service's origin, model, host and pin.

```dotenv
CASE_INTELLIGENCE_ASR_URL=https://speech.example.test
CASE_INTELLIGENCE_ASR_MODEL=synthetic-asr
CASE_INTELLIGENCE_ASR_ALLOWED_HOSTS=speech.example.test
CASE_INTELLIGENCE_ASR_EGRESS_PINS={"issuer":"https://speech.example.test","addresses":["<reviewed-private-IP>"]}
```

Replace `<reviewed-private-IP>` with the private IP reachable from the relay. For an operator-approved
public service, set `CASE_INTELLIGENCE_ASR_REMOTE_CONFIRMED=1` and omit the private
pin. If the service requires a bearer token, set `CASE_INTELLIGENCE_ASR_API_KEY_FILE`
to the path of the credential in the app's read-only secrets mount. The app must
trust the service's TLS certificate. Apply the settings to both the app and
the egress proxy using the installer.

Run `./install doctor --root <node-root>` or use the diagnostic directly inside the app container.

```sh
python -m case_intelligence.speech_endpoint
```

The check posts a short synthesized WAV to `/v1/audio/transcriptions` with the
configured model and `response_format=verbose_json`. A compatible service returns
source-language text and timed segments in seconds. The JSON result reports
`{"transcription": "ok"}` when the contract passes. Use the returned diagnostic
to correct the configuration, credentials or response format and retry.

The doctor always uses the installed synthetic fixture. To transcribe a matter's
recordings, its owner or an administrator must approve **Transcription** for the
displayed origin and model in matter setup. The queued worker checks that grant
and the requesting member's access immediately before it streams the original
audio or video. It sends a generic filename and the model settings. The response
must contain source-language text and timed segments within the recording. The app
checks the grant again before storing the response. Revocation during processing
leaves the recording available for retry. The app assigns the citation IDs and
keeps imported transcripts and reviewer edits.

When an approved API is unreachable, the job records a retryable failure. Restore
the endpoint or grant, then choose **Try again**. An interrupted request can
submit the same source again until import succeeds. An existing temporary
private job finishes on the private service. Installations with no configured
ASR API can continue using their private service. Check recognition quality on
representative recordings before retiring a previously qualified model.

## Optional embedding and rerank endpoints

Fresh nodes use word search until a retrieval endpoint is configured. On a
saved review bundle, the private retrieval worker can continue serving its
configured roles. In the selected app env file, set `CASE_INTELLIGENCE_EMBEDDING_URL=https://models.example.test` and
`CASE_INTELLIGENCE_EMBEDDING_MODEL=synthetic-embedding` to opt into an
OpenAI-compatible `/v1/embeddings` server. Independently configure
`CASE_INTELLIGENCE_RERANK_URL` and `CASE_INTELLIGENCE_RERANK_MODEL` for a
Cohere/Jina-style `/v1/rerank` server (`model`, `query`, `documents`; response
`results` with `index` and `relevance_score`). Both must list their model at
`GET /v1/models`. URLs are HTTPS origins, without `/v1`.

At startup, the app checks each configured endpoint independently with a model
list request and synthetic text. A failed startup check or subsequent API failure disables that role for the running app. The local
worker can supply roles without a configured endpoint. When only reranking is
ready, it orders lexical search results. With only embeddings, vector similarity
can improve the order of word matches. A result based on meaning alone requires
both the existing similarity threshold and a passing model rerank score.
Newly indexed passages without embeddings remain available
to word search, with their embedding column set to SQL `NULL`. Restart the app
after repairing an endpoint to refresh readiness. If a server fails during indexing or search, the app keeps the text and continues with the available search capabilities.

For each prefix (`CASE_INTELLIGENCE_EMBEDDING` or `CASE_INTELLIGENCE_RERANK`),
set `_ALLOWED_HOSTS` to the exact host, `_REMOTE_CONFIRMED=1` for a public
server, or `_EGRESS_PINS` to reviewed private pins as for the answer server.
Optional `_API_KEY` takes precedence over `_API_KEY_FILE`; use the existing
read-only secrets mount for files. Keys stay in request headers, never index
records or diagnostics. `_TIMEOUT` defaults to 120 seconds (maximum 300).
The reference Compose file supplies `_PROXY`; direct deployments must supply
an explicit CONNECT relay. There is no automatic remote fallback. An approved
embedding or reranking request can send extracted text, queries or candidate
passages to the operator-selected server. Review its retention policy before
granting access to a matter.

Invalid saved retrieval settings leave Compose `ps`, `logs`, `stop` and `down`
available with retrieval relay slots cleared. Commands that need egress settings
report the affected `CASE_INTELLIGENCE_EMBEDDING_` or `CASE_INTELLIGENCE_RERANK_`
configuration for correction before running. Unvalidated URLs and pins never
reach the relay.

Run `python -m case_intelligence.retrieval_endpoints` inside the app namespace
to repeat the synthetic checks. The embedding endpoint can pass while the
stored index still needs a rebuild. The index requires 768-dimensional vectors
and a recorded endpoint and model identity. Rebuild when that identity is
missing or changes, or when rows are missing vectors after a partial build.
Populated indexes created before this migration also need a one-time rebuild.
Earlier versions could record a model name alongside deterministic fallback
vectors. The new format records that the projection was initialized empty or
explicitly rebuilt. Exact search stays available during migration.
The rebuild processes the entire installation. Keep a backup and use the app's
environment, PostgreSQL access and index-record mount for the commands below.

For an external embedding endpoint, preview the destination, model and complete
list of affected matter IDs. The preview reads IDs locally and makes no API calls.
Keep the file on the operator's machine because it identifies matters and their
intended destination.

```sh
python -m case_intelligence.retrieval_rebuild --plan > rebuild-plan.json
```

Review the file and confirm each matter permits sending its indexed text to that
endpoint and model. If every matter permits it, stop the app and all index
writers, then supply that reviewed file as approval for this rebuild.

```sh
python -m case_intelligence.retrieval_rebuild --confirm-stopped --approved-plan rebuild-plan.json
```

The command checks the plan again under the index lock before reading passage
text. A change to the destination, model or set of matters requires a fresh
preview and review. Each embedding request contains passages from one matter.
Approval applies to this rebuild operation. For the bundled worker, stop all
writers and run `python -m case_intelligence.retrieval_rebuild --confirm-stopped`.
After a successful rebuild, restart the app.
The command replaces derived vectors transactionally, preserving source and
citation identities; it does not alter the index dimension. It records the
configured endpoint/model identity, not an attestation of remote model weights.
Changing the weights behind the same model name requires an explicit rebuild.
The old identity is invalidated before committing replacement vectors. If the
final identity write fails, keep writers stopped and rerun the rebuild.

### Retain compatible bundled embeddings

A populated, complete index from the current learned-vector format can migrate
to an operator endpoint without resending passages. Keep a backup, stop the
app and all other index writers, and leave the original private retrieval
worker running. Use the upgraded worker image with the original pinned model
revision. Its health response must identify the loaded embedding model and
revision recorded for this index. Configure the new embedding origin, model
and relay alongside the existing worker URL. From the app environment with
the index record mounted writable, run:

```sh
python -m case_intelligence.retrieval_rebuild --confirm-stopped --verify-migration
```

The command locks the projection and checks for missing embeddings. Both
services receive only the same three public synthetic sentences. All three
768-component embeddings must be distinct, nonzero, and bit-identical at the
single precision used by pgvector. On a match the command atomically records
the new endpoint identity, producing worker revision and sample fingerprint. The source rows,
vectors and citation IDs stay in place. Restart the app and run the retrieval
diagnostic. Each authorized embedding batch for a transferred index performs
one extra synthetic request to check the fingerprint before case text is
sent. Existing matter permissions still apply to the new destination.

A missing producing worker, older projection format, changed model, unavailable
endpoint, or differences from numerical precision can require the approved
rebuild above. Word search remains available. Matching a fixed sample checks
compatibility for that sample and cannot identify arbitrary replacement weights.

## Canary run

Exculpata is local-first by default. On a GPU host with Docker Compose, the
NVIDIA runtime, `nvidia-smi`, the pinned generator image already present, and
staged model weights, run this optional synthetic check from the checkout:

```sh
EXCULPATA_MODEL_CANARY=1 EXCULPATA_CANARY_OUTPUT=canary-receipt.json \
  .venv/bin/python -m pytest -q -o faulthandler_timeout=0 tests/test_model_canary_live.py
```

Set `EXCULPATA_MODEL_ROOT` to the staged cache. To select another model, set
`EXCULPATA_GENERATOR_MODEL_ID`, `EXCULPATA_GENERATOR_REVISION`, and
`EXCULPATA_GENERATOR_SERVED_NAME` before running. Nothing is pulled. Without
opt-in, a model root, a local pinned image, or a usable GPU host, the test skips.
The capture is named `canary-capture.jsonl` beside the receipt and the receipt
records its relative path and SHA-256. Without an output setting, both artifacts
go in pytest's temporary directory.

The receipt is informational and gates nothing: model enablement, configuration
validation, release checks, and CI do not require it. A passing receipt records
300 completed calls (20 synthetic prompts, 15 rounds), zero captured hits, and
both DNS and on-subnet TCP negative controls. Tool-shaped answer text is counted,
not treated as a network call. `upper_bound_95 = 3/N` appears only on a passing
receipt, labeled **for this suite on this build only**; failed or invalid
receipts have a null bound. A clean receipt covers one model, revision, and image.

Capture starts after health readiness and covers the prompt suite and controls.
A clean capture records no connection started and no outside lookup seen,
not an absence of all attempts. Off-subnet connects on the internal network send
no frame and cannot be observed. IGMP/MLD frames abort capture, as do other
unsupported packet forms; missing controls or packet loss invalidate it. The
trap does not reassemble packets or decode encoded honeytokens. The app can
reach approved destinations through its egress proxy, and an operator can
configure an external answer server. This run makes no broader outcome promise.
