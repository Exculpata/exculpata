# Case Assistant

Exculpata's **Case Assistant** implements the single-assistant workflow described in
[VISION.md](../VISION.md): attorneys prompt in plain words, and one matter-scoped
assistant decides how to search discovery, read originals, query structured files,
compute exact math, cite every source, and flag where the record has gaps—without
forcing reviewers to choose a processing mode.

## Architecture and boundaries

The Case Assistant runs inside the application shell (`modules.answers`) and obeys
every [ideal architecture contract](IDEAL.md) invariant:

- **Application authority (A1):** Every tool call checks live
  `authorize(matter_id, actor_id)` before reading data and enforces the active
  matter and selected source-set scope. Model output never grants access, mutates
  records silently, or executes arbitrary code.
- **Per-matter model grants (L1):** External model calls require both an operator-configured
  HTTPS endpoint behind the CONNECT relay and an explicit owner/admin matter grant
  (see [model configuration](MODEL_CONFIGURATION.md)).
- **Traceable, verified output (E2/E3):** Premise and derivation checks verify
  raw factual premises against cited originals and computed values against
  exact `CalculationReceipt`s instead of dropping computed numbers. Automated
  verification does not prove legal or semantic correctness;
  [review each claim against the originals](GENERATED_ANSWER_REVIEW.md).

Implementation modules:

- [`src/case_intelligence/case_assistant_tools.py`](../src/case_intelligence/case_assistant_tools.py) —
  matter-scoped read-only tool runtime, exact `Decimal`/date calculator, and
  `CalculationReceipt` audit trail.
- [`src/case_intelligence/case_assistant.py`](../src/case_intelligence/case_assistant.py) —
  multi-turn OpenAI/Ollama tool-calling loop and premise/derivation verifier.
- [`src/case_intelligence/workbench_assistant.py`](../src/case_intelligence/workbench_assistant.py) —
  workbench integration, live plain-language stage labels, and expandable work trace.
- [`src/case_intelligence/case_assistant_proposals.py`](../src/case_intelligence/case_assistant_proposals.py) —
  reviewer-gated case-memory proposals and read-back.
- [`src/case_intelligence/case_assistant_eval.py`](../src/case_intelligence/case_assistant_eval.py) —
  live synthesis evaluation runner against the 405-document synthetic stock-case suite.

## Matter-scoped read-only tools

The assistant selects from eight fixed, read-only, allowlisted tools (unknown
tools, cross-matter reads, unauthorized actors, and dynamic code execution fail closed):

| Tool | Purpose |
| --- | --- |
| `list_sources` | Enumerate matter sources, status, and unreadable/failed files so record gaps are surfaced honestly. |
| `search_discovery` | Run matter-scoped discovery search across indexed passages and return traceable references. |
| `read_source` | Read exact original source spans with source/version identity preserved. |
| `get_case_knowledge` | Read saved reviewer-confirmed case memory (case theory, entities, assertions, timelines) for the matter. |
| `calculate` | Execute exact `Decimal` and calendar-date operations (`sum`, `subtract`, `multiply`, `divide`, `percent`, `days_between`, `running_balance`) with zero `eval`/`exec`, returning a source-cited `CalculationReceipt`. Division by zero and `NaN` fail closed. |
| `query_table` | Filter, sort, and inspect structured CSV/spreadsheet tables within the matter without loading whole workbooks into prose. |
| `read_email_thread` | Read chronologically ordered messages across a matter email thread with attachment-coverage notices preserved. |
| `read_transcript_window` | Read timestamped windows around media transcript segments with speaker labels and provenance preserved. |

## Work trace and live stages

While the assistant works, the UI renders plain-language live stage labels
(searching discovery, reading sources, running calculations, checking support)
and stores an expandable **work trace** beside the answer so reviewers can inspect
every tool call, source read, `CalculationReceipt`, and record gap that informed
the response.

## Reviewer-gated case-memory proposals

The assistant can read confirmed matter memory via `get_case_knowledge` and emit
cited inline proposal cards for four case-memory categories:

- `case_theory`
- `contradiction`
- `timeline_event`
- `disclosure_review`

Proposals are never committed automatically. An authorized reviewer inspects the
cited originals and explicitly saves or dismisses each proposal inline (preserving
[E4 — never silently overwrite reviewed work](IDEAL.md#evidence-integrity-and-human-work)).

## Synthetic synthesis evaluation

`case_assistant_eval.py` exercises the real multi-turn Case Assistant against the
synthetic 405-document stock-case benchmark (combining filings, brokerage
statements, corporate splits/buybacks, emails, spreadsheets, transcripts, and
deliberately unreadable files) to score cross-document synthesis, exact math
receipts, source citations, and honest gap reporting against an operator-configured
model endpoint. See [eval](EVAL.md) and the design notes under `specs/case-assistant*.md`.
