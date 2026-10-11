# Slice 06 synthetic acceptance

Date: 2026-09-10. Baseline: `9b6fa6c855011953c91dce9d4fd554cae1636dd1` (current main at start).
Scope: reusable application team groups and explicit matter grants only.
Slice **06** landed on `main` as
[#65](https://github.com/Exculpata/exculpata/pull/65)
(`e44e485ab3eca94b7e5588765df58f7f4b2b4786`).

## Checks performed

- The focused authorization, local-account, onboarding, report-queue and group
  suite passed **94 tests**. The group file includes mixed grants, concurrent writes,
  audit-failure rollback, removed/disabled accounts, worker startup, queued-answer
  and transcription fences, independent report lease connections, late-save
  refusal, matter purge, mirrored migration, SQLite backup and clean restore.
- The real-browser runner passed with two independent local-account sessions,
  two groups and two matters. It created and assigned a group, refused access to
  the other matter, displayed mixed reasons, retained a direct grant after group
  removal, denied the open session after the final grant was removed, and checked
  export access before/after revocation. Desktop and 390-pixel mobile screenshots
  were inspected; no horizontal page overflow was observed.
- The transcription service suite passed **194 tests**.
- Python compilation, standard/Kerberos/local-account Compose validation and
  whitespace checks passed.
- Candidate files plus every current-main ancestor passed the publication
  sanitizer in an isolated candidate copy. Unrelated branch histories were not
  imported. No residue rule was suppressed. The installed pre-push guard is
  required again for the outgoing commit.

## Broad native comparison and limits

Using the CI setting `CASE_INTELLIGENCE_STORAGE_RESERVE_GIB=0`, the native macOS
candidate run recorded **2,448 passed, 125 failed, 9 skipped**; the isolated exact
baseline recorded **2,437 passed, 125 failed, 9 skipped**. The failed-test identities
were identical. Two additional focused regressions for matter purge and removal
of disabled direct grants were added and passed after that broad run.

The host lacks Linux-specific absolute-path media/document tools expected by
those tests (for example `/usr/bin/ffmpeg`). No usable local Docker daemon was
available for Linux validation. These matching baseline failures are comparison
evidence, **not a passing full suite or a merge waiver**. Follow the
[review policy](QUALITY_GATES.md#review-policy) for the submitted commit. No model-quality or installed-node acceptance is claimed.

The backup test uses SQLite's backup API, restores into a newly created target,
checks integrity and foreign keys, and verifies group/direct reasons and audit
history. This validates the added control-store schema with synthetic state;
it does not certify a real operator's encrypted backup, projection import or
replacement-host recovery. See [team group rollback constraints](TEAM_GROUPS.md).

## Reproduction

```console
python -m pytest tests/test_team_groups.py tests/test_identity_membership_audit.py tests/test_workspace_store.py tests/test_team_onboarding.py tests/test_report_compilation_jobs.py tests/test_browser_local_accounts.py tests/test_local_authentication.py -q
python scripts/qa-team-groups-browser.py --artifacts /tmp/recordbench-group-acceptance
```

The browser runner requires Node, OpenSSL, Playwright and a supported installed
browser. `PLAYWRIGHT_MODULE` can select the installed module and
`RECORDBENCH_QA_BROWSER_CHANNEL=chrome` can select Chrome. It starts a temporary
loopback HTTPS application with synthetic accounts and disposes of it afterward.
Generated captures stay outside the repository. No existing deployment is used.

## Review follow-up

The source-removal form again says **Remove**; **Remove direct grant** remains
specific to case-team membership. A rendered HTTP-page regression exercises both
forms with a synthetic failed source and direct member.

Transcription failed-job Retry, recording-check Retry and Continue now atomically
bind the queued attempt to the currently authorized acting member. Three
synthetic regressions revoke the original requester, reject their recovery
attempt, then verify owner recovery through worker authorization and transcript
import without changing the source version or restoring the old grant.

The expanded native run passed **100 tests** and failed **52 media tests**; every
failed-test identity is present in the exact-main comparison above. Linux media
integration remains a hosted check. Publication inspection covered the candidate
tree and complete current-head ancestry before committing the follow-up.
