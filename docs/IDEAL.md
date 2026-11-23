# Ideal Exculpata

A criminal-defense team brings discovery into one local workspace and gets
source-backed understanding while retaining control of its evidence and work.
This is a compact change-review contract, not a claim of complete implementation.
The rule table distinguishes executable checks from remaining review obligations.

Read [architecture](ARCHITECTURE.md) for current behavior,
[security](SECURITY_MODEL.md) for trust boundaries, and the
[product north star](PRODUCT_NORTH_STAR.md) for direction and known limits.

## Architecture boundaries and supported paths

**A1 — One application authority.** The app owns matter access, source identity,
citations, audit, jobs, exports and deletion. SQLite owns control records;
PostgreSQL/pgvector is a rebuildable retrieval projection. Retrieval workers rank
evidence, the generator proposes text, and transcription workers derive segments.
Workers and generated text never grant access or become source evidence.

**A2 — Compose through shared boundaries.** Routes call feature services;
services use narrow repositories and model adapters. Repositories reuse the
authorized store transaction; adapters do not import routes or own permissions.
New modules must not open a second authority or split protected transactions.
Layer imports are checked against [core evolution](CORE_EVOLUTION.md);
transaction boundaries across existing routes still require review.

**A3 — Reuse the supported path for each task.** Intake uses the authorized
[upload and receipt flow](INTAKE_RECEIPTS.md); exact enumeration uses the
[shared exact-search service](EXACT_SEARCH_RESULTS.md); case questions use the
[Case Assistant](CASE_ASSISTANT.md) and [grounded generation](../src/case_intelligence/generation.py).
Whole-text review uses the [durable range ledger](FULL_TEXT_REVIEW.md);
synthesis uses the [shared engine and adapter](FULL_TEXT_SYNTHESIS.md).
Reports use the [shared store and revision checks](REPORT_EDITING.md), and
bundle inspection/download use the [same preparation helper](EXPORT_READINESS.md).
New callers preserve these contracts rather than invent parallel state or parsers.

## Local first and explicit network authority

**L1 — No implicit outbound runtime calls.** Evidence processing stays local by default.
Operator-configured HTTPS model APIs (up to seven roles) require exact host allowlists,
reviewed private IP pins or remote consent, and per-matter owner/admin grants through
the constrained CONNECT relay (shared with OIDC). The signature updater and legacy
staging retain outbound access; operator overlays require review. See
[the reference egress contract](NETWORK_EGRESS.md) and [boundary tests](../tests/test_reference_network_boundary.py).

**L2 — Pin and validate model boundaries.** Operator model APIs require validated
HTTPS endpoints, model checks, and explicit matter grants; legacy bundled models use
pinned local revisions with offline read-only mounts. Tokens belong to owner-only
secret files, never Git or logs. Follow [model policy](MODELS.md).

**L3 — Keep the trusted-proxy boundary.** Only the gateway publishes a port,
bound to loopback by default. Clear client-supplied identity headers; authentication,
CSRF and live matter authorization remain application checks. A proxy header alone
cannot grant access. Follow [authentication](AUTHENTICATION.md).

## Evidence integrity and human work

**E1 — Preserve admitted originals.** Once ingested and hashed, source bytes are
immutable evidence. Derived text and indexes stay separate; changed bytes or source
versions invalidate support instead of silently rebinding it. Integrity checks
detect changes; they do not make operator-controlled storage physically immutable.

**E2 — Ground every supported answer in original spans.** Accepted factual claims
retain exact source/version locators; unsupported output is omitted or abstains.
Generated summaries and reviewer context are not substitute evidence. Citation and
text checks do not prove meaning or completeness; [human review remains necessary](GENERATED_ANSWER_REVIEW.md).

**E3 — Keep derivation traceable.** Derived results retain source/extraction identity
and applicable model/runtime revision records. Missing historical provenance stays
explicit; never invent a pin or describe declared metadata as runtime attestation.
Coverage, omissions and uncertainty travel with saved work and exports.

**E4 — Never silently overwrite reviewed work.** Human decisions remain distinct
from machine proposals. Shared edits check displayed revisions inside authorized
transactions; stale edits and late workers cannot replace newer accepted work.

## Rule enforcement

The [Quality gates](QUALITY_GATES.md) run the linked tests in their applicable suites.
Review-only entries identify gaps, not permission to skip existing checks.

| Rule | Enforced by |
| --- | --- |
| A1 | [Matter isolation](../tests/test_milestone_a_workbench.py), [hybrid retrieval isolation](../tests/test_review_bench_v2.py); ownership of new services is review only because no global authority checker exists. |
| A2 | [Import boundaries](../tests/test_import_boundaries.py); transaction boundaries across routes remain review only because static imports do not verify SQLite/Postgres transaction scopes. |
| A3 | [Case Assistant tools](../tests/test_case_assistant_tools.py), [exact search](../tests/test_exact_search_results.py), [full-text review](../tests/test_full_text_review.py), [export readiness](../tests/test_export_readiness.py); reuse by new callers is review only because no duplicate-path detector exists. |
| L1 | [Network boundary and inventory](../tests/test_reference_network_boundary.py), [live synthetic egress](../tests/test_egress_proxy_live.py), [matter model grants](../tests/test_generation_matter_grants.py); operator overlays and host services remain review only because repository tests cannot attest deployed configuration. |
| L2 | [Model staging](../tests/test_operator_model_staging.py), [offline retrieval startup](../tests/test_review_bench_v2.py), [manifest integrity](../services/transcription/tests/test_model_manifest.py); license, asset completeness and representative model quality are review only because fixtures cannot attest deployment acceptance. |
| L3 | [Real gateway header stripping](../tests/test_gateway_nginx.py), [authentication entry points](../tests/test_auth_entrypoints.py), [network topology](../tests/test_reference_network_boundary.py). |
| E1 | [Source/version resolution](../tests/test_source_references.py), [changed-original archive rejection](../tests/test_matter_archive.py), [review content basis](../tests/test_review_content_basis_regressions.py). |
| E2 | [Grounded generation](../tests/test_generation.py), [exact source locators](../tests/test_source_references.py); semantic correctness is review only because deterministic checks cannot establish it. |
| E3 | [Context receipts and exports](../tests/test_answer_context.py), [transcription manifests](../services/transcription/tests/test_model_manifest.py); completeness across every derived format is review only because there is no universal provenance audit. |
| E4 | [Report conflicts](../tests/test_report_conflicts.py), [note conflicts](../tests/test_notebook_conflicts.py), [human decision conflicts](../tests/test_review_decision_conflicts.py). |

This document's links, rule coverage and size are checked by
[its executable contract](../tests/test_documentation.py).
The draft requires maintainer review and approval before merge; the
[review policy](QUALITY_GATES.md#review-policy) and [publication safeguards](PUBLIC_ALPHA.md#prevent-disclosure-before-publishing)
still apply. This document grants no merge, enqueue or release authority.
