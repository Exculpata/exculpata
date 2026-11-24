# Reviewer-gated case memory proposals from the Case Assistant (Slice 4)

## Problem

Slices 1–3 let the Case Assistant investigate discovery, show verified
calculations, cite originals, and report gaps, but findings stay trapped in
chat history. Defense teams still have to copy working-theory claims, timeline
events, contradictions, and disclosure-review leads into case memory by hand,
and saved case memory does not flow back into future assistant turns.

## Root cause

1. `src/case_intelligence/workbench_assistant.py` persists verified findings
   only inside conversation payloads without deriving reviewer-gated proposals.
2. No authorized, CSRF-protected route validates proposal source support against
   current immutable originals before writing into matter-scoped case memory.
3. `WorkbenchToolBackend.get_case_knowledge` does not return saved case-theory
   facts, disputed issues, or dated events from the matter notebook.

## Approach

Add `src/case_intelligence/case_assistant_proposals.py` (`modules.answers`) and
wire reviewer-gated proposal persistence in `workbench_assistant.py` (`app_shell`):

1. **Cited proposal builder (`build_verified_proposals`):** Derives candidate
   proposals (`case_theory`, `contradiction`, `timeline_event`, `disclosure_review`)
   from verified findings, competing accounts, and optional model-suggested
   candidates. Every proposal must cite at least one verified `SourceRef` in
   `result.citations` (and any referenced `calculation_id` must exist in
   `result.receipts`). Uncited or fabricated proposals are dropped. Every
   disclosure item uses `"Candidate for attorney review"` framing (per `#278`).
2. **Reviewer-gated persistence route (`install_assistant_proposal_routes`):**
   Registers `POST /matters/{slug}/conversations/{conversation_id}/messages/{message_id}/assistant-proposals/{proposal_id}`.
   Checks live authorization (`authorized_matter`) and CSRF (`require_csrf`),
   re-verifies every cited original under `source_store.mutation_guard()`
   (refusing with `409 Conflict` if any cited source changed or was removed),
   and saves one deduplicated, matter-scoped `NotebookItemRecord` (`origin="answer"`,
   status `suggested`/`disputed`/`needs_review`, never auto-confirmed). Because
   items live in the matter's existing notebook store, matter purge, exports,
   Reports, access controls, and conflict checks apply automatically.
3. **Read-back into assistant memory & UI cards:** `get_case_knowledge` exposes
   saved `case_theory`, `timeline`, and `issues` items to future assistant turns,
   and `templates/workbench_assistant_trace.html` renders proposal cards with
   **Save to case memory** buttons.

## Vision check

Matches `VISION.md` and `docs/IDEAL.md` E4: one assistant turns discovery into
durable team memory without ever silently writing unreviewed facts or widening
matter boundaries.

## What it could break

- `tests/test_api_snapshots.py`: adds one reviewed route in both route snapshots.
- `tests/test_import_boundaries.py`: registers `case_assistant_proposals` in
  `modules.answers` (imports only `modules.answers` + stdlib).
- `tests/test_generator_no_tools.py`: adds `case_assistant_proposals` to T7.

## Proving test

`tests/test_case_assistant_proposals.py` runs an end-to-end Case Assistant
investigation in the real FastAPI workbench and proves: (1) proposals render
in HTML without writing anything automatically; (2) uncited/fabricated proposals
are dropped; (3) saving via POST writes a deduplicated, source-cited `suggested`
item visible to future `get_case_knowledge` calls; (4) removing a cited source
causes saving that proposal to fail closed with `409`.
