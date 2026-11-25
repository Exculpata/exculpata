# Standard `openai` Generator Server Dialect

## Kickoff

- **Goal (user-visible outcome):** Keep Exculpata local-first by default while allowing operators who explicitly configure an external OpenAI-compatible Chat Completions API (hosted or self-hosted) to select `--external-generator openai` (`CASE_INTELLIGENCE_GENERATOR_SERVER=openai`), pass `./install doctor`, and run Case Assistant, cited answers, source classification, and question drafting on explicitly granted matters without weakening any egress, consent, per-matter grant, or local verification guard.
- **Expected files to touch:**
  - `specs/openai-standard-generator-dialect.md`
  - `src/case_intelligence/generator_schema.py`
  - `src/case_intelligence/generation.py`
  - `src/case_intelligence/question_generation.py`
  - `src/case_intelligence/case_assistant.py`
  - `src/case_intelligence/workbench_assistant.py`
  - `src/case_intelligence/external_generator.py`
  - `src/case_intelligence/generator_config.py`
  - `src/case_intelligence/generator_completion.py`
  - `src/case_intelligence/claim_evaluation.py`
  - `scripts/exculpata_install.py`
  - `docs/MODEL_CONFIGURATION.md`
  - `tests/test_openai_standard_dialect.py` (new synthetic regression suite) and any existing dialect assertions that enumerate `SERVERS`
- **Acceptance check:**
  1. Synthetic regression tests against a strict OpenAI-compatible HTTP server (rejecting `chat_template_kwargs`, legacy `max_tokens`, custom `temperature`, and unsupported strict JSON Schema length/array-count keywords with HTTP 400) fail on commit `307e5a4` and pass after this change for startup/`doctor` probe, cited answers, source classification, question drafting, Case Assistant tool loop, installer round-trip, and per-matter grant enforcement.
  2. Existing `ollama`, `vllm`, `llamacpp`, and `lmstudio` request shapes remain unchanged.
  3. Local Draft 2020-12 schema validation and deterministic citation/grounding checks remain enforced on every response, and all local-first egress/consent/grant checks pass.
  4. `make check-fast` (or equivalent full local check suite + static boundary/fixture/doc checks) and `scripts/publication-check.py` pass cleanly.
- **Issue link:** https://github.com/Exculpata/exculpata/issues/603

## Problem

Exculpata already provides HTTPS-only external model transport, exact host allowlists, CONNECT relay isolation, installation-level remote consent (`--confirm-remote-generator`), owner-only API key secret files, and per-matter owner/admin grants. However, `OpenAICompatibleGenerator` assumes every `CASE_INTELLIGENCE_GENERATOR_BACKEND=openai` endpoint is a local inference server (`vllm`, `llamacpp`, or `lmstudio`) and sends local-server-specific request fields that standard hosted and third-party OpenAI-compatible Chat Completions APIs reject with HTTP 400:

1. `chat_template_kwargs: {"enable_thinking": ...}` is sent unconditionally in four builders:
   - `OpenAICompatibleGenerator.generate()` (`src/case_intelligence/generation.py`)
   - `OpenAICompatibleGenerator.classify_source()` (`src/case_intelligence/generation.py`)
   - `schema_chat_request()` (`src/case_intelligence/generator_schema.py`)
   - `generate_question_draft()` (`src/case_intelligence/question_generation.py`)
2. Legacy `max_tokens` and fixed low `temperature` (`0.0`/`0.1`/`0.2`) are sent unconditionally; newer OpenAI reasoning models and several OpenAI-compatible frontier endpoints reject `max_tokens` (requiring `max_completion_tokens`) and reject custom `temperature`.
3. `response_format` with `strict: True` forwards Draft 2020-12 length/array-count constraints (`minLength`, `maxLength`, `minItems`, `maxItems`) and `const` that some strict-schema endpoints reject with HTTP 400.
4. `CASE_INTELLIGENCE_GENERATOR_SERVER` and `--external-generator` only accept `ollama`, `llamacpp`, `lmstudio`, and `vllm`.
5. `generator_schema._urllib_transport` treats HTTP 400 during the schema probe as `status="refused"`, leaving answering disabled even when the endpoint is reachable and authorized.

## Root cause

Protocol transport (`backend="openai"`, i.e., `/v1/models` and `/v1/chat/completions`) and inference-server wire dialect (vLLM/llama.cpp extensions vs. standard OpenAI Chat Completions) were conflated inside each request builder rather than owned by a single typed server-dialect specification.

## Approach

1. **Single typed wire-dialect descriptor:** Define an immutable `OpenAIWireDialect` (and lookup helper `openai_wire_dialect(server)`) in `src/case_intelligence/generator_schema.py` alongside `SCHEMA_SERVERS` and `OPENAI_SERVERS`. Add `"openai"` to `SCHEMA_SERVERS`, `OPENAI_SERVERS`, `external_generator._BACKEND`, and `generator_config.generator_server`.
   - `vllm`, `llamacpp`, `lmstudio`: `send_chat_template_kwargs=True`, `token_limit_field="max_tokens"`, `send_temperature=True`, `strip_strict_schema_bounds=False`, `always_reserve_reasoning_tokens=False`. (Preserves existing local behavior byte-for-byte.)
   - `openai`: `send_chat_template_kwargs=False`, `token_limit_field="max_completion_tokens"`, `send_temperature=False`, `strip_strict_schema_bounds=True`, `always_reserve_reasoning_tokens=True`.
2. **Reasoning-token headroom on `openai`:** Because the `openai` dialect cannot disable model-side reasoning via `chat_template_kwargs`, include the existing fixed reasoning allowance (`PROBE_REASONING_TOKENS` / `DEFAULT_REVIEW_BUDGET.reasoning_tokens`) in `max_completion_tokens` for the schema probe, answers, source classification, and question drafting so reasoning models do not exhaust a 64- or 400-token ceiling before emitting visible JSON. Non-reasoning models stop normally once the short JSON object completes; local response-size and schema length limits still bound output.
3. **Wire-safe strict schema projection without relaxing local validation:** Add `openai_wire_schema(schema, server=...)` so the `openai` dialect removes strict-mode keywords rejected by hosted providers (`minLength`, `maxLength`, `minItems`, `maxItems`) from the outbound `response_format` copy while `complete_structured()` continues to validate every response locally against the full Draft 2020-12 schema. Use `{"type": "string", "enum": [PROBE_MARKER]}` in `PROBE_SCHEMA` (equivalent single-value constraint accepted across local and hosted servers).
4. **Apply the dialect consistently across all callers:**
   - `generator_schema.schema_chat_request`
   - `generation.OpenAICompatibleGenerator.generate` and `.classify_source`
   - `question_generation.generate_question_draft`
   - `case_assistant.OpenAIAssistantClient` (with `schema_server` forwarded from `workbench_assistant.assistant_client_from_generator`)
   - `claim_evaluation.request_settings` (read `max_completion_tokens` or `max_tokens`, and handle omitted `chat_template_kwargs`)
5. **External availability check timeout:** In `generator_completion.generator_available`, allow up to `min(client.timeout, 10.0)` seconds for HTTPS endpoints (retaining `2.0` seconds for local HTTP) so cold TLS handshakes through the CONNECT relay do not spuriously report an available server as offline.
6. **Preserve all local-first and egress invariants:** Do not alter HTTPS enforcement, exact host allowlists, CONNECT relay routing, `--confirm-remote-generator`, per-matter owner/admin grants, `unvalidated model` notices, or fail-closed `RecordedDispatch` (`/tokenize` remains vLLM-only). Keep docs neutral and focused on operator choice.

## What it could break

- Existing tests asserting exact `SERVERS` / `OPENAI_SERVERS` tuples or rejecting `"openai"` as a server name (must verify all installer, BYOM, and schema tests).
- Existing `vllm`, `llamacpp`, `lmstudio`, and `ollama` request-body assertions (mitigated by keeping their dialect flags identical to current behavior).
- `tests/test_import_boundaries.py` if any cross-layer imports are introduced (mitigated by keeping the dialect table inside `generator_schema.py`, which existing callers already import).
- `tests/test_documentation.py` and `scripts/publication-check.py` when updating `docs/MODEL_CONFIGURATION.md` and `scripts/exculpata_install.py`.

## Proof

1. Failing-first synthetic regression suite (`tests/test_openai_standard_dialect.py`) running a real local HTTP server that enforces the strict standard OpenAI Chat Completions contract (HTTP 400 on `chat_template_kwargs`, `max_tokens`, `temperature`, or unsupported strict JSON Schema keywords):
   - Verified failing on commit `307e5a4`.
   - Passes after the change for `probe_schema_enforcement`, `OpenAICompatibleGenerator.generate`, `OpenAICompatibleGenerator.classify_source`, `OpenAICompatibleGenerator.draft_questions`, `OpenAIAssistantClient.complete_turn` / `CaseAssistant`, `claim_evaluation.request_settings`, installer round-trip (`--external-generator openai --confirm-remote-generator`), and per-matter grant denial/grant enforcement.
   - Verifies local Draft 2020-12 schema rejection still triggers when a model violates `maxLength` / `maxItems` despite the stripped wire schema.
   - Verifies `vllm`, `llamacpp`, `lmstudio`, and `ollama` retain their exact existing request payloads.
2. Full relevant test suites, import-boundary / fixture-write / documentation checks, and `scripts/publication-check.py`.
