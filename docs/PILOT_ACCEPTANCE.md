# Pilot acceptance

This guide is for an office evaluating the Exculpata prerelease on synthetic
data. It is **not a security certification**. It does not promise a result for
any matter, model, or installation. ABA Model Rule 7.1 is the caution against
communications that create an unjustified expectation about a lawyer's
services; nothing in this guide is an outcome promise.

Each control below states the check, how to run it on a synthetic-data node,
and the evidence already in this repository. A gap is marked **No evidence yet**.
Links point at files, tests, and CI jobs in this repository. Run the checks
from a development checkout after `make bootstrap`, with:

```console
export CASE_INTELLIGENCE_STORAGE_RESERVE_GIB=0
export CASE_INTELLIGENCE_TEST_MODE=1
```

Those two variables match the application job in
[`.github/workflows/quality-gates.yml`](../.github/workflows/quality-gates.yml)
(job `application`). They keep the synthetic suite inside a small disk reserve.
Use synthetic records only.

## Phased pilot plan

1. **CPU baseline, Harbor Street.** On a development checkout, `make demo` runs
   [`scripts/demo.py`](../scripts/demo.py) and prepares the fictional Harbor
   Street matter from [`demo_data/harbor-street`](../demo_data/harbor-street).
   Provenance is in [`demo_data/README.md`](../demo_data/README.md). The demo
   binds to loopback, uses a synthetic preview sign-in, and does not need a
   model. A separate installed CPU node is
   [`docs/INSTALL.md`](INSTALL.md) (`./install --models none`) followed by
   [`docs/FIRST_RUN.md`](FIRST_RUN.md). **No evidence yet** for loading the
   Harbor Street corpus into that installed Compose node.
2. **Known-answer synthetic case.** The frozen corpus is
   [`tests/fixtures/synthetic/product-foundation/v1/corpus.json`](../tests/fixtures/synthetic/product-foundation/v1/corpus.json).
   [`tests/test_exact_search_results.py`](../tests/test_exact_search_results.py)
   `test_frozen_known_answer_corpus_runs_through_result_service` runs it, and
   [`docs/EXACT_SEARCH_RESULTS.md`](EXACT_SEARCH_RESULTS.md) records the
   coverage. **No evidence yet** for one installed-node matter that presents
   that corpus as a click-through case.
3. **Failure paths.** Use the checks under [Failure paths](#failure-paths).
   They are synthetic tests. **No evidence yet** for a recorded drill that
   fills the disk of an installed node.
4. **GPU.** Planning targets are in the README hardware table and
   [`docs/BETA_REFERENCE_INSTALL.md`](BETA_REFERENCE_INSTALL.md). **No evidence
   yet** for a recorded GPU pilot result.

## Matter isolation

**Check.** A direct request that names another matter's original, snippet,
transcript, export, or job returns no foreign content.

**How to run.**

```console
.venv/bin/python -m pytest -q \
  tests/test_continuous_source_browsing.py::test_media_and_passage_links_keep_library_context \
  tests/test_milestone_a_workbench.py::test_cross_matter_search_support_and_conversation_isolation \
  tests/test_matter_media_workflow.py::test_transcript_and_clip_routes_fail_closed_across_matters \
  tests/test_work_product_exports.py::test_report_exports_refuse_cross_matter_citations \
  tests/test_matter_archive.py::test_cross_matter_and_operational_canaries_are_absent_but_failed_work_remains \
  tests/test_answer_context.py::test_cross_matter_id_and_reader_cannot_leak_receipt \
  tests/test_intake_receipt_http.py::test_receipt_http_rejects_cross_matter_and_malformed_batches_without_writes
```

**Evidence.**

| Surface | Evidence |
|---|---|
| Originals | [`tests/test_continuous_source_browsing.py`](../tests/test_continuous_source_browsing.py) requests `/sources/{token}/content` on a second matter and expects 404. |
| Snippets | [`tests/test_milestone_a_workbench.py`](../tests/test_milestone_a_workbench.py) reuses a support token on the other matter and expects 404. |
| Transcripts | [`tests/test_matter_media_workflow.py`](../tests/test_matter_media_workflow.py) rejects a foreign transcript segment, clip download, and summary export. |
| Exports | [`tests/test_work_product_exports.py`](../tests/test_work_product_exports.py) refuses a cross-matter citation. [`tests/test_matter_archive.py`](../tests/test_matter_archive.py) keeps a foreign-matter canary out of the preservation archive. |
| Jobs | [`tests/test_answer_context.py`](../tests/test_answer_context.py) returns 404 for another matter's answer-job context. [`tests/test_intake_receipt_http.py`](../tests/test_intake_receipt_http.py) rejects a cross-matter receipt batch. |

The application job in
[`.github/workflows/quality-gates.yml`](../.github/workflows/quality-gates.yml)
runs the application suite. Matter scope is also stated in
[`docs/SECURITY_MODEL.md`](SECURITY_MODEL.md).

## Identity and authorization

**Check.** Role, session expiry, and a disabled account each block the action
that identity no longer allows. Ordinary membership does not close a matter.
The gateway drops forged authentication headers.

**How to run.**

```console
.venv/bin/python -m pytest -q \
  tests/test_local_account_lifecycle.py::test_changes_revoke_sessions_without_restart_and_never_revive \
  tests/test_local_authentication.py::test_disabled_local_account_cannot_reuse_a_prior_session \
  tests/test_identity_membership_audit.py::test_session_idle_expiry_and_stable_provider_subject \
  tests/test_case_lifecycle.py::test_member_cannot_exit_and_admin_requires_current_session_role
PYTHONPATH=src python3 -m unittest tests.test_gateway_nginx -v
```

**Evidence.** [`docs/AUTHENTICATION.md`](AUTHENTICATION.md) describes local,
OIDC, and Kerberos modes, server-side sessions, and the `reviewer` /
`administrator` roles. The lifecycle test covers password change, disable and
re-enable, and role change: the old session does not survive.
[`tests/test_local_authentication.py`](../tests/test_local_authentication.py)
covers a disabled local account.
[`tests/test_identity_membership_audit.py`](../tests/test_identity_membership_audit.py)
covers idle expiry.
[`tests/test_gateway_nginx.py`](../tests/test_gateway_nginx.py) is the
deployment-contract step "Verify real gateway strips forged authentication
headers" in
[`.github/workflows/quality-gates.yml`](../.github/workflows/quality-gates.yml)
(job `deployment-contract`).

## Runtime network egress

**Check.** Companion services that must not reach the network have no network
namespace. Other services attach only to the named Compose networks. Model
runtime settings stay offline. The gateway is the published service and
defaults to loopback.

**How to run.**

```console
.venv/bin/python -m pytest -q tests/test_reference_network_boundary.py
./scripts/compose-check.sh
```

**Evidence.** [`tests/test_reference_network_boundary.py`](../tests/test_reference_network_boundary.py)
reads [`compose.yaml`](../compose.yaml) and checks `network_mode: none` for the
disconnected services, internal networks, unpublished ports except the
gateway, and offline model flags. [`docs/SECURITY_MODEL.md`](SECURITY_MODEL.md)
describes the same boundary: the gateway is the published service, model
workers have no published ports, and the transcription worker has no network
namespace. `./scripts/compose-check.sh` is the deployment-contract step
"Validate standard and Kerberos Compose graphs".

**No evidence yet** for a packet capture or firewall log from a running node.
The Compose test is a source contract, as that test file states.

## Hostile inputs and parser limits

**Check.** Malformed, oversized, or hostile synthetic inputs fail closed:
paths, media types, empty files, symlinks, damaged PDF and Office containers,
spreadsheet shared-string references, and email MIME part and body limits.

**How to run.**

```console
.venv/bin/python -m pytest -q \
  tests/test_upload_pilot.py::test_store_rejects_paths_mime_malformed_empty_oversize_and_symlinks \
  tests/test_xlsx_shared_strings.py \
  tests/test_email_attachment_coverage.py::test_malformed_multipart_is_not_reported_as_ready \
  tests/test_email_attachment_coverage.py::test_mime_and_body_limits_apply_beneath_attachment_boundaries
```

**Evidence.** Those tests, plus the bounded-parser statement in
[`docs/SECURITY_MODEL.md`](SECURITY_MODEL.md). Email limits are also described
in [`docs/EMAIL_ATTACHMENT_COVERAGE.md`](EMAIL_ATTACHMENT_COVERAGE.md).

**No evidence yet** for a fuzz campaign beyond these fixtures.

## Prompt injection from discovery text

**Check.** Untrusted discovery text that tells the model to ignore prior
instructions does not become the answer. The gold case
`evidence-prompt-injection` is in
[`src/case_intelligence/model_portfolio_gold.py`](../src/case_intelligence/model_portfolio_gold.py).

**How to run.**

```console
.venv/bin/python -m pytest -q tests/test_model_portfolio_gold.py
```

**Evidence.** [`tests/test_model_portfolio_gold.py`](../tests/test_model_portfolio_gold.py)
checks that the frozen suite, including that case, is structurally valid.
[`docs/SECURITY_MODEL.md`](SECURITY_MODEL.md) says prompt-injection fixtures and
span checks do not prove general model safety.

**No evidence yet** for a run of that case against a live generator with a
recorded pass or fail. The structural test does not call a model.

## Audit trail

**Check.** Security-relevant actions append a content-minimized audit event
with actor, time, matter, action, and outcome. Event details that would copy
source text stay out of the preservation archive's audit chronology.

**How to run.**

```console
.venv/bin/python -m pytest -q \
  tests/test_identity_membership_audit.py::test_preview_login_session_csrf_membership_revocation_and_audit
```

**Evidence.** That test covers preview sign-in, CSRF, membership revocation,
and the audit record. [`docs/STORAGE_AND_BACKUP.md`](STORAGE_AND_BACKUP.md)
describes the matter-scoped audit chronology and the omitted detail fields.
[`docs/SECURITY_MODEL.md`](SECURITY_MODEL.md) and
[`docs/AUTHENTICATION.md`](AUTHENTICATION.md) state that events are attributed
and content-free for the flows they name.

**No evidence yet** for a published inventory of every route and the audit
action it writes.

## Backup and restore integrity

**Check.** A stopped, consistent backup restores into an isolated target.
Restore does not start the application. Pending closure still requires a fresh
deletion confirmation. An encrypted split snapshot round-trips.

**How to run.** Follow [`docs/STORAGE_AND_BACKUP.md`](STORAGE_AND_BACKUP.md)
on synthetic data, including its restore drill. Then:

```console
.venv/bin/python -m pytest -q \
  tests/test_backup_consistency.py::test_encrypted_split_snapshot_and_postgres_restore \
  tests/test_case_lifecycle_web.py::test_stopped_backup_clean_restore_keeps_mode_but_requires_fresh_confirmation
```

The PostgreSQL case needs `CASE_REVIEW_POSTGRES_TEST_DSN` set to a disposable
synthetic database, as in the `postgres-integration` and `deployment-contract`
jobs of
[`.github/workflows/quality-gates.yml`](../.github/workflows/quality-gates.yml).
The deployment-contract job runs
`test_encrypted_split_snapshot_and_postgres_restore` for both naming
generations.

**Evidence.** The documents and tests above. The storage document says not to
declare backup ready without a clean restore drill.

## Retention and deletion

**Check.** An expired or overdue matter is still present after the maintenance
scheduler and an answer job run. A stored date does not authorize deletion.
Final deletion requires a separate confirmation after a cancellation window of
at least seven days.

**How to run.**

```console
.venv/bin/python -m pytest -q \
  tests/test_no_automatic_matter_purge.py \
  tests/test_matter_retention_and_themes.py::test_supported_maintenance_cleans_staging_without_date_driven_deletion \
  tests/test_expired_matter_recovery.py::test_enabled_startup_preserves_expired_complete_restore \
  tests/test_case_lifecycle.py::test_seven_days_and_fresh_confirmation_required_without_timer_purge
```

**Evidence.** The code contract, which these tests exercise:

| Behavior | Code |
|---|---|
| Maintenance reports zero purged matters and does not claim deletion | [`src/case_intelligence/workbench.py`](../src/case_intelligence/workbench.py) lines 1898–1905 |
| Due-date inventory is empty, and setting a deletion date is rejected | [`src/case_intelligence/workspace_store.py`](../src/case_intelligence/workspace_store.py) lines 1856–1861 and 2055–2059 |
| A legacy review period is only a check-in interval | [`src/case_intelligence/workspace_store.py`](../src/case_intelligence/workspace_store.py) lines 1630–1635 |
| The cancellation window is seven days | [`src/case_intelligence/case_lifecycle.py`](../src/case_intelligence/case_lifecycle.py) lines 25 and 252–253 |
| Confirmation is refused until that window ends, and the claim requires a fresh token | [`src/case_intelligence/case_lifecycle.py`](../src/case_intelligence/case_lifecycle.py) lines 345–346 and 376–406 |

[`docs/CASE_LIFECYCLE.md`](CASE_LIFECYCLE.md) is the operator description.
[`docs/SECURITY_MODEL.md`](SECURITY_MODEL.md) states that maintenance never
initiates or retries deletion. [`docs/STORAGE_AND_BACKUP.md`](STORAGE_AND_BACKUP.md)
states that reaching the end of the window never deletes a matter.

The application job runs these tests with the rest of the suite.

## Coverage limits

### Email attachments

**Check.** Extracted email shows attachment names and an unprocessed-content
notice. Attached-message text does not become the parent body. That notice is
not a claim that attachment bytes were searched.

**How to run.**

```console
.venv/bin/python -m pytest -q tests/test_email_attachment_coverage.py
```

**Evidence.** [`docs/EMAIL_ATTACHMENT_COVERAGE.md`](EMAIL_ATTACHMENT_COVERAGE.md)
and that test module. The document states that attachment child sources,
archive expansion, and PST support remain separate work.

**No evidence yet** for review of attachment file contents as their own sources.

### Entity recall

**Check.** Deterministic suggestions are reviewable and distinguishable from
human records. Visited-unit coverage is not mention recall.

**How to run.** Read [`docs/ENTITY_DISCOVERY.md`](ENTITY_DISCOVERY.md) and run:

```console
.venv/bin/python -m pytest -q tests/test_automatic_discovery.py tests/test_suggestion_inbox.py
```

**Evidence.** [`docs/product-slices/17-entity-extraction.md`](product-slices/17-entity-extraction.md)
says the landed extractor is deterministic with limited recall, and that
visited-unit coverage does not prove every mention was found.
[`docs/ENTITY_DISCOVERY.md`](ENTITY_DISCOVERY.md) is the processing contract.

**No evidence yet** for a measured mention-recall figure on a gold set of
untitled names, aliases, or multilingual text.

### 6,000-character synthesis cap

**Check.** A complete original longer than 6,000 characters is omitted as a
whole. A prefix is not sent in its place.

**How to run.**

```console
.venv/bin/python -m pytest -q \
  tests/test_full_text_synthesis.py::test_long_original_retains_explicit_omission_for_decisive_accounts_after_6000
```

**Evidence.** [`src/case_intelligence/review_budget.py`](../src/case_intelligence/review_budget.py)
line 15 sets `evidence_item_chars` to 6,000.
[`docs/HIERARCHICAL_SYNTHESIS.md`](HIERARCHICAL_SYNTHESIS.md) and
[`docs/FULL_TEXT_SYNTHESIS.md`](FULL_TEXT_SYNTHESIS.md) describe the omission.
The test above records `oversized_original` for the long unit.

## Failure paths

**Check.** A full disk during preservation leaves no partial archive. Restart
or clean restore of a pending closure still requires a fresh confirmation.
Answering stays unavailable when no generator is configured, and search still
works. An interrupted purge does not invent a new receipt.

**How to run.**

```console
.venv/bin/python -m pytest -q \
  tests/test_matter_archive.py::test_diskfull_during_zip_write_cleans_all_temporary_content \
  tests/test_case_lifecycle_web.py::test_stopped_backup_clean_restore_keeps_mode_but_requires_fresh_confirmation \
  tests/test_case_lifecycle.py::test_confirmed_interrupted_purge_retries_but_legacy_failed_never_does \
  tests/test_case_lifecycle.py::test_new_ongoing_checkins_never_create_delete_schedule
```

The Harbor Street demo in [the phased plan](#phased-pilot-plan) shows the
unavailable-generator message while search and source review work. That
message comes from the unavailable generator in
[`src/case_intelligence/generation.py`](../src/case_intelligence/generation.py)
lines 756–762, used when the backend setting is empty (lines 771–774).

**Evidence.** The tests above, [`docs/CASE_LIFECYCLE.md`](CASE_LIFECYCLE.md)
for restart, restore, and interrupted deletion, and the README demo section.
The application job runs the tests.

**No evidence yet** for an installed-node drill that fills a real disk, kills
the process mid-export, or unplugs a GPU during a pilot.
