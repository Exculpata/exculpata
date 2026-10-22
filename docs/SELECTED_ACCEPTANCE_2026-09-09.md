# Selected final-acceptance candidate — September 9, 2026

Status: combined candidate under final acceptance; not a release or deployment.
This branch contains only the six component heads below, merged without product
code corrections. It preserves their original histories and adds current
integration documentation. The default branch must receive this combined head
only after its own tests, hosted code/security reviews and maintainer acceptance.

| Component | Outcome | Preserved head |
| --- | --- | --- |
| [#32](https://github.com/neilofneils404/exculpata-oss/pull/32) | Check final export readiness and link Reports needing repair | `d8801d90cc62382f96bab5279d5e67dd380576cd` |
| [#35](https://github.com/neilofneils404/exculpata-oss/pull/35) | Plan portable installation, search, review, and work-product slices | `c4261579881904a0ecb877a6ced7c08449082f94` |
| [#36](https://github.com/neilofneils404/exculpata-oss/pull/36) | Define strict exact-search grammar and known-answer corpus | `25455cdb9a61b67bc09adf45f8f2c2b5ab308a73` |
| [#37](https://github.com/neilofneils404/exculpata-oss/pull/37) | Check complete candidate history without unrelated fetched branches | `5155a418b58dfe3e8d0d7e3546a1a875ccc7bdaf` |
| [#38](https://github.com/neilofneils404/exculpata-oss/pull/38) | Preserve findings, gaps, and human review in Reports | `1de30e5eb43164e11c8dffc04f1a3c2d1ebcda08` |
| [#40](https://github.com/neilofneils404/exculpata-oss/pull/40) | Make investigation budgets and coverage limits explicit | `cd4fa734c416687d52e4d36103364e582eed2dc0` |

The visible additions are **Check export**, fuller saved-review-to-Report
conversion, and explicit investigation budgets. The exact-search parser is a
foundation; this candidate does not add the Find sources UI or proximity search.
Contributor briefs and candidate-history checks do not change staff workflows.

## Deferred work

The following open contributions remain separate for follow-up. Recheck current
threads before editing; inherited findings can occur in several branches.

| PRs | Required follow-up before renewed acceptance |
| --- | --- |
| #34 | Experimental Mac qualification: model quality, clean installation, restore/update and platform lifecycle evidence. |
| #39 | Installer hostname validation and protected creation of missing storage ancestors. |
| #41, #43 | Account snapshot caching, rename/audit consistency and validation of the frozen authentication store in backups. |
| #42, #45 | Search admission for actual scans, media labels, proximity previews, grammar compatibility and either-order performance. |
| #44 | Preserve selected human material and distinct assertions sharing citations; finish cancellation/deletion recovery. |
| #47 | Classifier result contract, enforced admission/generation/storage limits, bounded citations and truthful/scalable coverage. |
| #48 | Preserve model snapshot links; validate ancestors during account relocation, resume and update. |
| #46 | Refresh the broad integration candidate after component fixes; verify full-text Report conversion and export/closure coordination. |
| #49 | Reconcile the frozen acceptance manifest and test tablet support-drawer layout. |

Do not adopt the broad #46 integration branch to obtain the six selected changes:
it also includes the deferred behavior. Subsequent work should reconcile against
the accepted default branch and retain synthetic regressions and source support.

## Acceptance and recovery

Run the full application and transcription suites, Compose/compilation and
publication checks, focused component regressions, and the existing synthetic
Report/export browser scripts against the combined tree. Inspect current-head
hosted code and security reviews; an older completion or a quota/error message
is not final-head approval. Retain the exact tested head in acceptance receipts.

These components add no schema migration or dependency. Investigation accounting
uses existing JSON fields; older runs retain unknown accounting. Reverting code
must preserve existing source bytes, Reports, review decisions and current work.
Deployment adoption requires a separate compatible integration, current backup,
clean restore proof and real application acceptance under the operator's policy.
