# Disclosure-review (`Brady`/`Giglio`) candidate workflow for attorney review (#272)

## Kickoff

- **Goal (user-visible outcome):** Surface cited passages inside the single Case Assistant and matter notebook workflow that may warrant attorney review as potential exculpatory or impeachment candidates across four neutral categories (`inconsistent_statement`, `witness_benefit`, `alternate_suspect`, `credibility_issue`), strictly framed as **candidates for review** (never asserting legal duty or violation), with reviewer actions to save, mark reviewed, dismiss, and export with original citations.
- **Expected files to touch:**
  - `specs/disclosure-review-candidates.md`
  - `src/case_intelligence/case_assistant_proposals.py`
  - `src/case_intelligence/case_assistant.py`
  - `src/case_intelligence/workbench_assistant.py`
  - `src/case_intelligence/work_product_exports.py`
  - `src/case_intelligence/templates/workbench_assistant_trace.html`
  - `docs/CASE_ASSISTANT.md`
  - `tests/test_disclosure_review_candidates.py`
- **Acceptance check:**
  1. Synthetic fixture tests prove all four disclosure-review categories (`inconsistent_statement`, `witness_benefit`, `alternate_suspect`, `credibility_issue`) are flagged with original citations and neutral reasons, while benign/negative passages produce zero flags and uncited/fabricated/conclusive-wording candidates are rejected.
  2. Static string test over UI templates, export builders, proposal helpers, and docs confirms `"candidate for review"` framing and zero prohibited conclusive legal-duty/violation phrases.
  3. End-to-end synthetic HTTP test proves inline proposal rendering, saving as `needs_review`, marking reviewed (`confirmed`), dismissing (`dismissed`), and exporting Markdown/DOCX/CSV with original citations and `"candidate for review"` notice.
  4. Eval harness test exercises disclosure-review candidate flagging end-to-end through `CaseAssistant`.
  5. New tests fail on base commit `6a4a6c1` and pass after implementation; `tests/test_case_assistant_proposals.py`, `tests/test_case_assistant.py`, `tests/test_case_assistant_ui.py`, `tests/test_disclosure_review_candidates.py`, `tests/test_generator_no_tools.py`, `tests/test_import_boundaries.py`, `tests/test_source_size_ratchet.py`, `ruff`, and `scripts/publication-check.py --skip-history` pass.
- **Issue link:** https://github.com/Exculpata/exculpata/issues/272

## Problem and root cause

`case_assistant_proposals.py` defines `disclosure_review` and `DISCLOSURE_NOTICE`, and `workbench_assistant.py` provides a single "Save to case memory" button, but:
1. `CaseAssistant` does not pass model-emitted `proposals` through to `build_verified_proposals`, nor is there deterministic category classification/detection for the four core attorney-review candidate categories (`inconsistent_statement`, `witness_benefit`, `alternate_suspect`, `credibility_issue`) with negative-fixture coverage.
2. There is no fail-closed runtime or test guard blocking prohibited conclusive phrases ("is Brady material", "Brady violation", "disclosure was required", etc.).
3. Inline proposal cards only support saving (`needs_review`), not direct reviewer-gated "Mark reviewed" or "Dismiss" actions, and notebook exports do not carry a dedicated `"candidate for review"` notice when disclosure-review candidates are exported.

## Approach

1. **`src/case_intelligence/case_assistant_proposals.py`:**
   - Define `DISCLOSURE_SUBTYPES` (`inconsistent_statement`, `witness_benefit`, `alternate_suspect`, `credibility_issue`) with neutral candidate-for-review labels, `PROHIBITED_DISCLOSURE_PHRASES`, and `has_prohibited_disclosure_wording(text)`.
   - Add `subtype: str = ""` to `AssistantProposal` (default empty for backward compatibility).
   - Add `detect_disclosure_candidates(refs, inspected_texts)` to conservatively flag seeded passages across the four categories with exact `SourceRef` citations, neutral category/reason, and zero flags without a verified citation.
   - Update `build_verified_proposals` to reject any `disclosure_review` proposal containing prohibited conclusive wording (`has_prohibited_disclosure_wording`) and optionally merge deterministic cited candidates when `inspected_texts` is provided.
2. **`src/case_intelligence/case_assistant.py` & `src/case_intelligence/workbench_assistant.py`:**
   - Carry verified `raw_proposals` and `inspected_texts` on `AssistantInvestigationResult` (optional defaults so existing callers and tests stay untouched), and forward them into `build_verified_proposals` in `_result_to_answer`.
   - Extend `save_assistant_proposal` in `install_assistant_proposal_routes` to accept an optional `decision` form field (`"save"` -> `needs_review`, `"review"` -> `confirmed`, `"dismiss"` -> `dismissed`) so attorneys can save, mark reviewed, or dismiss inline under CSRF + matter authorization + original-integrity checks (updating existing saved notebook items cleanly if already saved).
3. **`src/case_intelligence/templates/workbench_assistant_trace.html` & `src/case_intelligence/work_product_exports.py`:**
   - Render neutral category labels, cited source badges, and inline **Save candidate**, **Mark reviewed**, and **Dismiss** controls on proposal cards.
   - Include an explicit `"Candidate for attorney review"` notice in `notebook_blocks` (`work_product_exports.py`) whenever exported entries include `needs_review` or disclosure-review candidates, keeping exports strictly non-conclusive and citation-backed.
4. **`docs/CASE_ASSISTANT.md`:** Document the four neutral disclosure-review candidate categories, non-conclusive language contract, and review/dismiss/export workflow.

## Vision check

Strictly honors [VISION.md](../VISION.md) ("Never add a mode or a single-use feature. Make the one assistant better instead.") and [IDEAL.md](../docs/IDEAL.md) (`A1`, `E2`, `E4`): zero new modes, every flag requires an inspected original citation, human review remains distinct from machine suggestions, and prohibited legal-conclusion phrasing fails closed.

## What it could break

- `tests/test_case_assistant_proposals.py` and `tests/test_case_assistant_ui.py`: existing proposal fields, idempotency, and HTML markers (`Save to case memory`, `data-assistant-proposals`, `data-assistant-proposal-form`) must remain preserved.
- `tests/test_source_size_ratchet.py`: keep `work_product_exports.py` within its recorded line baseline (2,267 lines) and `case_assistant_proposals.py` well under 1,500 lines.
