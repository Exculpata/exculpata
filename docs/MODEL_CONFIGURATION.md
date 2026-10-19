# Model choice, acquisition and compatibility

Exculpata's product direction is to let operators select and procure models
compatible with its adapters. The reference configuration gives contributors a
repeatable starting point; it is not an exclusive list of models the product
should ever support. Model selection belongs in administrator configuration,
while reviewers choose capabilities such as transcription or generated answers.

The application remains responsible for access controls, source citations,
exports, job handling and clear failure messages regardless of model choice.
Accuracy and resource requirements depend on the selected models and inputs.

## What can be configured today

| Component | Existing integration | Current limit |
|---|---|---|
| Answer generation | `CASE_INTELLIGENCE_GENERATOR_BACKEND` selects `ollama` or `openai`; `CASE_INTELLIGENCE_GENERATOR_URL` and `CASE_INTELLIGENCE_GENERATOR_MODEL` select an operator endpoint and model. `CASE_INTELLIGENCE_GENERATOR_SERVER` selects `ollama`, `llamacpp`, `lmstudio`, or `vllm` | The endpoint and model must satisfy the adapter contract in [existing answer endpoints](#existing-answer-endpoints). The installer wires bundled vLLM for `--models review` and `--models all`. `--external-generator` with `--models none` or `--models transcription` points at an operator server. A compatible-looking API is not automatically qualified. |
| Embeddings | `CASE_REVIEW_EMBEDDING_MODEL` and `CASE_REVIEW_EMBEDDING_REVISION` configure the SentenceTransformer worker | Local files are required; remote model code is disabled. Embedding dimensions, index compatibility and reindexing must be checked before changing a populated deployment. |
| Reranking | `CASE_REVIEW_RERANKER_MODEL` and `CASE_REVIEW_RERANKER_REVISION` configure the CrossEncoder worker | Local files are required; remote model code is disabled. Ranking/output behavior still needs evaluation. |
| Speech recognition | WhisperX/faster-whisper profiles use approved CTranslate2 snapshots | Version 2 inventories select exact compatible model identities for the existing profiles; [offline import](ASR_MODEL_IMPORT.md) copies prepared inventories to a new cache. Raw-model preparation and installer integration remain operator work. |
| Word alignment | Manifest-approved English/Spanish Torchaudio checkpoints | Language-to-checkpoint bindings are currently fixed. Another language/checkpoint requires adapter and inventory support. |
| Speaker diarization | Nemotron or Community-1 adapter selected during installation | These are separate implementations with their own artifact/runtime contracts. An unrelated diarization model cannot be substituted by changing a filename. |

These are application/worker configuration interfaces, not a claim that the
installer exposes every choice interactively. Review the resolved service
configuration when using an operator-managed deployment. Do not modify the
Python mapping or remove manifest checks merely to make a custom model appear
ready. Adding a new architecture may require a new adapter; a compatible model
can use the versioned ASR inventory when it satisfies the existing adapter contract.

The current model-portfolio display also assumes reference generator IDs and
retrieval metadata. Configurability of the underlying adapters does not yet
provide complete custom-model installation, readiness reporting or migration.

## Existing answer endpoints

The answer client is selected in `generator_from_environment`
([`src/case_intelligence/generation.py`](../src/case_intelligence/generation.py),
lines 771–792). An empty `CASE_INTELLIGENCE_GENERATOR_BACKEND` selects the
unavailable generator: search and source review still work, and answering does
not. The two configured backends are:

| Backend | Contract the code calls | Required settings |
|---|---|---|
| `ollama` | `GET {URL}/api/tags` and `POST {URL}/api/chat` with the answer JSON schema as `format` | `CASE_INTELLIGENCE_GENERATOR_URL`, `CASE_INTELLIGENCE_GENERATOR_MODEL`. Set `CASE_INTELLIGENCE_GENERATOR_SERVER=ollama`. |
| `openai` | `GET {URL}/v1/models` and `POST {URL}/v1/chat/completions` with nested `response_format.json_schema.schema` and `strict` true | The same URL and model keys. `CASE_INTELLIGENCE_GENERATOR_SERVER` is `vllm`, `llamacpp`, or `lmstudio`. llama.cpp must receive that nested object; a top-level `response_format.schema` is not sent. Optional `CASE_INTELLIGENCE_GENERATOR_API_KEY`, or `CASE_INTELLIGENCE_GENERATOR_API_KEY_FILE`, sends `Authorization: Bearer`. `CASE_INTELLIGENCE_GENERATOR_DISABLE_THINKING=1` sends `chat_template_kwargs.enable_thinking` false. |

`{URL}` is `CASE_INTELLIGENCE_GENERATOR_URL` after trimming a trailing slash.
The URL is an origin or path prefix. It is not a URL that already ends in
`/v1/chat/completions` or `/api/chat`. The host must be loopback
(`127.0.0.1`, `localhost`, or `::1`) or an exact name in
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

The bundled installer can point this client at an existing server.
[`scripts/exculpata_install.py`](../scripts/exculpata_install.py) accepts
`--models none|review|transcription|all`. `none` writes an empty backend unless
`--external-generator ollama|llamacpp|lmstudio` is set with
`--external-generator-url` and `--external-generator-model`. `review` and `all`
write `CASE_INTELLIGENCE_GENERATOR_BACKEND=openai`,
`CASE_INTELLIGENCE_GENERATOR_URL=http://generator:8000`, the pinned served model
name, thinking disabled, and a 180-second timeout. That URL is the vLLM service
in [`compose.yaml`](../compose.yaml) (the `generator` service). They cannot be
combined with an external server.

Compose sets `CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS` from
`EXCULPATA_GENERATOR_ALLOWED_HOSTS`, falling back to an older
`RECORDBENCH_GENERATOR_ALLOWED_HOSTS` value only when the Exculpata name is
unset, and otherwise defaulting to `generator`. The external-generator option
writes the host it just checked into `EXCULPATA_GENERATOR_ALLOWED_HOSTS`, so a
stale value cannot replace it. A non-loopback host is refused until that
allowlist contains it. Inside
the app container, loopback is the container itself, so a server on the host is
not reached by `127.0.0.1`. The installer asks again and explains that; use a
LAN address or the Docker host-gateway name with the same port. That name is
`host.docker.` followed immediately by `internal`. The app service maps
`EXCULPATA_HOST_GATEWAY_ALIAS` to the host gateway. The installer writes that
alias when the chosen host is the gateway name. The default alias is
`host-gateway.example.test`.

A name is local only when every address it resolves to is loopback, RFC1918, or
IPv6 ULA. A public address requires explicit confirmation before it is saved.
Link-local addresses, including the cloud metadata address, and unspecified
addresses are rejected instead of confirmed. Each connection uses the exact vetted numeric addresses, with the original
host retained for HTTP and TLS certificate verification. Redirects and environment
proxies are disabled. IPv6 cloud metadata and multicast addresses are rejected
even with consent. IPv6 scope identifiers do not change the numeric address used
for these policy checks. DNS lookup deadlines do not change global socket settings. One-word names and `*.local` names are not
trusted from the spelling alone. The installer recognizes its controlled Docker
host-gateway alias before Docker creates it; runtime requests still resolve and
validate that alias. Invalid URLs are asked again. Non-interactive
invalid input stops with the same explanation. `--confirm-remote-generator`
supplies the confirmation for a public host when input is not interactive. It
does not allow a link-local or unspecified address.

Startup sends one small strict-schema request. `doctor` sends the same request
unless `--dry-run` is set. Ollama is checked with native `format`. llama.cpp,
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

`doctor` and the installer also print CPU cores, RAM, GPU VRAM when a tool
reports it, and Apple Silicon when the host is Darwin arm64. The suggestion
comes from [`config/model_profiles.json`](../config/model_profiles.json). A
missing tool selects the CPU profile and says so. Nothing is downloaded.

Embeddings and reranking stay in-process. `CASE_REVIEW_MODEL_DEVICE=cpu` loads
Granite and the ModernBERT reranker on CPU. When an index already contains
vectors and the embedding model or revision changes, `doctor` and startup say
that existing indexes must be rebuilt. Exculpata does not rebuild them
automatically. An API key for the answer server is read from
`/run/exculpata-secrets/generator-api-key`.

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
configuration. Doctor probes bundled generators inside the existing app
namespace, while external probes use saved endpoint consent and the runtime's
inline-key-before-file precedence. Saved unsupported answers and compiled
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
The installer obtains selected artifacts from upstream into the operator's
model cache. Their terms remain separate from Exculpata's Apache-2.0 license.
Operators should consult the exact upstream model card and terms for their use,
including any required access approval. A download instruction does not grant
additional rights or establish suitability for every use.

Some dependencies contain artifacts inside their packages. In particular,
WhisperX includes a VAD checkpoint. Exculpata preserves its verified MIT
notice in the service package/image inputs; see [third-party notices](../THIRD_PARTY_NOTICES.md).
Describing a deployment as using operator-supplied models does not remove
notices for components actually included in an image or package.

The current alignment preparation also downloads the pinned NLTK `punkt_tab`
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
