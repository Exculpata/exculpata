# Graduate Case Assistant to default-on for tool-capable model endpoints

## Kickoff

- Goal restated: Enable the single `VISION.md` Case Assistant by default for OpenAI-compatible and Ollama model endpoints (and generated node installations) without requiring an opt-in environment flag, while preserving safe zero-endpoint fallback, explicit opt-out (`CASE_INTELLIGENCE_CASE_ASSISTANT=0`), and existing test-suite single-turn generator contracts.
- Expected files: `specs/case-assistant-default-on.md`, `src/case_intelligence/workbench_assistant.py`, `src/case_intelligence/workbench.py`, `scripts/exculpata_install.py`, `docs/CASE_ASSISTANT.md`, `tests/test_case_assistant_default_on.py`.
- Acceptance check: Synthetic unit and HTTP tests prove (1) with `CASE_INTELLIGENCE_CASE_ASSISTANT` unset and `CASE_INTELLIGENCE_TEST_MODE` unset, OpenAI-compatible and Ollama endpoints run Case Assistant by default; (2) explicit `CASE_INTELLIGENCE_CASE_ASSISTANT=0` (or `false`/`off`/`no`) cleanly opts out to single-turn grounded generation; (3) `UnavailableGenerator` (zero-endpoint installations) never crashes when `CASE_INTELLIGENCE_CASE_ASSISTANT=1` or default-on is active; (4) an explicit `AssistantModelClient` runs Case Assistant without setting `CASE_INTELLIGENCE_CASE_ASSISTANT=1`; (5) `test_case_assistant_default_on.py`, `test_workbench_assistant.py`, `test_generation_matter_grants.py`, `test_answer_context.py`, `test_bring_your_own_model.py`, `test_oss_installer.py` (selected), `test_import_boundaries.py`, and `test_generator_no_tools.py` pass.
- Issue link: [VISION.md](../VISION.md) (following Case Assistant Slices 1-7, #585-#591).

## Problem and root cause

After Case Assistant Slices 1-7 landed (`#585`-`#591`), `case_assistant_enabled()` in `workbench_assistant.py` remained gated behind opt-in `CASE_INTELLIGENCE_CASE_ASSISTANT=1`, and `scripts/exculpata_install.py` did not emit the setting. As a result, real installations with configured OpenAI-compatible or Ollama endpoints still fell back to the legacy single-turn 12-passage Q&A path unless operators manually discovered and set an undocumented variable.

## Approach

1. Add `supports_case_assistant(client)` in `workbench_assistant.py` returning `True` for `AssistantModelClient` (`complete_turn`), `OpenAICompatibleGenerator`, and `OllamaGenerator`, and `False` for `UnavailableGenerator` or non-tool backends.
2. Update `case_assistant_enabled(client=None)` so:
   - Explicit `CASE_INTELLIGENCE_CASE_ASSISTANT` truthy (`1`/`true`/`yes`/`on`) enables Case Assistant whenever `client is None` or `supports_case_assistant(client)` (protecting zero-endpoint installs).
   - Explicit falsy or unrecognized non-empty `CASE_INTELLIGENCE_CASE_ASSISTANT` disables it cleanly.
   - When unset: explicit `complete_turn` clients run Case Assistant; `CASE_INTELLIGENCE_TEST_MODE=1` preserves legacy single-turn test harnesses; production/non-test runtimes enable Case Assistant by default for tool-capable endpoints.
3. Pass `getattr(self.generator, "client", None)` into `case_assistant_enabled(...)` in `workbench.py`.
4. Emit `"CASE_INTELLIGENCE_CASE_ASSISTANT": 1` in `scripts/exculpata_install.py` generated `app.env` values and document default-on behavior and opt-out in `docs/CASE_ASSISTANT.md`.

## Vision check

Directly completes `VISION.md` ("One assistant. Attorneys prompt in plain words.") by making the single Case Assistant the real default experience instead of an opt-in experiment, without weakening any egress, matter-grant, or citation verification boundary.

## What it could break

- Zero-endpoint installations (`UnavailableGenerator`) when `CASE_INTELLIGENCE_CASE_ASSISTANT=1` is written to `app.env`: mitigated by `supports_case_assistant(client)`.
- Existing single-turn generator tests under `tests/conftest.py` (`CASE_INTELLIGENCE_TEST_MODE=1`): preserved automatically unless they opt in or pass a `complete_turn` client.
- `selected_snapshot` (`RecordedDispatch` vLLM `/tokenize` audit contract): remains intact on its dedicated path.

## Proving test

`tests/test_case_assistant_default_on.py` tests default-on activation for OpenAI/Ollama endpoints outside test mode, explicit `0`/`false`/`off` opt-out, zero-endpoint `UnavailableGenerator` safety with `CASE_INTELLIGENCE_CASE_ASSISTANT=1`, and HTTP end-to-end execution with an `AssistantModelClient` without setting any env var.
