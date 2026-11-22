# Richer Case Assistant discovery tools: tables, email threads, transcripts (Slice 5)

## Problem

Real criminal-defense discovery is not just prose PDFs: financial records and
call detail records arrive as CSV/TSV tables, communications arrive as multi-part
email threads, and body-cam/jail-call recordings arrive as long timestamped
transcripts. Reading only raw page chunks forces the model to do error-prone
manual filtering and miss replies or surrounding spoken context.

## Root cause

`src/case_intelligence/case_assistant_tools.py` only exposes generic
`list_sources`, `search_discovery`, `read_source`, `get_case_knowledge`, and
`calculate`, with no structured table filtering/aggregation, email-thread
traversal, or media transcript time-window inspection.

## Approach

Add three read-only, strictly allowlisted tools to `ALLOWED_TOOLS` in
`case_assistant_tools.py` and implement them in `WorkbenchToolBackend`:

1. **`query_table`:** Parses CSV/TSV source text via stdlib `csv.DictReader`,
   filters rows by column equality/substring or exact `Decimal` `min`/`max`
   range, and optionally computes exact `Decimal` column aggregates (`count`,
   `sum`, `min`, `max`), recording a source-cited `CalculationReceipt`.
2. **`read_email_thread`:** Given an authorized email `document_id`, reuses
   `source_email_threads.read_eml` and `group_threads` across the matter's
   authorized emails to return the full chronological thread and index each
   message as a canonical `WorkbenchCitation`.
3. **`read_transcript_window`:** Given an authorized transcript `document_id`
   and `[start_seconds, end_seconds]`, filters parsed `PilotUnit` cues by
   `start_ms`/`end_ms`, returning exact timestamps, speaker labels, and text.
4. Update `CaseAssistant.investigate` to capture inspected texts from all three
   tools so `verify_assistant_answer` verifies their citations and math.

## Vision check

Fits `VISION.md`: deepens the one assistant's tools instead of adding separate
modes; keeps exact `Decimal` math via `CalculationReceipt`s; stays read-only,
local-first, and zero-exec (T7).

## What it could break

- `tests/test_case_assistant_tools.py`: updates expected allowlisted tool set.
- `tests/test_generator_no_tools.py`: T6/T7 remain enforced with zero exec.

## Proving test

`tests/test_case_assistant_rich_tools.py` uploads synthetic wire CSVs and a
3-message EML thread into a real `WorkbenchService`, plus a timestamped
transcript fixture, and proves: (1) `query_table` filters and sums exact wire
amounts with a `CalculationReceipt`; (2) `read_email_thread` returns all 3
linked emails; (3) `read_transcript_window` returns only cues in `[60s, 180s]`;
(4) scope violations and invalid arguments fail closed.
