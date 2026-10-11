# Continuity B — explicit durable context selection

Status: **Done** — PR #103 landed at `d21c1d4`; see the
[landing receipt](#landing-receipt--september-22-2026). The existing
[cruise](../EXIT_ALPHA_CRUISE.md) is historical; GitHub issues and milestones are the work queue. C is not selected.

Reviewed baseline: `0d59d5fe4f7be575a66039274415de945a6d5db5`.
Implementation base: `91a97d2ef2b7d4733551a9189bd9c2c7c50c692c`.
The newer commit corrects test lease timing and documents that correction; it is
preserved. The original checkout predates A and has unrelated onboarding edits;
those remain untouched in that checkout. This increment uses an isolated branch.
The companion B/C proposal informs this contract; only the user's B scope is
selected, not its C instructions or model evaluation.

## Selected contract

One empty-by-default selection per matter and reviewer. Knowledge stays shared;
selection ownership is neither private notes nor a team default. Typed stable
references identify notes, entities and assertions (including events), including
all existing review statuses and origins. Selection never confirms truth.
Persist ordered references, approval stamps, selection revisions and attribution,
not copies of current facts. Note versions are opaque updated_at tokens; entity
and assertion versions are actual revisions. Support and role dependencies are
tracked separately. No truncated A projection is used as authoritative context.

GET and preview do not write selections or knowledge. Mutations use the existing
source guard, authorized connection and transaction, CSRF and current authority.
Concurrent changes require explicit recovery. Missing records stay missing;
changed records require explicit reapproval. Remove/reorder never refresh other
approvals. No same-name merge, record recreation or automatic re-add.

Fifty references is a storage/UI limit, not a model-fit promise. Bound reference
scans and serialized bytes before loading full records. Complete exports include
attributed selection manifests; note-only exports and historical answer scopes
are unchanged. Add a paired migration, stopped-backup/clean-restore and purge
coverage. Mixed-version writers are unsupported; stop writers before upgrade,
retain a complete pre-upgrade backup and use its matching release for rollback.
Ordinary exports are not restore archives.

No model calls, prompt/retrieval changes, jobs, Report-memory compilation,
infrastructure changes or lifecycle reopening. Saved selections are **not yet
consumed by answers**. B acceptance requires synthetic service, HTTP and existing
browser-runner evidence, restart, migration/restore/export/purge, current Quality
and publication checks. Required landing remains pending until separately
authorized. Exact executed evidence and handoff will be appended here.

## Executed evidence — first persistence increment

Python 3.12, synthetic fixtures, `PYTHONPATH=src` with the existing contributor
environment. `python -m pytest -q tests/test_matter_context.py
tests/test_matter_knowledge_workflow.py`: **45 passed**. Covers all review states,
two owners, same-name identities, away-and-back note edits, role/source changes,
concurrent revisions, bounded materialization, missing/removal behavior,
SQLite backup/clean restore/restart/export/purge, and unchanged A workflows.
`python scripts/publication-check.py --skip-history`: generic working tree
**CLEAN**; history was not scanned. Initial development tests exposed a missing
migration registration and fixture argument errors; these were corrected before
the passing run. HTTP/export/browser and full gates are still in progress.

## Implementation decisions

The UI opens complete bounded inspection from A's typed record links and adds a
Selected context section to Case notes. Deterministic Move up/down, Remove and
Clear actions use selection revisions. Add and Accept current basis additionally
require the exact server-resolved approval stamp displayed during inspection.
Conflicts retain the attempted action, ID, revision and stamp alongside current
state; no automatic retry or merge occurs. Unrelated entries retain their old
approval stamps even when another entry is reconciled. Missing and oversized
entries remain removable. Existing entity merge/split operations retain their
stable records; B detects their revision changes and never substitutes a target
identity. The existing editor remains responsible for repairing assertion roles.

Approval fields are the real record version plus separate deterministic hashes
of record fields, exact support metadata/availability and typed role dependencies.
Notes use opaque `updated_at`, including away-and-back invalidation. Fingerprints
are change detectors, not a historical archive: previous fact bodies are not
copied. Inspection labels current fields and the approved version separately.
All attached support within the bound is inspected, including late competing
accounts; no A first-two-reference projection is admitted as complete context.
Unavailable support may be deliberately retained as labeled orientation; it never
gains a live-original link or truth status from selection.

Bounds: 50 selected references; 200 source references and 100 roles per inspected
record; 1,000 dependency rows and 512 KiB before full materialization per selection
inspection, plus an independent 512 KiB serialized bound. Candidate inspection
has its own bounded allowance. No correction history is loaded. Complete export
has a separate 1,000-owner / 8 MiB manifest ceiling and existing bundle bounds.
This does not promise that any selection will fit a future model request.

## Executed evidence — workflow and recovery

All material was synthetic; no live generator was enabled. Commands used the
existing Python 3.12 contributor environment, with `PYTHONPATH=src` for this
worktree and `umask 022` for account-directory tests.

- `python -m pytest -q tests/test_matter_context_workflow.py`: **6 passed** for
  initial HTTP flows, conflict recovery, exact original returns, two reviewers,
  CSRF/admin/revocation, note-only exports, full runtime restart/clean restore and
  complete export/purge. The first attempt found an omitted bundle allowlist
  entry and an incorrect test expectation for existing admin POST denial; both
  were corrected before the pass.
- Expanded B service and HTTP suites: **19 passed**, including transaction
  ownership/rollback, read-only identity validation, dependency count/byte limits,
  separate SQLite connections, escaped instruction-like text and machine origin.
- `python scripts/context-storage-restore-drill.py`: **passed**, upgrading the
  reviewed baseline, preserving frozen legacy answer scopes, restoring B state
  and rolling back the pre-upgrade backup with its matching baseline reader.
- `python -m pytest -q tests/test_review_bench_v2_postgres.py --deselect
  tests/test_review_bench_v2_postgres.py::test_live_learned_dense_only_paraphrase_and_unsupported_abstention
  --junitxml=/tmp/recordbench-context-postgres-results.xml`, with the existing
  pinned pgvector image in a disposable test container: **7 passed, one explicitly
  deselected**. `check-postgres-test-report.py` confirmed zero skipped. The test
  container was removed. No model evaluation was requested or run.
- Bundled transcription `PYTHONPATH=src python -m pytest -q` from
  `services/transcription`: **194 passed** (all collected cases, exit 0).
- `python -m compileall -q src scripts tests` and `./scripts/compose-check.sh`:
  **passed**, including standard, Kerberos and local-account graphs.
- The existing Quality read-only installer diagnostic snippet: **passed**;
  no application node was installed or changed.
- `python scripts/run-browser-acceptance.py --archives /path/to/verified-archives
  --output /tmp/recordbench-context-pinned-suite`: **all nine journeys passed**,
  Chrome **153.0.8010.36**, including B's seven checks. Native keyboard save,
  source-return navigation, concurrent tabs, restart/reload, same-name IDs,
  explicit reconciliation/removal, Light/Dusk and 390-pixel reflow passed.
  The initial standalone B run hit a browser navigation timing issue; the
  corrected runner waits for the old element to detach, and the rerun passed.

An expanded focused run had **251 passed, one failed** because a new bound test
intercepted allowed reads of other selected identities as well as its oversized
fixture. The test was narrowed to the oversized identity; the 19-test B rerun
passed. A broader authority run also included an initially invalid machine-note
fixture without required original support; support was added, preserving the
existing source guard. These were test-fixture errors, not waived requirements.
The first full application attempt was deliberately interrupted at **636 passed,
one skipped** to incorporate the transaction-ownership correction; it is not a
full-suite pass. Final full-suite and final browser evidence follow below.

## Initial implementation evidence and handoff

Implementation revision: `2567f974d46604c0870131d4f2c1e72bac27fb2a`.
Persistence/contract commit: `4e6888caa3cd80cbb4aba47d9cb21e30776cbd86`.
The subsequent receipt commit changes documentation only.

The final focused command was:

```console
PYTHONPATH=src CASE_INTELLIGENCE_STORAGE_RESERVE_GIB=0 python -m pytest -q tests/test_matter_context.py tests/test_matter_context_workflow.py tests/test_matter_knowledge_workflow.py tests/test_identity_membership_audit.py tests/test_browser_local_accounts.py tests/test_local_account_lifecycle.py
```

Result: **159 passed**. This includes local-account/provider/session/principal
changes after rendering for both the existing notebook and new context surface,
with administrator/member controls. The earlier authority run's invalid
machine-note fixture produced **157 passed, one failed** before correction.
The final B-only subset separately passed **19 tests**.

The final complete pinned browser runner was repeated after the read-only
authority correction and inspection-template adjustment:

```console
PYTHONPATH=src python scripts/run-browser-acceptance.py --archives /path/to/verified-archives --output /tmp/recordbench-context-final-browser-suite
```

Result: **nine of nine journeys passed**, Chrome **153.0.8010.36**; B's seven
checks all passed. The runner's checksummed archives and bounded artifact
allowlist remain in use. No browser harness, CI job or infrastructure was added.

Independent Gitleaks **8.30.1** working-tree and reachable-HEAD history scans
passed: **no leaks found**, **345 reachable commits** for the implementation
head. Generic publication sanitization passed. The installed outgoing-history
pre-push guard was executed locally without pushing and **blocked**. Its installed
working-tree scanner reported eight operator-deny rule/path findings; the same
scanner on an untouched archive of the implementation base reported the
**identical eight findings**. No matched values or private deny terms are retained
here. This comparison is diagnostic, not publication clearance or a waiver.
Guard settings and dispositions were not changed.

Both observed full-suite failing cases reproduced against an untouched archive
of `91a97d2ef2b7d4733551a9189bd9c2c7c50c692c`:

- `test_same_size_edit_with_restored_mtime_reloads_cached_snapshot`: baseline
  diagnostic attempts one/two passed, attempt three failed because consecutive
  writes had the same filesystem ctime.
- `test_notebook_rechecks_live_local_authority_after_render[False-disable_principal]`:
  baseline attempt one passed; attempt two failed with SQLite's “cannot start a
  transaction within a transaction” at the fixture's direct principal update.
  The corresponding current-tree notebook case also passed when rerun alone.

These unrelated baseline issues were not repaired in B. The complete local
application result is recorded below; neither baseline comparison turns a failed
full-suite gate into a pass.

Final full application command:

```console
umask 022
PYTHONPATH=src CASE_INTELLIGENCE_STORAGE_RESERVE_GIB=0 python -m pytest -q
```

Result: **3,265 passed, nine skipped, two failed in 822.89 seconds**. The two
failures are the baseline-reproduced cases above. In the current full run, the
notebook fixture failed at transaction exit with “cannot commit - no transaction
is active”; its baseline diagnostic failed at transaction entry instead. Both
are concurrent transaction errors at that fixture's unguarded direct write.
The complete local application gate remains **failed**, despite the focused
passes. No unrelated baseline fix was included.

The nine skips were the eight opt-in PostgreSQL/model cases and the opt-in
encrypted backup drill. Seven PostgreSQL cases were executed separately and
passed as recorded above. The encrypted backup case was then executed separately:
`PYTHONPATH=src RECORDBENCH_BACKUP_INTEGRATION=1 python -m pytest -q tests/test_backup_consistency.py::test_encrypted_split_snapshot_and_postgres_restore`:
**one passed in 10.12 seconds**, using real restic encryption and PostgreSQL
restore in disposable synthetic storage; app stop/start is simulated by that
existing drill. No installed-node acceptance is claimed.
The live learned-model case was deliberately excluded:
B does not invoke or change models, and C/model acceptance is outside this task.

Changed areas: paired migration 0035; reference-only context repository/service;
thin context routes; Case notes and detail navigation/inspection; transaction-safe
read-only authority refresh; complete-export manifest and purge integration;
synthetic service/HTTP/browser/restore acceptance; this brief and binding queue.
Generation, prompts, retrieval, historical notebook scopes, model behavior,
deployment and unrelated working-tree edits are unchanged. No push, PR, merge,
release, installation or deployment was performed.

Outstanding gates at that revision: the failed full application result; installed publication
clearance; the current [review policy](../QUALITY_GATES.md#review-policy) and authorized
landing. No hosted review was requested automatically. This is implemented local
B, **not Done**, a supported release or deployment acceptance. C remains a
separate unselected task.

**Next action recorded at that revision:** maintainer review of this bounded B diff and its existing
application/publication gate findings, before separately authorizing publication
and landing. Do not integrate C or bypass either gate to close B.

## September 22 B inspection follow-up

Review of local head `cce20b813402acae2dabd24e26f7a72cbceb5789` found that
changed role dependencies showed the recorded identity name and revision but
omitted the current identity's name and review status. Context inspection now
distinguishes recorded and current names, types and revisions, displays current
review status and origin, and links to the current identity by its stable ID.
Deleted identities retain the recorded role with an explicit missing warning.
Current review metadata is resolved through the existing authorized repository
after dependency byte/count preflight; only its fingerprint is persisted.
The richer role fingerprint conservatively invalidates prior local B role
approvals; GET never upgrades them and explicit reconciliation remains required.
No schema, model, generation, retrieval or infrastructure change accompanies this
follow-up. The binding queue's opening text now agrees with its B-selected row.

Executed with the same Python 3.12 environment, synthetic fixtures and `umask 022`:

- `PYTHONPATH=src CASE_INTELLIGENCE_STORAGE_RESERVE_GIB=0 python -m pytest -q
  tests/test_matter_context.py tests/test_matter_context_workflow.py`:
  **20 passed in 11.72 seconds**. The new HTTP regression covers all five entity
  review statuses, escaped current names, exact identity links, unchanged stored
  approval and deliberate reconciliation.
- The earlier 159-test focused command plus `tests/test_browser_acceptance_runner.py`:
  **255 passed in 99.29 seconds**. The role regression was subsequently extended
  with deletion/missing-identity assertions; that exact test passed separately
  (**one passed in 1.47 seconds**).
- `PYTHONPATH=src python scripts/run-browser-acceptance.py --archives
  /path/to/verified-archives --output /tmp/recordbench-context-role-review-suite`:
  **nine of nine journeys passed**, Chrome **153.0.8010.36**. B's seven checks
  now include changed role inspection, dismissed-identity labeling, exact
  identity navigation and explicit reconciliation in Light/Dusk and at 390 pixels.
  Standalone B browser acceptance also passed before the complete runner.
- `PYTHONPATH=src python scripts/context-storage-restore-drill.py`: **passed**
  baseline upgrade, clean restore, historical answer scope preservation and
  rollback with the matching pre-upgrade reader.
- Compileall, whitespace checks and generic working-tree publication sanitation:
  **passed**. Generic sanitation is not installed publication-guard clearance.

Current full-application and outgoing-history results for this follow-up are
recorded in the subsequent receipt; the previous failures are not waived.

### Pre-publication acceptance receipt and handoff

Implementation revision: `fa6df35f1b5058c231bd308756372eca10fb3c92`.
This receipt is a subsequent documentation-only commit.

The complete application command was executed again against the follow-up:

```console
umask 022
PYTHONPATH=src CASE_INTELLIGENCE_STORAGE_RESERVE_GIB=0 python -m pytest -q
```

Result: **3,268 passed, nine skipped in 800.05 seconds**, exit 0. This includes
the final deletion assertions added to the role regression. The current local
application gate passes. The two earlier baseline-reproduced failures did not
recur; their diagnostic evidence above remains relevant to maintainer review,
and no unrelated correction was made. The nine skips retain the same opt-in
PostgreSQL/model and encrypted-backup meanings documented above. The separately
executed PostgreSQL, encrypted-backup and transcription evidence is from the
initial implementation; those suites were not repeated for this inspection-only
follow-up. The baseline-upgrade/clean-restore drill was repeated and passed.

The installed outgoing-history guard was executed against `fa6df35` without a
push and **blocked**. Its current-tree scanner reports the same eight rule/path
findings as the untouched implementation-base archive. This is diagnostic, not
clearance. No deny values, guard settings or dispositions were changed or added
to the repository. The independent Gitleaks working-tree scan passed with no
leaks. A read-only `git ls-remote origin refs/heads/main` confirmed public main
still points to `91a97d2ef2b7d4733551a9189bd9c2c7c50c692c`; no newer upstream
work needs reconciliation. The original checkout's unrelated work is unchanged.

Local evidence logs for this follow-up: `/tmp/recordbench-context-role-review-application.log`,
`/tmp/recordbench-context-role-review-focused.log`,
`/tmp/recordbench-context-role-missing.log`,
`/tmp/recordbench-context-role-review-suite/summary.json`,
`/tmp/recordbench-context-role-review-restore.log` and
`/tmp/recordbench-context-role-review-publication.log`. These are local execution
artifacts, not committed runtime state or published attachments.

**Outstanding gates and next action:** maintainer disposition of the installed
publication guard's existing findings, followed by separately authorized
publication and current-candidate Quality/maintainer landing checks. No push,
PR, merge, release or deployment occurred. B remains implemented locally,
**not Done**; saved selections are not consumed by answers. Stop at B; C remains
a separate task.


## Landing receipt — September 22, 2026

B is **Done**. PR #103 landed at
`d21c1d4849b46b8ae85db19b4d2ce80c8a9669a8` at 11:32:26 UTC on September 22.
The [binding queue receipt](../EXIT_ALPHA_CRUISE.md#continuity-b-landing-receipt)
links the PR, hosted review and Quality runs. It supersedes the earlier pending
handoffs above while retaining their actual failures and diagnostic evidence.

The user authorized publication and landing, then explicitly approved the
B-only exact-file publication dispositions. All matches in the three changed
historical queue versions were confined to public repository URLs. The already
approved baseline dispositions were retained in a new external guard
installation; the deny list and generic rules remained intact. Complete
outgoing-history checks passed before branch publication and protected landing.
The original checkout's guard and unrelated work stayed unchanged.

GitHub's merge API rejected the verified public no-reply email. The documented
normal protected fast-forward then landed the exact reviewed head, preserving
its scanned author identities. No force push, administrator bypass, protection
change, history rewrite, release or deployment was used.

Exact-head Quality run **35720397034** and PR integration run **35720409778**
passed application, PostgreSQL integration, synthetic-browser, transcription,
deployment-contract and secret-scan. The integration application log reported
**3,268 passed, nine skipped, one dependency deprecation warning in 737.99 seconds**.
The PostgreSQL log reported **seven passed, one deliberate model deselection,
zero skipped**. The hosted browser receipt confirmed all nine journeys and B's
seven checks on Chrome **153.0.8010.36**. The existing local upgrade/restore,
export, purge and authority evidence remains recorded above.

Post-merge Quality run **35722012792**, attempt **2**, passed all six jobs.
The application retry reported **3,268 passed, nine skipped, one dependency
deprecation warning in 598.85 seconds**. Its first application attempt made
continued progress to 96% but was cancelled at
the existing 30-minute job timeout; its recorded progress contained no failure
markers. That attempt did not pass. Only the application job was rerun, on the
same commit and without changing workflow, timeout or tests; the five successful
jobs were retained. The final result, rather than a waiver, closes this gate.

The user requested hosted Codex code review immediately after landing. Request
comment **5775685862** bound it to the full landed head. The official summary
recorded completion at **11:39:30 UTC** on that head; result comment **5775776780**
reported no major issues, and no inline findings were posted. This was a hosted
code review after landing, not a pre-merge hosted review or a security review.
Local review and the existing native review-policy gate had completed before
landing; the PR did not carry the strict hosted-review label.

Outcome: case-team reviewers can durably select, inspect, reconcile, remove and
reorder existing matter knowledge with current status/support warnings and
stable identity, while selections remain reference-only and are not consumed by
answers. No B implementation or acceptance gate remains unresolved at this
landed revision. This documentation-only receipt follows the repository's
publication, Quality and review process. No new product work is selected:
**stop at B; C is separate and unselected**.
