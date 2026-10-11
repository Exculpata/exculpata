# Evidence-backed events and assertions

Slice 18 connects the [entity workspace](ENTITY_WORKSPACE.md) to reviewer-owned
events and assertions. The storage and provenance contract is
[issue #80](https://github.com/Exculpata/exculpata/issues/80).
The implementation depends on the cruise promotion in
[PR #81](https://github.com/Exculpata/exculpata/pull/81), including
commit `e0301b9839b8c69d07f40c5f5827ab4873f428d6`. That prerequisite and this
behavioral PR have separate final-head review and main acceptance gates.

## Reviewer workflow

Find an original passage, choose **Attach to entity**, and select the intended
identity. From its detail page choose **Create event or assertion**. Enter a
title and the statement being reviewed, select an explicit entity role and a
supporting mention, and attribute the account to its speaker or source. The
manual workflow requires no model. A record begins with at least one entity
role and one supporting original account.

Roles are subject, participant, speaker, witness, location, object or organization.
Select additional identities by name or alias in the record's **Add or correct
an entity role** controls. Search pages retain separate same-name identities.
Inspect the identity before selecting it. To correct a role, select the saved
role, the intended identity and the intended role, then save. This replacement
is atomic, including when it is the record's only role.

To compare another account, find its original passage and choose **Use in event
or assertion**. Open the intended record from the chronology, choose supporting
or competing, and enter that account's attribution. **Open original passage**
retains the record as source-review return context. Each source keeps its own
account position and attribution; the human review decision belongs to the
record separately. Confirming, disputing, dismissing or correcting a record does
not rewrite the source accounts. Confirmation records a human decision; it
does not establish objective truth or certify source accuracy.

A record may be an event or an assertion. The statement is reviewer-authored
working text, not new source evidence. A shared name, similar wording, transcript
speaker cluster or shared appearance in a document does not create any record,
identity, relationship, guilt, intent or causal inference. Existing review-map
co-mentions remain leads. There is no machine relationship proposal adapter in
this slice, and discovery reruns cannot overwrite these manual records.

**Date as stated** retains the raw wording, including ambiguous slash dates.
**Date uncertainty** records the reviewer's explanation. The optional ordering
date accepts only a valid, explicitly entered `YYYY-MM-DD` calendar date. The
application never derives it from the raw wording or supplies a timezone.
Chronology shows all saved events and assertions, ordered by explicit ordering
date first, then unresolved or undated records. Pages contain 50 records; entity
detail links to its filtered chronology and shows the first 50 linked records.
This is a reviewer-maintained chronology, not an exhaustive event extractor.

Below the saved records, the matter-wide chronology shows **Dates found in
sources**: a read-only draft of every passage in which automatic discovery found a
date, from identities and passages a reviewer has not marked Not relevant. A
passage a reviewer attached by hand, or one discovery found as a name, thing or
identifier, is not listed even if its identity is retyped or merged into a date;
a found date moved into another date identity still is. A full
calendar date (`YYYY-MM-DD`, with or without a stated time) that is a real day
appears in date order by day; times and offsets are shown as stated but never used
for ordering. Any other date, such as `03/04/2026` whose day/month order is unknown
or an impossible day like `2026-02-30`, is listed after them by source and is never
guessed. Each entry shows its passage with the date highlighted, links to the
original when its source is still current (otherwise it is labelled retained
history) and to the date identity, which can return to the same timeline position.
Each entry shows the review status of its date identity and, separately, of its
passage, so a disputed passage is visible even on a confirmed identity. Pages
hold 25 dates. Viewing the draft saves, confirms and exports nothing; to keep
one, open its passage and choose **Use in event or assertion**. An identity's
filtered chronology does not show the draft. See
[entity discovery](ENTITY_DISCOVERY.md#automatic-discovery-after-processing).

## Revisions, originals and identities

All mutations check the displayed record revision inside the existing immediate
workspace transaction with current actor, matter membership and lifecycle
authorization. Selecting or replacing a role also checks the chosen entity's
revision. Independent writers cannot both save from the same record revision.
Conflict recovery shows the saved record and preserves submitted fields,
account attribution/position, role selection and review decision. Reopening a
form is not permission to overwrite later changes. A removed record is not
recreated, and lost access never returns the submitted draft.

Original-source attachments use the existing document/version/unit/locator,
excerpt digest, support token and retained excerpt contract. The source mutation
guard spans source resolution through the workspace commit, in source-then-
workspace lock order. New attachments require current originals. When originals
change or disappear, saved excerpts remain explicitly historical and live links
are disabled. Reviewer decisions remain independently visible. Correct the
source account or attach replacement support deliberately; the application does
not replace the original excerpt with generated prose.

Each role retains the explicitly selected entity ID, label, type and revision.
Later entity edits, merges, splits or deletion never silently reassign that role.
The record marks the identity current, changed or missing and links to a current
identity when available. Deleting an entity leaves the role's separate saved
label in the assertion; the entity deletion control discloses this. Correct or
remove the assertion role separately when that retained interpretation is wrong.

At least one role and one supporting account must remain. Add replacement support
before removing the last supporting account; roles can be replaced atomically.
An identical passage with the same attribution is one account. Repeating an
attachment is idempotent after revision checks; change its position using the
account-correction control. The same original may have separately attributed
accounts when it contains distinct speakers. No semantic truth inference is
performed on reviewer text.

History records actor, time, action and each revision's statement/date/decision,
plus role and account additions, removals and corrections. Excerpts are recorded
at addition/removal, not recopied on every edit. Content-free success audits
follow the existing application audit boundary. Individual export audits identify
the exported assertion; chronology export audits identify the matter. Removing a record permanently
removes its roles, accounts and history; original sources and entities remain.

## Exports and bounds

Individual record JSON and Markdown include roles, both account positions,
attribution, original source/version/unit/locator support, current source
availability and correction history. Chronology JSON and Markdown contain every
record in the requested matter or entity filter, independently of page size.
Exports read a consistent authorized snapshot. Markdown preserves disagreement
and raw date uncertainty; it never reduces competing accounts to one accepted
fact. The full matter work-product bundle adds `assertions/NNNN-assertion.json`
for every record under its existing aggregate export limits. Bundle support is
explicitly not revalidated, including after failed closure, and must not be
interpreted as a live-original availability claim. Existing administrator
recovery authority and response/deletion leases remain in force.

Limits are 5,000 records per matter, 100 roles and 200 accounts per record,
1,000 retained revisions per record and 64 MiB of logical assertion storage per
matter, including history and per-row allowance. A write exceeding a limit rolls
back completely. Chronology export refuses an oversized result instead of
silently dropping records; use individual exports if necessary. These limits
are not measured corpus-capacity qualification. Final bundles remain bounded by
the existing aggregate work-product limit. Work-product exports do not contain
original files and are not reopenable Exculpata packages.

Entity-to-Report compilation, matter memory/background, production import,
larger capacity, learned relationship extraction, a visual graph, held Mac work
and deployment remain separate scopes. The landed
[single-run full-text synthesis adapter](FULL_TEXT_SYNTHESIS.md) has its own
bounded input and export contract; it does not infer relationships or connect
entity records to Reports.

## Migration, clean restore and rollback

Mirrored additive migration `0034_evidence_assertions.sql` introduces records,
roles, original accounts and correction history. The existing SQLite control
store is authoritative on both retrieval profiles; no derived PostgreSQL
relationship schema or new provider is added. Repository operations compose the
entity repository's existing connection, lock and authorized transaction. Routes
contain no persistence SQL. Matter purge and retention expiry delete every new
record and its dependent tables before retaining only the existing minimized
closure/audit receipt.

Before upgrade, stop writers and take a verified complete backup under
[storage and backup](STORAGE_AND_BACKUP.md): control database, session state,
managed originals, configuration/secrets, matching release and any applicable
projection/account boundary. Restore into a clean target and verify integrity,
foreign keys, original bytes, source resolution, decisions, role snapshots and
history. The synthetic HTTP drill moves the old runtime and backup offline
before starting the restored app, then exports and exercises real source purge.
It does not qualify an independently installed node or an operator's backup.

Rollback uses the verified pre-upgrade backup and its exact matching older
reader. Never run pre-0034 application/purge code against upgraded state. Export
later work and preserve the upgraded backup first; an earlier snapshot cannot
contain later decisions. Release tags remain immutable.

Validation commands:

```console
python -m pytest -q tests/test_evidence_assertions.py tests/test_assertion_workflow.py tests/test_entity_workspace.py tests/test_entity_discovery.py
python scripts/assertion-storage-restore-drill.py
python scripts/browser-accept-assertions.py --chrome-binary /path/to/chrome --chromedriver /path/to/chromedriver --output /tmp/generated-assertion-acceptance
```

The browser journey also runs in the required
[synthetic browser runner](BROWSER_ACCEPTANCE.md) (`scripts/run-browser-acceptance.py`).

The [dated validation receipt](EVIDENCE_ASSERTIONS_VALIDATION_2026-09-12.json)
records actual results and limitations. The
[review policy](QUALITY_GATES.md#review-policy) applies separately; synthetic acceptance is not confidential-casework readiness.

The [historical post-18 receipt](EXIT_ALPHA_CRUISE.md#historical-post-18-receipt-superseded-snapshot)
records this feature's final-head gates and passed independent post-merge checks.
The dated synthetic receipt retains its preparation-time observations.
