# Shared Report editing and recovery

Current contract, September 5, 2026. This document governs Report editing;
[Report exports](REPORT_EXPORTS.md) governs downloads.

Team members already share Reports. Older forms now refuse to replace newer
saved work. **Review changes** shows the current title, purpose, status and
section order, or the current edited section with its exact source support.
Submitted title, purpose, status, heading and text remain available for the
writer to compare and adjust before saving again. A further intervening edit
conflicts again. Ordinary validation errors also preserve authored fields.
Source links and the full Report open in a new tab so the proposed fields stay
available during review.

A Report's details, marking it Final, movement controls and whole-Report
Delete require the displayed Report version. Editing or removing one section
requires that section's displayed version and the currently displayed Report
status. Teammates can therefore edit different sections independently. Adding
written or cited material checks the displayed status and appends under the
same serialized transaction: independent additions retain both sections and
unique consecutive positions. Movement changes section positions without
invalidating another section's unchanged text editor.

If the Report changes after a page was opened, marking it Final requires
reviewing the current work first. Existing Final status remains a manual label:
current Final Reports are deliberately editable, without automatic downgrading,
immutable approval or new citation validation at status change. A Draft form
cannot edit, remove or append while the Report is currently Final. The status
check compares current state, not a history of intermediate status transitions.

Stale structural/deletion actions show current work and ask the user to return
and review before retrying. When someone deletes a Report or section during an
edit, recovery keeps the submitted text for copying and offers no Save that
could recreate it. Leaving recovery can lose unsaved fields; this is not
persistent draft storage, a browsable edit history or automatic conflict merging.

## Storage and HTTP contract

No schema migration or new work-product storage is required. Existing Report
and section `updated_at` values are opaque concurrency tokens. Successful
writes advance the affected values beyond their preceding value even when the
clock repeats or moves backward; restoring old text does not restore its token.
Every section mutation also advances its parent Report token. Moving sections
changes the parent token and positions but preserves section-content timestamps.

All routes below retain CSRF and authorized-matter checks. Store and adapter
methods require the relevant arguments too:

| POST action beneath `/matters/{slug}/reports/{report_id}` | Required displayed fields |
| --- | --- |
| Report details, `/delete`, `/sections/{section_id}/move` | `expected_updated_at` for the Report |
| `/sections/{section_id}`, `/sections/{section_id}/delete` | `expected_updated_at` for the section; `expected_status` for the Report |
| `/sections`, `/from-notebook/{item_id}`, `/from-finding/{finding_id}`, `/from-clip/{clip_id}`, `/from-answer/{conversation_id}/{message_id}` | `expected_status` for the Report |

Each mutation begins an immediate SQLite transaction, rechecks active principal,
membership, matter and exact Report/section scope, compares the fields above,
then writes. A missing or stale field produces 409; ordinary invalid authored
input produces 400 with a correction form. Deleted authored work produces 409
with submitted text retained. Unauthorized recovery cannot reveal saved work
or another matter. Administrator access without membership retains the existing
read-only Report policy. Edit tokens do not grant authority.

Existing current-editor attribution and success audit remain; old/new bodies
are not added to audit. Source references remain attached to their exact source
versions. Report copies from notes, findings, answers and clips retain existing
snapshot behavior, and accepted edits appear in new individual and complete
exports. This does not introduce a source-retargeting or export contract change.

## Validation and rollback

`tests/test_report_conflicts.py` covers two connections, shared header/section
conflicts, independent edits and parallel appends, Final/structure/deletion
checks, repeated clocks, restored old text, cross-matter canaries, revoked or
inactive members, closing matters, CSRF, escaped all-field recovery, missing
tokens, correction, and deleted-Report draft recovery. Existing Report, review
and media tests use the required displayed fields and retain source/export
coverage. The frozen Review acceptance pack updates only the changed media
test-file digest and aggregate fingerprint; selected cases, node digests and
fixtures remain unchanged.

`scripts/browser-accept-report-conflicts.py` uses a temporary loopback app and
two distinct synthetic reviewer sessions. It checks real upload and support,
shared Report discovery, metadata/section conflicts, repeated mobile recovery,
independent edits/additions, Final, ordering, stale deletion, deleted-section
recovery, complete Word/Markdown export and deliberate closure with unchanged
external original bytes. Run with local Chrome and ChromeDriver paths:

```sh
PYTHONPATH=src python scripts/browser-accept-report-conflicts.py \
  --chrome-binary /path/to/chrome --chromedriver /path/to/chromedriver \
  --output /path/to/synthetic-results
```

Local validation passed 796 application tests with 9 environment-dependent
skips, 56 focused Report/export/review tests, and all 194 transcription tests.
Both Compose graphs, compilation, publication tree/history and whitespace checks
passed. All eight final two-reviewer Chrome checks passed, with desktop/mobile
rendering inspected; the existing seven Report-bundle browser checks also
passed. The local bundle-script runner disabled unused WebSocket autodetection
to avoid an unrelated inherited system-package mismatch. The
[review policy](QUALITY_GATES.md#review-policy) applies before merging.
Production activation remains a separate deployment decision.

Older software can read the existing schema but cannot enforce these guards.
Do not mix old and new writers when relying on conflict protection. A rollback
preserves saved work while removing the guards. Refresh forms after changing
versions; old forms without tokens receive recovery on the new version.
