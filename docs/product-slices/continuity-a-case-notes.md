# Trusted matter continuity A — connect existing Case notes

Status: **Done** — landed September 21, 2026 at
`dd367cc3a65fa733d44b612026a9d3912523fed6` through PR #100.
Implementation baseline: `622cf7a03b67c6d850f06799c98b2333b2efd52d`.
GitHub issues and milestones are the work queue; the [cruise](../EXIT_ALPHA_CRUISE.md) is historical.

## Landing receipt — September 21, 2026

PR #100 is merged, and the landed revision is
`dd367cc3a65fa733d44b612026a9d3912523fed6`. Post-merge Quality run
**35610419040** completed successfully on that exact revision: application,
PostgreSQL integration, synthetic-browser, transcription, deployment-contract
and secret-scan all passed. The [cruise receipt](../EXIT_ALPHA_CRUISE.md#continuity-a-landing-receipt)
links the merged PR and run.

The final-head hosted code review reported no major issues; review findings were
resolved before maintainer-authorized landing. A separately requested hosted
security review did not complete. It was optional under the repository policy;
this receipt does not claim a completed hosted security review. The installed
publication guard passed before landing, with the previously owner-approved
exact documentation dispositions and all generic and private-deny checks intact.

The dated local results and review corrections below are historical evidence.
Their pending-acceptance statements are superseded by this receipt. In particular,
the original local full-suite timing failure remains a failed local result;
the successful post-merge application gate is separate evidence. This completes
only Continuity A, with no deployment, model qualification or supported-release
claim. B's selection contract precedes C's integration into one task path.

## User outcome and scope

An authorized reviewer returns to Case notes and sees existing notes alongside
saved identities and events/assertions, with links to existing record editing,
chronology, Review map, Reports and original sources. Two authorized reviewers
see the same matter records; note labels remain separate from stable identities.
Human confirmation does not establish independent truth. A disputed status,
competing source account and Review map comparison candidate retain their
different meanings. There is no inferred dispute category.

The page works without generation. Opening it adds no model calls, knowledge
mutations, selections, jobs or stored projections. Existing operational access
auditing and recent-activity tracking continue. Note editing, pinning, confirmed
or explicitly selected note reuse and exports keep their existing contracts.
Export case notes still exports notes, not a new combined knowledge artifact.

## Read contract

- Eight identities per page ordered by name and stable ID; six events/assertions
  ordered by explicit ordering date, then undated records, creation time and ID.
  The two page controls are independent of note filtering/pagination. All entity
  and assertion review statuses, including dismissed, are included and labeled.
  Applying note filters or a note-status tile preserves both knowledge pages
  and resets only the note page; status tiles also retain the note query/type.
- At most two mentions per identity and two accounts **per stance** per record.
  Counts and omitted records/references remain visible with complete-record links.
  Up to three aliases appear; additional aliases are explicitly counted.
  Statements/excerpts have labeled complete-record routes and visible ellipses.
  Mention excerpts show up to 260 characters, including retained historical text.
  Truncated account excerpts link directly to the complete account in its
  assertion record, even when no accounts were omitted from the preview.
  An out-of-range page displays the latest available page with its actual number.
- Review status and original-reference availability are independent. Unavailable
  support keeps attribution and historical text but has no live original link.
  Sources are validated by their exact saved metadata, never by token alone.
- Projection SQL shares the existing authorized SQLite transaction and source
  guard, batches admitted references, loads no correction histories and performs
  no writes. The HTTP response rechecks access after rendering and is uncached.
  This recheck refreshes the session, enabled identity and provider authority;
  an administrator override requires a current administrator role. Kerberos
  requests revalidate trusted-proxy binding and current group authorization.
  It does not repeat the initial administrator-access audit event.
  Existing administrator read-only Case notes access remains read-only. Without
  case-team membership, the preview explains that complete records, omitted
  references, identity management and chronology require membership, and hides
  those member-only links. Original-source and page links remain usable.
- Source links preserve the matter-local notebook filters and knowledge pages as
  return context, including through the full source reader. Existing source
  routes revalidate support. A later reload reads current human revisions.

No schema, graph database, automatic summary, identity/relationship inference,
new notebook, background-source workflow, selection persistence or retention/
reopen behavior is added. Increments B/C and model qualification are separate.

## Acceptance

Use synthetic notes, two same-name identities, an alias, an allegation, a competing
account later than supporting accounts, an uncertain date and unavailable support.
Prove two-reviewer inspection/edit/reload, independent paging and honest omissions,
no knowledge writes on GET, generation unavailable, escaped source instructions,
cross-matter denial and access revocation during response rendering. Exercise
source-return links, existing exports and note reuse, keyboard navigation,
390-pixel reflow, both themes and a reopened runtime. Source text cannot authorize
actions or change scope. Existing finite fixtures establish only their tested cases.

Record commands actually executed and limitations in this brief before handoff.
Do not mark Done before acceptance and landing. Preserve all unrelated holds.

## Local validation — September 21, 2026

The clean implementation branch starts at the implementation baseline above; no
upstream difference from the reviewed handoff baseline was found. Existing
onboarding edits in another checkout were preserved. No schema or persistent
record shape changed, so this increment introduces no migration/restore step.

Executed with Python 3.12 and synthetic fixtures:

- Focused projection, notebook, conflict, graph and source-navigation selection:
  **74 passed**, including **22** new continuity service/workflow cases.
- Browser runner failure/receipt contracts: **88 passed**, including failure of
  the newly registered continuity journey. CI uses the existing runner and its
  bounded artifact allowlist; there is no separate browser harness or CI job.
- PostgreSQL integration: **7 passed, zero skipped**, with the learned-model
  evaluation explicitly deselected. A disposable instance used the existing
  pinned pgvector image; it was removed after the checks.
- Bundled transcription suite: **194 passed**. Standard, Kerberos and dedicated
  local-account Compose graph checks passed. Compilation and diff whitespace
  checks passed.
- Standalone browser acceptance passed with Chrome 134: reopened runtime,
  unavailable generator, same-name identities, alias, historical source state,
  keyboard navigation, both source stances, full-reader return, note filters,
  pagination/reload, Light/Dusk and 390-pixel reflow.
- Pinned browser runner: **all eight journeys passed** with Chrome
  **153.0.8010.36**, using checksum-verified cached archives. The seven existing
  journeys retained their acceptance and the new continuity journey passed all
  seven checks. An initial integration run passed the existing journeys but the
  new script rejected the runner's precreated empty output directory; after
  correcting that contract, the complete runner passed. Existing results still
  cannot be overwritten by a direct rerun into a nonempty output directory.
- Full application suite: **3,192 passed, nine skipped, one failed** in 760.63s.
  The failure was the existing
  `test_same_size_edit_with_restored_mtime_reloads_cached_snapshot`: its two
  writes received the same filesystem ctime. It reproduced on the fifth
  diagnostic attempt against an unmodified archive of the upstream reference.
  The whole local-account lifecycle module separately reran with **57 passed**.
  This is baseline flakiness, not a passing full-suite result. The final extra
  continuity and browser-runner cases are covered by the focused runs above.

Representative commands (use the existing contributor Python environment):

```console
umask 022
CASE_INTELLIGENCE_STORAGE_RESERVE_GIB=0 python -m pytest -q tests/test_matter_knowledge.py tests/test_matter_knowledge_workflow.py tests/test_matter_notebook.py tests/test_notebook_conflicts.py tests/test_evidence_graph.py tests/test_evidence_graph_workflow.py tests/test_assertion_navigation.py
python -m pytest -q tests/test_browser_acceptance_runner.py
python scripts/browser-accept-matter-knowledge.py --chrome-binary /path/to/chrome --chromedriver /path/to/chromedriver --output /tmp/generated-continuity-acceptance
python scripts/run-browser-acceptance.py --archives /path/to/verified-browser-archives --output /tmp/generated-browser-suite
python scripts/publication-check.py --skip-history
```

The generic working-tree sanitizer passed. The installed publication guard
reported identical pre-existing findings on the candidate and unmodified
upstream trees; the installed outgoing-history check of that upstream base also
blocked. Added content, including new files, passed the installed scanner.
These comparisons are diagnostic, not a waiver or publication clearance. The
repository-wide history scan also includes unrelated local refs; it is separate
from the exact outgoing-history check. No guard settings were changed.

The first complete-suite attempt was interrupted after account-directory tests
rejected group-writable fixtures created with the shell's `0002` umask. The
subsequent attempt uses `022`; no product permission check was weakened.
Synthetic acceptance is not confidential-casework readiness, model qualification,
an installed-node acceptance or a supported release. At this initial validation
stage, no push, merge, publication, product installation or deployment had been
performed; subsequent publication and landing are recorded above.

At that stage, the next acceptance step was to review the bounded diff, reconcile
the baseline timing test and existing publication findings without weakening
their checks, then obtain final Quality and maintainer landing acceptance after
publication was explicitly authorized. Those landing gates are now complete as
recorded above.


## PR review corrections — September 21, 2026

The initial PR head subsequently passed all hosted Quality checks. Its code
review identified three user-visible gaps, corrected in this follow-up:

- Identity mention previews now render bounded, escaped saved excerpts and a
  complete-mention link for team members, including when the original is stale.
- Administrator previews hide member-only record/management links and explain
  the membership requirement. Synthetic cases follow every retained knowledge
  link both with and without team membership, including omitted-reference and
  truncated-statement cases; ordinary record authorization remains unchanged.
- Note filter submissions and all five status tiles preserve both knowledge
  pages while resetting the note page. Status tiles also retain query and type.

Validation: the focused continuity, notebook, conflict, graph, source-navigation
and browser-runner selection passed **171 tests**. The strengthened administrator
cases separately passed **two tests**. Against the original PR code, the new
regression selection failed in the **nine expected cases**; the administrator
with team membership remained passing. The pinned Chrome **153.0.8010.36**
continuity journey passed all seven checks, now including real filter submission,
status-tile navigation on later pages and visible unavailable-mention text.
Light/Dusk and 390-pixel reflow/source-return checks passed. The enlarged browser
fixture initially timed out on a full-element screenshot; bounded viewport
captures allowed the complete journey to pass. Generic publication sanitization
and whitespace checks passed. Full-suite validation on the updated head remains
with CI; the earlier local filesystem-timing failure is not reclassified.

Run the focused commands above with `PYTHONPATH=src` when borrowing an existing
contributor environment from another checkout so imports target this worktree.
The initial publication passed the installed outgoing-history guard after the
owner-approved exact documentation versions were recorded. The follow-up keeps
those versions unchanged and remains subject to the same installed guard.


### Second review follow-up

Truncated supporting and competing account excerpts now include a labeled link
to the exact account in the complete assertion record, even with no omitted
accounts. The administrator preview continues to hide member-only links. The
final notebook access recheck now uses the underlying authorization lookup,
retaining the real-actor and authorized-transaction checks without logging a
second successful administrator access; late denials remain audited.

The new regression selection reproduced five failures on the preceding head:
four long-account cases and the duplicate administrator audit. Two access-control
cases already passed and remain passing. After correction, **112 focused tests**
passed across continuity, notebook, graph, source navigation, matter management
and administrator authentication, plus **88 browser-runner contract tests**.
The pinned Chrome **153.0.8010.36** continuity journey passed all seven checks,
including following the long-account link to its exact account and returning to
Case notes. The longer source fixture required whitespace normalization in the
browser's source-pane text comparison; the complete text remains checked.
That updated head required fresh hosted CI and requested code review, subsequently
completed before the final landing recorded above.


### Third review follow-up

The final notebook authorization check now resolves the current session and
provider authority instead of trusting the administrator role captured before
rendering. Local account restrictions, revoked sessions and disabled principals
withhold the rendered content. Kerberos requests also refresh the trusted-proxy
binding and current group authorization. The actor must still match the original
reader, and any owner-backed administrator preview requires a current
administrator role. Matter and repository authorization checks remain in place;
late denials are audited without a second successful administrator-access event.

Twelve synthetic cases change authority after the template renders, using real
local account mutations and session revocation, with member/nonmember and
unchanged-account controls, plus Kerberos administrator-group removal with and
without continued application admission. Eight cases reproduced the defect on
the preceding head; all twelve pass after the correction. The broader continuity,
notebook, graph, source-navigation, matter-management, administrator, local-account
and identity selection passed **191 tests**. The pinned Chrome
**153.0.8010.36** continuity journey passed all seven checks. Fresh hosted CI and
the requested code review were pending at this stage and completed before landing.


### Fourth review follow-up

The notebook GET no-write test now stops and joins its fixture's compilation
worker before installing the shared SQLite trace callback. This isolates the
request from periodic compilation lease recovery while retaining the original
mutation checks. Callback cleanup also runs if the request raises. Product
behavior and production worker configuration are unchanged.

A deterministic diagnostic forced compilation recovery into the traced request
window and reproduced the preceding head's assertion failure. The corrected test
passed under that diagnostic. Injecting a write into the request still caused
the no-write assertion to fail, confirming the check remains effective. The
continuity service/workflow and report-compilation job suites passed **82
tests**. Earlier browser acceptance remains applicable to this test-only change;
fresh hosted CI and requested code review subsequently completed before landing.
