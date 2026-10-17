# Continuity C implementation and acceptance handoff — September 22, 2026

C is **Done** at `3c814e37adf18a84de9191bad84e0e8a2745e525` in
[PR #107](https://github.com/neilofneils404/exculpata-oss/pull/107). Full final-head
Quality, requested hosted code review and fixed-corpus real-model acceptance
passed. The [landing receipt](EXIT_ALPHA_CRUISE.md#continuity-c-landing-receipt)
supersedes the historical pending-gate notes below. This does not authorize
deployment or make a supported-release claim. The [bounded contract](product-slices/continuity-c-recorded-context.md)
retains the selected scope and limitations.

## Revisions and prerequisite

- Planning baseline: `0d59d5fe4f7be575a66039274415de945a6d5db5`.
- B landed in [PR #103](https://github.com/neilofneils404/exculpata-oss/pull/103)
  at `d21c1d4849b46b8ae85db19b4d2ce80c8a9669a8`; its accepted landing receipt is
  `2a50ae6be36182d73c1d76d7896f68096bc18c29`.
  Read-only verification confirmed merged state and successful completed
  [post-merge Quality run 35722012792](https://github.com/neilofneils404/exculpata-oss/actions/runs/35722012792),
  attempt 2, on that exact B implementation. B's receipt records all six gates,
  hosted code review and maintainer acceptance; earlier failures remain recorded.
- C base: `dcee0cfb7912a4289faef5e8646fc33b82da128a`. Reconciliation retained B,
  the Report test lease correction and subsequent documentation Quality work.
- Implementation: `62b961d20f04aca8ff15d05630fa5b37a7a1298f`.
- Dispatch refinement: `9a16678c93c6a5b27e01bf495afe92300b98a2a1`, the final
  behavioral revision covered here. This pins matching tokenizer/chat controls,
  links each account's stance and attribution to its exact admitted evidence ID,
  and adds a real local HTTP wire comparison using a synthetic runtime.
- Branch: `codex/matter-continuity-c`, in an isolated worktree. Original checkout
  onboarding edits were preserved. No held product work, model change, new
  database, separate queue or autonomous memory process was selected.

## User outcome and boundaries

Both existing focused-answer composers offer unchecked **Use my saved matter
context**. The existing queued `/ask` transaction binds approved complete context
and its scope to the job/request key. Duplicates and retries retain that snapshot;
removal and edits affect future submissions. Conflicting legacy modes are refused;
legacy confirmed/selected notebook requests remain operational. Context-off does
not erase ordinary old chat history, which is separately identified in receipts.

Supporting and competing originals must be available together, current and in
scope, within existing kind/count/character ceilings. Typed identity IDs, aliases,
uncertain dates, human states and reviewer notes remain orientation. They do not
support factual claims. The existing verifier remains in place; C additionally
requires source-close exact spans so overlapping source words cannot validate an
unsupported noun imported from a note. This intentionally limits paraphrasing.

Immutable submission and prepared-request records live in migration 0036 in the
existing control database. Request receipts retain exact serialized JSON/digest,
evidence routes and omissions, normalization, available model labels, runtime
budget and each repair/worker attempt. Prepared, attempted, transport failed and
completed have distinct meanings. A tokenizer request itself sends context for
preparation and is recorded even if generation is rejected for size. No credential
headers or endpoints are retained. No case text is written to general logs.

Complete chat input is counted by the configured runtime tokenizer using matching
chat-template controls. Input plus 1,200 output tokens and a 256-token margin must
fit the reported runtime window. The path neither estimates tokens from characters
nor silently truncates selected records. Token usage must match admission. Missing
or incompatible tokenizer/usage contracts fail closed; legacy paths are unchanged.

Answer and conversation views/exports expose **context supplied**, not proof of
attention or immutable model attestation. Complete exports include unfinished job
receipts. Note-only exports remain note-only. Report copy/compilation of these
answers is refused until it can preserve the notice; context is never made a Report
citation. Current changes are shown separately from historical records. Original
source, membership/provider, conversation, cancellation and worker checks protect
dispatch and final save. No write lock spans inference. Existing deletion/purge,
backup and clean-restore boundaries include the new state.

## Validation commands and evidence

Commands ran with Python 3.12 in the existing contributor environment, synthetic
fixtures only, `umask 022`, and `PYTHONPATH=src`. Application tests additionally used
`CASE_INTELLIGENCE_STORAGE_RESERVE_GIB=0` for temporary synthetic stores. Evidence
files mentioned below are local diagnostics, not uploaded attachments.

| Check | Command / result |
| --- | --- |
| Final C and generation regressions | `python -m pytest -q tests/test_answer_context.py tests/test_generation.py` — **48 passed**, including all **34 C tests**, 31.64 seconds. |
| Final application suite | `python -m pytest -q --tb=short --junitxml=/tmp/continuity-c-application-final-head.xml` — **3,328 passed, 9 skipped**, 831.82 seconds, exit 0, on behavioral revision `9a16678c93c6a5b27e01bf495afe92300b98a2a1`. All 34 C tests are included. |
| Account lifecycle diagnostic | `python -m pytest -q tests/test_local_account_lifecycle.py` — **57 passed**, 19.11 seconds; does not erase a full-run failure. |
| PostgreSQL integration | `python -m pytest -q tests/test_review_bench_v2_postgres.py --deselect tests/test_review_bench_v2_postgres.py::test_live_learned_dense_only_paraphrase_and_unsupported_abstention --junitxml=RESULT.xml`, then `python scripts/check-postgres-test-report.py RESULT.xml` — **7 passed, 1 deliberate model deselection, zero skips**. Cached pinned pgvector/PG17 image, ephemeral loopback test server, removed afterward. |
| Pinned browser runner | `python scripts/run-browser-acceptance.py --archives ARCHIVES --output OUTPUT` — **9/9 journeys passed**, Chrome **153.0.8010.36**, including the extended nine-check B/C journey. |
| Final dispatch browser check | `python scripts/browser-accept-matter-context.py --chrome-binary CHROME --chromedriver DRIVER --output OUTPUT` — **9/9 B/C checks passed** after the dispatch refinement, including keyboard new-conversation submission and a 390-pixel receipt/original link. |
| Transcription | From `services/transcription`: `python -m pytest -q` — **194 tests passed**, exit 0. No model weights were downloaded. |
| Deployment contracts | `./scripts/compose-check.sh` — standard, Kerberos and dedicated local-account graphs valid. |
| B upgrade, backup, clean restore, rollback | `python scripts/context-storage-restore-drill.py --answer-context` — passed from exact accepted B; immutable C prepared record and old notebook scope survived clean restore; matching pre-upgrade reader/backup rollback passed. Mixed writers were not used. |
| Encrypted backup integration | `EXCULPATA_BACKUP_INTEGRATION=1 python -m pytest -q tests/test_backup_consistency.py::test_encrypted_split_snapshot_and_postgres_restore` — **1 passed**, 10.42 seconds, actual restic encryption and PostgreSQL restore. |
| Compilation and whitespace | `python -m compileall -q src scripts tests`; `git diff --check` — passed. |
| Generic publication sanitizer | `python scripts/publication-check.py --skip-history` — **CLEAN**; expressly not outgoing-history/private-deny clearance. |
| Independent secrets | Gitleaks **8.30.1**, redacted working-tree and reachable-history scans — no leaks; initial history scan inspected **573 commits**. The completed handoff and final behavioral history were scanned again before the receipt commit. |

The nine application skips are eight opt-in PostgreSQL/model cases and the
opt-in encrypted-backup integration case. The separate PostgreSQL and encrypted
backup runs above exercised their applicable checks; the learned model case was
deliberately excluded and is not claimed as a pass. The one application warning
is the existing Starlette/AnyIO deprecation. Final documentation/scope contracts
also passed (**32 tests**). These are local Quality results, not hosted C statuses.

The C regression corpus selects two same-name identities, a disputed event with
support and a later competing original, an unresolved date, an unsourced
hypothesis/instruction injection and stale-source cases. Tests exercise meaningful
source-close mock answers, both accounts, identity separation, context-only claim
rejection, off/removal/history semantics, legacy modes, source-set limits,
cross-matter denial, duplicate concurrent connections, atomic rollback,
revocation, source invalidation, cancellation, restart/retry, obsolete workers,
oversize whole groups, exact wire bytes, repair differences, immutable storage,
exports, complete stopped-runtime restoration with the original store offline,
restored original navigation and purge. Mocks and the synthetic HTTP runtime
qualify application behavior only.

### Failures and corrections retained

Initial C tests exposed serializer key-order differences and an existing overlap
verifier accepting an unsupported context-only noun. Canonical wire recording and
the additional C exact-span gate corrected those failures. Initial browser runs
exposed a moved legacy disclosure, newline-versus-submit keyboard behavior, and a
navigation stale-element race; the composer and browser checks were corrected.
An initial full application run was stopped after a missing product-name title
failure (**1 failed, 1,664 passed, 1 skipped** at interruption). The new inspector's
title was corrected and the branding contracts passed.

The first complete browser attempt passed eight journeys but account-controls
failed during fixture setup under a permissive inherited umask. Explicit
`umask 022` produced the complete nine-journey pass. PostgreSQL initially failed
seven cases because this existing environment lacked psycopg; a temporary external
dependency target containing psycopg 3.3.6 was used for the successful real
integration run. These setup failures were not accepted as passes or hidden by skips.

The first completed full application run returned **1 failed, 3,325 passed,
9 skipped** in **845.95 seconds**. The failure was
`test_same_size_edit_with_restored_mtime_reloads_cached_snapshot`: consecutive
writes had identical filesystem ctime at the fixture's timestamp assertion.
Neither its test nor the account implementation differs from C's base. B's
[earlier diagnostic](product-slices/continuity-b-context-selection.md) reproduced
this same failure on an untouched baseline. All 57 account lifecycle tests passed
separately here. These facts identify the failure but do not turn that full run
into a pass. That run began before the final dispatch refinement and the last two
C regressions were collected; the final 48-test focused run includes them.

## Initial model gate (superseded by the follow-up below)

`python scripts/accept-answer-context-model.py --output RESULT.json` returned
**exit 2**, `adapter: UnavailableGenerator`, `configured: false`,
`model_gate: outstanding`, with no executed scenarios. No real model was available
through normal application configuration. No alternate model, weight download,
private configuration discovery or confidential remote request was used.

No actual model artifact revision, runtime version, effective model settings,
latency, useful answer or citation-quality result could be observed. The script's
1,200-token reservation and temperature 0.1 fields are request defaults, not
settings of an executed real model. Synthetic tokenizer counts, model names,
fingerprints and mock answers do not qualify this gate.

The retained script uses the configured generator through ordinary HTTP matter,
selection and `/ask` requests with three fixed new-conversation questions:
disagreement, same-name identity and unsupported hypothesis. It saves receipts and
available settings/runtime evidence for review. Its keyword checks are preliminary;
a reviewer must inspect useful answers, citation entailment, disagreement and
identity separation. The strict exact-span gate and the supported runtime's
chat-tokenization/usage contract need particular live evaluation. Failure or runtime
unavailability leaves acceptance outstanding.

## September 22 real-model follow-up

The user authorized continuing acceptance with the existing configured runtime.
Only the fixed synthetic corpus and a disposable OSS application workspace were
used. No model, deployment configuration or live application state changed.
Runtime connection information, exact model artifact/runtime evidence and raw
receipts remain outside this repository; they are not public attachments.

The first actual run passed disagreement and unsupported-hypothesis checks but
failed the identity question: the model removed the descriptive phrase from the
middle of the original sentence, and both attempts failed the unchanged exact-span
verifier. That is a failed model run, not acceptance. The C-only prompt now
explicitly requires an intact original sentence, including internal descriptive
phrases, and repeats that instruction beside the evidence. No verification rule,
model setting, token budget, source scope or legacy prompt was weakened.

A synthetic regression confirms that an abbreviated identity claim is rejected,
the intact original succeeds on the single repair, both attempts are recorded,
and the two same-name identity IDs remain distinct. The focused C/generation
suite passed **49 tests**. The acceptance script now also opens each citation,
checks exact original identity/version/digest and wording, requires successful
jobs, and rejects parcel content in the unrelated-identity answer.

Two subsequent normal-application runs passed all three fixed questions. The
second used the strengthened citation checks:

| Question | Inspected outcome | Latency |
| --- | --- | --- |
| Disputed delivery | Both attributed accounts retained, each citing its own exact original; raw ambiguous date preserved | 3.806 seconds |
| Same-name museum volunteer | Intact museum/postcard sentence with its own source; no parcel attribution or identity merge | 2.046 seconds |
| Unsourced helicopter hypothesis | Not supported; no factual claim or citation manufactured from the note | 1.777 seconds |

All three original links returned HTTP 200. Prepared and completed tokenization
and generation receipts were inspected; admission counts matched actual response
usage, with input plus reserved output and margin inside the runtime window.
No repair was needed in either passing run. This is acceptance of these three
synthetic questions with the observed configured model, not a general model-quality,
paraphrase, throughput, deployment or supported-release claim. The earlier failure
and unavailable-runtime attempt remain part of the record.

## Follow-up revisions and final local checks

- Intact-original prompt and strengthened real-model harness: `631ce90f90409370803b1933432247e6feab5226`.
- Authority-revocation fixture serialization: `6c85121b537b4cb6443a15d8747cc2d2a1b7f30b`.

The follow-up full application run completed with **3,328 passed, nine skipped,
one failed** in 848.25 seconds. The failure was
`test_notebook_rechecks_live_local_authority_after_render[True-disable_principal-context]`:
SQLite reported “cannot commit - no transaction is active” at the fixture's direct
write. That fixture lacked the workspace lock required by CONTRIBUTING; B's dated
record already documents the same baseline race. The correction adds the lock to
that test transaction only; application authorization behavior is unchanged.

After correction, `python -m pytest -q tests/test_matter_knowledge_workflow.py
tests/test_answer_context.py tests/test_generation.py tests/test_review_acceptance_pack.py
--tb=short` passed **102 tests** in 76.99 seconds, including the failing parameter
and all C tests. This focused pass does not relabel the earlier full run as green;
final-current-head full Quality remains required before landing.

Additional follow-up checks passed: the existing pinned browser's nine B/C checks,
194 transcription tests, 32 documentation/scope tests, compilation, all Compose
contracts, whitespace validation, generic publication inspection and independent
redacted Gitleaks working-tree inspection. An initial documentation command named
a nonexistent test file and collected no tests; the corrected command using
`tests/test_quality_change_scope.py` produced the 32-test pass.

The worktree initially inherited an older publication guard. The already-approved
newer installation retained all prior dispositions and corrected that setup.
Its outgoing-history inspection now finds only public repository-link literals
in two C documentation paths. The exact current and historical bytes require a
narrow owner-approved disposition; private deny terms and generic checks remain
enabled. No public upload has occurred.

## Historical remaining gates before publication (superseded by landing)

No C publication, hosted Quality, PR/native approval, merge, release or deployment
occurred. Before any authorized push, the installed outgoing publication check
must run and pass without bypass; public text and artifact metadata require
separate inspection. Hosted review remains optional unless a maintainer applies
`require-hosted-review`. Local diagnostics do not waive current-head Quality or
branch protections.

**Next action:** complete the final-current-head Quality, outgoing publication
inspection and review gates, then land C and record its exact upstream acceptance.
The installed publication preflight currently blocks outgoing history; inspect and
resolve its exact findings without bypass before any push.
