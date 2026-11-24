# Case assistant multi-turn tool loop and premise/derivation verifier (Slice 2)

## Problem

With Slice 1 (`case_assistant_tools.py`), Exculpata has read-only matter tools
and deterministic `CalculationReceipt`s, but no assistant loop that drives those
tools across discovery or verifies answers combining raw facts with computed math.

## Root cause

1. `src/case_intelligence/generation.py:278` enforces `refuse_tool_request` on
   all chat calls, preventing multi-turn discovery tool loops.
2. `src/case_intelligence/generation.py:899-901` drops any claim with a number
   absent verbatim from raw text, discarding derived calculations.
3. `src/case_intelligence/generation.py:55` caps answers at `MAX_ANSWER_CLAIMS = 8`.

## Approach

Add `src/case_intelligence/case_assistant.py` (`modules.answers`) stacked on
Slice 1, keeping existing grounded answering (`generation.py`) unchanged:

1. **Scoped, fail-closed tool transport:** Keep `_bounded_json_request` defaulting
   to `refuse_tool_request` (preserving all existing egress pins) and add
   `_bounded_assistant_request` requiring `validate_assistant_tool_payload`, which
   allows only `ALLOWED_TOOLS` function definitions and rejects legacy keys or
   unknown tools with `ToolRequestRefused` before serialization or transport.
2. **Multi-turn loop (`CaseAssistant.investigate`):** Supports OpenAI-compatible
   (`OpenAIAssistantClient`) and Ollama (`OllamaAssistantClient`) chat-with-tools
   endpoints behind `AssistantModelClient`, with live cancellation, stage updates,
   tool self-correction on `AssistantToolError`, and immediate abort on
   `AssistantAccessError`.
3. **Premise + derivation verifier (`verify_assistant_answer`):** Verifies every
   citation against inspected `runtime.cited_refs`, every `calculation_id` against
   `runtime.receipts`, and every finding number against EITHER cited raw excerpts
   OR referenced `CalculationReceipt` values—preserving valid math while omitting
   hallucinated numbers/citations and surfacing unreadable-file and budget gaps.

## Vision check

Matches `VISION.md`: one assistant choosing searches, reads, and calculations;
never drops computed numbers or caps findings at 8; flags gaps honestly;
preserves T7 static no-code-execution.

## What it could break

- `tests/test_generator_no_tools.py`: T1–T5 remain unchanged on legacy paths;
  T6 enforces the structural `_bounded_assistant_request(..., payload_validator=validate_assistant_tool_payload)`
  contract (no filename allowlist); T7 adds `case_assistant` and `case_assistant_tools`.
- `tests/test_import_boundaries.py`: registers `case_assistant` in `modules.answers`.

## Proving test

`tests/test_case_assistant.py` runs `CaseAssistant.investigate` on the rendered
405-document `stock_case_gold` corpus and proves: (1) multi-turn tool calls
produce a verified `> 10%` finding backed by `CalculationReceipt` + citations and
surface the unreadable scan gap; (2) fabricated citations, hallucinated numbers,
revoked access, and non-allowlisted tools fail closed; (3) OpenAI and Ollama
adapters parse tool calls via fake transport.
