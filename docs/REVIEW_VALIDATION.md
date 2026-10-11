# Shared source validation and conflict recovery

Current contract, September 5, 2026.

**Check every source** already shares saved criteria, runs and source decisions
with authorized matter members. Its preliminary Exculpata label and rationale
remain separate from the team's human validation. The ordinary decision page
now calls that shared work **Team validation** and shows who saved the current
review. This is one current shared review per source decision, not a separate
private review for each person.

An older form cannot silently replace a teammate's decision or note. **Review
validation changes** shows the saved choice, note and reviewer alongside the
original machine result and exact source support. The proposed choice and note
remain in the form for comparison and correction. Source/support links open a
new tab so the draft remains visible. Saving after another intervening change
produces another conflict. Other source decisions can be reviewed independently.

Pending source decisions offer no Save action and cannot be adjudicated through
the store/API. If a decision becomes unavailable during editing, recovery retains
the proposed fields for copying without recreating it. Loss of matter access
blocks both recovery and saving. Current authorized edits retain the existing
review-metric and export behavior. Human edits never rewrite the machine label,
rationale, citations, source version or frozen population.

## Required version and authority contract

`POST /matters/{slug}/full-review/{run_id}/decisions/{document_id}` retains CSRF
and authorized-matter checks and adds required displayed `expected_updated_at`.
The store method requires the same argument. It begins an immediate SQLite
transaction, rechecks active principal/membership/matter and exact run/document,
compares the displayed revision, refuses pending decisions, then updates only
human choice/note and current reviewer/time metadata.

The existing decision timestamp serves as the concurrency token. Human edits,
normal pending-to-completed machine decisions, and the existing changed-source
invalidation path serialize and advance that metadata beyond its preceding
value even if the clock repeats or moves backward. Machine completion and source
invalidation retain their previous result/citation logic. Restoring an older
human note does not restore its token. No schema or new result-history table is
introduced, and tokens grant no access by themselves.

Missing/stale tokens return 409 with the proposed choice/note. Invalid choices
return 400 with the note retained for correction. An unavailable decision's edit
returns 409 without a Save action. Unauthorized requests remain denied, and
administrator access without matter membership retains its read-only boundary.
Success audit retains the existing operation metadata; old/new notes are not
added to audit. Reviewer names are resolved from stored attribution only after
matter access is established, with a plain fallback for unavailable identity.

## Validation and limits

`tests/test_review_decision_conflicts.py` covers independent connections and
simultaneous saves, independent source reviews, repeated/backward clocks, restored
old text, machine completion/source invalidation, pending decisions, cross-matter
canaries, revoked/inactive membership, closing matters, CSRF, escaped all-field
recovery, invalid/missing tokens, unavailable decisions and restart persistence.
The existing review-metric fixture adds only displayed-version arguments. Its
frozen acceptance node/file and aggregate fingerprints are refreshed; case
selection, expected metric assertions and fixture bytes are unchanged.

`scripts/browser-accept-review-decision-conflicts.py` uses a temporary loopback
application and two distinct synthetic reviewer sessions with a deterministic
local generator. It exercises normal upload, exact support, criterion/sample
execution and shared discovery, stale choice/note recovery, another intervening
edit, mobile resolution, saved reviewer attribution, separate machine output,
revoked open-form/new-read denial, complete export and deliberate owner closure.

```sh
PYTHONPATH=src python scripts/browser-accept-review-decision-conflicts.py \
  --chrome-binary /path/to/chrome --chromedriver /path/to/chromedriver \
  --output /path/to/synthetic-results
```

Local validation passes 807 application tests with 9 environment-dependent
skips, 32 focused source-validation/review tests and all six browser workflows.
Desktop/mobile rendering was inspected, and recovered changes were submitted
in the mobile viewport. Compilation, whitespace and publication tree/history
checks pass. Both Compose graphs and all 194 transcription tests pass.
The [review policy](QUALITY_GATES.md#review-policy) applies before merge; activation is separate.

This is protection of the current shared review, not a browsable revision
history, assignment/handoff system, private per-reviewer annotations or automatic
merge. Proposed fields persist only in the recovery page; leaving it can lose
them. Existing data remains readable by older code, but old writers cannot enforce
these guards. Do not mix old/new writers when relying on conflict protection.
Rollback retains saved work and source references while removing these guards;
refresh forms after changing versions.
