# ADR 0001: Core and modules

## Status

Proposed

## Context

Exculpata is local-first by default. The [product north star](../PRODUCT_NORTH_STAR.md)
and [core evolution plan](../CORE_EVOLUTION.md) call for shared evidence and access
contracts with incremental feature extraction. This ADR records the proposed
boundaries for that work; it changes no runtime behavior or storage.

At this documentation baseline, `src/case_intelligence/workbench.py` has 16,749
lines and imports 94 distinct internal modules. `workspace_store.py` has 9,432
lines and is imported by 56 modules. The internal import graph has one strongly
connected component of 34 modules and five smaller components of two modules each:

- `embedding_index` and `retrieval_endpoints`.
- `pilot_uploads` and `pilot_source_access`.
- `matter_archive` and `case_lifecycle_support`.
- `workbench` and `app_context`.
- `question_draft_evaluation` and `question_explanations`.

These counts come from an AST walk of all Python files under
`src/case_intelligence`, resolving absolute and relative imports at every scope,
including function-level imports. Dependencies are distinct importing/imported
module pairs, including package initializers; imported symbols are attributed to
the module named by the import unless they name an existing submodule. Line counts
include blank and comment lines. Cyclic components here have more than one module.

## Decision

The core owns shared authority, records and lifecycle contracts. Feature modules
use those contracts. The application shell composes them. The following map
names target boundaries using current files; most core packages remain an
extraction target, while the registry now mounts its first module.
Names below are relative to `case_intelligence`
unless another path is given. Specific entries such as `timeline_exports` take
precedence over broader families such as `timeline_*`.

| Layer | Responsibility and current code families |
| --- | --- |
| `core.storage` | Records, SQL, migrations and the shared unit of work: `workspace_store`, `store/*`, `store_records`, `sqlite_store`, `repositories`. No feature imports. |
| `core.access` | Identity and authorization: `identity*`, `local_account*`, `local_login*`, `case_oidc`, `oidc_*`, `team_groups`, `isolation`, `header_policy`. |
| `core.lifecycle` | Retention, deletion, recovery and durable jobs: `case_lifecycle*`, `lifecycle`, `matter_archive*`, `managed_storage`, retention/purge paths, `jobs`. |
| `core.sources` | Intake, extraction, original references and exact search: `ingestion`, `pilot_uploads`, `pilot_source_access`, `intake_receipts`, `malware_scan`, `*_preflight`, `derived_text`, `text_records`, `source_*`, `pdf_*`, `docx_*`, `ocr_*`, `exact_search*`, `source_find`. |
| `core.contracts` | Shared validation and stable naming: `contracts`, `legacy_names`, `compat_env`, `branding`, the stdlib-only `modules` contract (`ModuleContext`, `RouteHook`, `PurgeHook`), and the core citation-resolution contract (`cite()`). |
| Module: discovery_review | `modules/discovery_review`: authorized document list, open-item views, saved list views, the field definition reader and the derived-state purge hook. |
| Module: transcription | `services/transcription`, `media_*`, `transcript_search`, `video_frame_ocr`. Transcription already has a separate service. |
| Module: retrieval | `retrieval*`, `embedding_index`, `unit_stream`. |
| Module: answers | `generation`, `generator_*`, `external_generator`, `answer_*`, `question_*`, `ask_router`. |
| Module: insight | `entity_*`, `assertion_*`, `timeline_*`, `evidence_graph*`, `briefing*`, `witness_inconsistency`, `case_theory`, `production_summary*`. |
| Module: workflows | `full_text_*`, `hierarchical_synthesis`, `workflow_jobs`, `review_*`, `investigation_planner`, `deeper_investigation`. |
| Module: deliverables | `work_product_exports`, `report_*`, `full_review_exports`, `timeline_exports`, `model_export_notes`. |
| Evals, development only | `defense_eval`, `tier_evals`, `evaluation`, `*_gold`, `review_bench*`, `slice1_*`, `claim_evaluation`. |
| Application shell | `workbench`, `app_context`, `cli`, `modules.registry`, routes and composition. The only runtime layer permitted to import all layers. |

The core retains matter authorization, source identity, citations, human versus
machine provenance, durable work, retention, exports and audit authority. Modules
must preserve the [ideal architecture contract](../IDEAL.md#architecture-boundaries-and-supported-paths)
and use narrow repositories on the authorized transaction. Separately deployable
workers do not gain independent authority over those records.

## Allowed edges

An arrow means an import dependency from the left side to the right side.

| Edge | Rule |
| --- | --- |
| Module -> core | Allowed through explicit shared contracts. |
| Application shell -> any layer | Allowed for composition. |
| Evals -> any layer | Allowed for development only; runtime code must not depend on evals. |
| Core -> module | Forbidden. |
| Module -> application shell | Forbidden. |
| Module -> another module's internals | Forbidden. |

Cross-module composition belongs in the application shell, using explicit
interfaces and injected dependencies. This ADR does not grant an unrestricted
cross-module import exception. Core dependencies must preserve a single
transaction owner and the invariants below. Existing violations remain migration
work; documenting them does not make them the target architecture.

## Root of the big cycle

Storage imports feature code that in turn imports storage. `workspace_store`
imports `full_text_review`, `full_text_synthesis` and `answer_context`, alongside
`intake_receipts`, `matter_context_repository`, `source_roles`, `case_oidc` and
`appearance`. `store.knowledge_services` imports `entity_repository`,
`assertion_repository` and `matter_purpose`. Returning dependencies include
`generation` and `identity` importing `workspace_store`.

Some of these dependencies cross between proposed core areas; the feature
imports are the forbidden direction that the extraction must remove. Storage
should own records and SQL. Feature types and orchestration move to their owning
module or behind narrow protocols injected by the application shell. Repositories
must reuse the authorized connection and transaction lock, preserving source
guards and atomic final saves. Moving imports into functions does not remove a
static dependency or resolve this cycle.

## Core invariants

- Source bytes are immutable evidence after ingestion and hashing. Derived text
  and indexes remain separate; changed originals invalidate support.
- Every citation resolves through the core to a stored source/version and exact
  offset or source locator. The proposed `cite()` contract denotes that shared
  boundary, not a new API implemented by this ADR. Generated text is not evidence.
- Core authorization runs before module code receives matter data or performs
  work. Modules cannot grant access or widen source scope.
- Every deletion or re-versioning goes through the core lifecycle, preserving
  explicit confirmation, cancellation, recovery and attempt fencing. See the
  [matter lifecycle](../CASE_LIFECYCLE.md) and its
  [formal model](../../formal/case_exit_purge/README.md).
- When a module degrades, coverage notices and omissions remain visible in saved
  work and exports. Processing coverage does not establish recognition quality
  or certify an absence of relevant material.
- Machine proposals remain distinct from human decisions. Authorized mutations
  retain revision checks and cannot silently replace reviewed work.

## Extension mechanism

Choose **A, an internal registry**, as the first implementation direction:
`case_intelligence.modules` defines the shared contract; `modules.registry` in
the application shell holds a fixed, reviewed list of `Module` objects. The first
registered module lives at `case_intelligence/modules/discovery_review`. One
generic step in `create_workbench_app` mounts route hooks with a frozen
`ModuleContext` and existing authorization and response-lease helpers. The context
borrows the application connection and lock, membership and source-store access,
storage root, scanner and scan mode. Its capacity closure accounts for pending
upload and playback reservations under the shared reservation lock; this slice
has no caller or render writes. One generic step in `execute_matter_purge` invokes
purge hooks after clip cleanup and records storage failure if a hook raises.
No packaging change or dynamic discovery is required.

Keep the `Module` contract compatible with **B, Python entry points**, for later
out-of-tree extensions: `[project.entry-points."exculpata.modules"]` could be read
with `importlib.metadata`. No such entry points exist today; packaging discovery
would not relax core authority or the allowed edges.

**C, Compose profiles**, remains the service-level mechanism for transcription,
retrieval and generation. Service composition does not replace the in-process
module interfaces. Provider changes retain artifact identity, provenance and the
acceptance requirements of [model policy](../MODELS.md).

## Consequences

There is no big-bang move. Extract one boundary per move-only PR, keeping the
[route and store API snapshots](../../tests/test_api_snapshots.py) unchanged.
Keep behavior changes separately reviewable. Preserve persisted jobs, saved work,
exports and recovery compatibility before removing delegating entry points.
Schema changes require their own migration and clean-restore evidence.

Small cycles provide bounded starting points, followed by removing feature
imports from storage. Each extraction must preserve the protected transaction
and demonstrate the real composed workflow. Smaller files alone are not evidence
of improved boundaries. No code is moved by this decision.

## Enforcement

PR 2, after this ADR merges, will add the import-boundary ratchet inside the
existing application suite. It will map every scanned module to a layer, record
existing forbidden edges and cyclic component sizes, and reject new forbidden
edges, growth of the largest component or new components with more than one
module. Removing an edge will require shrinking the baseline. Synthetic probes
will establish rejection of new edges and cycles and acceptance of removals.
This is planned enforcement, not a check supplied by this documentation PR.

Existing [source line-count ceilings](../../tests/snapshots/source_line_counts.json)
remain in force, including 16,921 for `workbench.py` and 10,838 for
`workspace_store.py`. They constrain growth, not dependency direction. Human
review still checks evidence integrity, transaction ownership, module authority
and the user-visible outcome under the [review policy](../QUALITY_GATES.md#review-policy).
