# Exit-alpha cruise

> Retired: use [GitHub issues](https://github.com/Exculpata/exculpata/issues)
> and [milestones](https://github.com/Exculpata/exculpata/milestones)
> for the work queue. This cruise is retained for historical context; its old
> PR rules are superseded by the [review policy](QUALITY_GATES.md#review-policy).

## Current state — September 30, 2026

Reference upstream revision: `3c814e37adf18a84de9191bad84e0e8a2745e525`.
Manual entities, entity discovery, events/assertions, the first single-run
full-text synthesis adapter, graph exploration, Linux CPU B-lite and trusted
matter continuity A, B and C are landed.
Historical receipts below retain their original scope and evidence.

**Ask routing foundation (approved; local implementation, not landed):**
[Pure intent classification](EXACT_SEARCH.md#ask-intent-classification) selects
exact search, every-source review or a question from typed text, with a short
explanation. This increment adds no UI, HTTP route, model call or I/O. It is
proposed in [PR #146](https://github.com/Exculpata/exculpata/pull/146).
Review corrections preserve the reusable PR template and keep ordinary statements
about a population of records on the question path. The [review policy](QUALITY_GATES.md#review-policy) applies before landing;
deployment remains paused.

**Trusted matter continuity A is Done**:
[connect Case notes to saved identities and events/assertions](product-slices/continuity-a-case-notes.md).
A reviewer returning to an active matter can inspect the team's saved knowledge,
its human review status and current original-source availability from Case notes.
PR #100 landed at `dd367cc3a65fa733d44b612026a9d3912523fed6`, and post-merge Quality run
**35610419040** passed all six gates. The [landing receipt](#continuity-a-landing-receipt)
and [brief](product-slices/continuity-a-case-notes.md#landing-receipt--september-21-2026)
record final acceptance and preserve the earlier local validation limitations.

**Trusted matter continuity B is Done**:
[explicit, durable reviewer-owned context selection](product-slices/continuity-b-context-selection.md#landing-receipt--september-22-2026).
PR #103 landed at `d21c1d4` after all six Quality gates passed.
Post-merge Quality run **35722012792** passed after the application-only retry;
hosted Codex code review completed without actionable findings on the same head.
B acceptance remains complete.

**Trusted matter continuity C is Done**:
[recorded selected context in focused answers](product-slices/continuity-c-recorded-context.md).
PR #107 landed at the reference revision above after full Quality, requested
hosted code review and fixed-corpus real-model acceptance. The
[landing receipt](#continuity-c-landing-receipt) records the evidence and limits.
**Assistant dock passage save is Done:** [PR #123](https://github.com/Exculpata/exculpata/pull/123)
landed at `63d807f503501aec9e65b5ea3dd1199536c1459d`. Its PR records exact-head
Quality and synthetic acceptance, including local environment limitations.

**Selected next increment (approved; local implementation, not landed):**
A dependable manual review workspace: open a source, read or watch it, write a
source-linked human note or ask about it, then move through the filtered queue.
Default manual review reaches the sources/viewer; automated screening remains
available under an explicit name. A compact persistent reader with optional
queue and adjustable/collapsible questions/notes keeps source location visible.
Current question scope and historical answer provenance remain distinct;
in-flight responses cannot replace a newer source or conversation. Human notes
reuse existing provenance, review status, authorization, CSRF and deduplication.
Saved work and guarded entity discovery remain reachable. Preserve reports,
exports, full conversations, extraction cautions and deletion policy.
Synthetic baseline screenshots, rendered PDF/media checks, navigation/race/error
regressions and current Quality gates bound acceptance. No new model, graph,
storage system, automatic ingestion extraction or CI deduplication. Draft PR
publication is selected; merge, deployment and production-server acceptance
remain paused. The [workspace contract](WORKSPACE_LAYOUT.md) records behavior
and acceptance commands as implementation is validated.

**Usability follow-up (approved; local implementation, not landed):** a synthetic
walkthrough of the landed workspace found that pages lead with oversized headers,
nine tabs and repeated notices, and that a stale cached stylesheet could pair new
reader markup with old styles. Approved order: (0) content-versioned static assets;
(1) one compact matter bar — Home, Review, Ask, Search, Case file, Settings — with
matter search, one-line readiness and Home-only retention detail; (2) reader
refinements; (3) automatic deterministic people/things/date suggestions after
processing, with a bulk review inbox (approved in principle; suggestions stay
Suggested until a reviewer decides); (4) compact cited answers; (5) assistant
actions under a separate brief. Increments 0 and 1 landed in
[PR #126](https://github.com/Exculpata/exculpata/pull/126) at `d991411`
after hosted code review; hosted security review was unavailable and was waived for
that PR. Increment 2 (whole-page PDF fit, folders in the reader queue, bounded find
in this file) landed in [PR #127](https://github.com/Exculpata/exculpata/pull/127)
at `29f94e9` after clean final-head hosted code review and passing Quality; it also
raised the application gate timeout to 45 minutes. Increment 3a (automatic
discovery after processing, migration `0037`, behind
`CASE_INTELLIGENCE_AUTOMATIC_DISCOVERY`) landed in
[PR #131](https://github.com/Exculpata/exculpata/pull/131) at `a53a6fa`
after clean final-head hosted code review, passing Quality and maintainer
acceptance; its restore drill is `scripts/automatic-discovery-restore-drill.py`.
Increment 3b (one grouped suggestions inbox on People & things, no schema change)
landed in [PR #135](https://github.com/Exculpata/exculpata/pull/135) at
`bd3333a` after a clean final-head hosted code review (all 34 earlier review threads
reconciled), passing Quality and maintainer acceptance. Increment 4a (compact cited
answers: numbered citations and one collapsed caveat line per answer, no schema
change) landed in [PR #138](https://github.com/Exculpata/exculpata/pull/138)
at `0817b20` after clean final-head hosted code review, passing Quality and
maintainer acceptance. Increment 3c (a read-only draft of found dates on the
timeline, no schema change; reviewer-created events stay the existing flow) landed
in [PR #139](https://github.com/Exculpata/exculpata/pull/139) at
`a512e3c` after a clean final-head hosted code review (all 12 review threads
reconciled), passing Quality and maintainer acceptance. Increment 5
([assistant actions](product-slices/assistant-actions.md)) is approved: A (assistant
panel basics) landed in [PR #140](https://github.com/Exculpata/exculpata/pull/140)
at `809676f`; B (suggest people, things and dates from an answer's cited passages,
no schema change) is proposed in its own PR; C follows. Each PR follows the [review policy](QUALITY_GATES.md#review-policy). See
the [workspace layout contract](WORKSPACE_LAYOUT.md#compact-matter-bar) and
[entity discovery](ENTITY_DISCOVERY.md#automatic-discovery-after-processing).

Model/runtime changes, unrelated visual work/slice 21, Mac work/PR #34,
PR #87, wider onboarding, later slice-19 work, deployment and supported-release
claims remain unselected or held. Parallel release-readiness evidence continues;
this increment does not qualify or change any model/runtime profile.

## Governing product direction

The [product north star](PRODUCT_NORTH_STAR.md) governs what RecordBench is
building: a free, local-first review workspace for case teams, with excellent
manual review, complete exact search, accountable collection processing,
source-supported investigation, and inspectable matter knowledge. GitHub issues and milestones
are the implementation queue. Feature contracts describe
current behavior; the north star describes intent; release readiness defines
what must be proven before a supported release.

The [September 12 assessment](PRODUCT_DIRECTION_2026-09-12.md) identifies gaps
and subsequent delivery stages. Slice 19's first single-run adapter is landed
as recorded below. Multiple-run aggregation, new questions, background material,
matter memory, production imports and larger capacity remain separately scoped
work. Slice 20 is landed as recorded below. The bounded Linux CPU contributor
node path (B-lite) and Continuity A/B/C are Done. The manual review workspace increment above is selected.

**Last updated:** 2026-10-01

## Continuity C landing receipt

[PR #107](https://github.com/Exculpata/exculpata/pull/107) landed on
September 22, 2026 at `3c814e37adf18a84de9191bad84e0e8a2745e525` through a normal protected
fast-forward. The installed outgoing publication guard passed before branch
publication and before landing. All existing branch protections remained enabled.

[Exact-head branch Quality](https://github.com/Exculpata/exculpata/actions/runs/35775440687)
and [PR integration Quality](https://github.com/Exculpata/exculpata/actions/runs/35775533728)
passed all jobs. Both application runs passed **3,329 tests, nine skipped**;
PostgreSQL passed seven with zero skips. Browser, transcription, deployment,
publication and secret checks passed.
[Requested hosted Codex code review](https://github.com/Exculpata/exculpata/pull/107#issuecomment-5783033824)
completed on the same head with no major issues and no unresolved review threads.

The [real-model follow-up](CONTINUITY_C_ACCEPTANCE_2026-09-22.md#september-22-real-model-follow-up)
passed all three fixed synthetic questions after intact-original prompt refinement.
It preserves both attributed disputed accounts, distinct same-name identities,
original citations and refusal of unsupported note-only claims. Earlier failed
runs remain recorded. This accepts C's focused-answer increment only; other
answer paths, Report compilation, model changes, deployment and supported-release
claims remain outside it.

The separate [post-merge Quality run](https://github.com/Exculpata/exculpata/actions/runs/35778435161)
passed all jobs on the same landed revision: application **3,329 passed, nine
skipped**, PostgreSQL seven passed with zero skips. This is independent of the
passing branch and PR runs above.

## Continuity B landing receipt

[PR #103](https://github.com/Exculpata/exculpata/pull/103) landed on
September 22, 2026 at `d21c1d4849b46b8ae85db19b4d2ce80c8a9669a8` through a
normal protected fast-forward after GitHub rejected the public no-reply address
in its merge API. The installed publication guard passed before each push;
branch protections and all Quality requirements remained enabled.
[Exact-head Quality](https://github.com/Exculpata/exculpata/actions/runs/35720397034)
and [PR integration Quality](https://github.com/Exculpata/exculpata/actions/runs/35720409778)
passed all six jobs. [Post-merge Quality](https://github.com/Exculpata/exculpata/actions/runs/35722012792)
also passed, with an application-only retry after the first attempt reached the
existing 30-minute timeout at 96% progress without a reported test failure.
That cancelled attempt is not a pass and remains in the evidence record.

The user requested hosted code review immediately after landing.
[Hosted Codex code review](https://github.com/Exculpata/exculpata/pull/103#issuecomment-5775776780)
completed on the landed head with no actionable findings; no security-review
completion is claimed. The [brief](product-slices/continuity-b-context-selection.md#landing-receipt--september-22-2026)
records scope, exact validation, migration/restore/export/purge evidence and
historical diagnostic limitations. B does not consume selections in answers,
select C, change models, or authorize deployment or a supported release.

## Continuity A landing receipt

[PR #100](https://github.com/Exculpata/exculpata/pull/100) merged on
September 21, 2026 at `dd367cc3a65fa733d44b612026a9d3912523fed6`.
[Post-merge Quality run 35610419040](https://github.com/Exculpata/exculpata/actions/runs/35610419040)
passed application, PostgreSQL integration, synthetic-browser, transcription,
deployment-contract and secret-scan on that exact revision. Final-head hosted
code review and maintainer landing acceptance completed; the separately requested
hosted security review did not complete and was optional under repository policy.
The installed publication guard passed before landing. The
[bounded brief](product-slices/continuity-a-case-notes.md#landing-receipt--september-21-2026)
retains the synthetic acceptance and review-correction evidence. This receipt
does not qualify a model/runtime, deployment or supported release.

## Current slice-20 landing receipt

Slice **20** — source-backed graph exploration from a saved entity — is **Done**.
The September 19 landing sequence on `main` is:

- Cruise selection: `06f286eac5c62641de18be6db74e84b55228ea5a`.
- Graph implementation: `f5a6ae51b729e7337043d3e532ee150bdd48c4e2`.
- Graph omission-label fix: `6dca8a55b29b9268596f2d89c27fb6314adda77c`.
- Planner fixture-write serialization: `e7a6847a717c22ea97e06608657eab9bad24590c`.

The verified implementation snapshot before this receipt update is
`e7a6847a717c22ea97e06608657eab9bad24590c`.
Its [post-landing Quality run](https://github.com/Exculpata/exculpata/actions/runs/35462727086)
passed all six jobs: application, PostgreSQL integration, synthetic browser,
transcription, deployment contract and secret scan. This receipt closes the
slice-20 queue state only; it does not select slice 21 or authorize visual work,
PR #34, PR #87, deployment or a supported release. Leftover local visual paths
remain outside this update and must not be published.

The pending-acceptance wording in the [slice-20 brief](product-slices/20-graph-exploration.md),
[graph feature contract](GRAPH_EXPLORATION.md) and
[slice overview](product-slices/README.md) describes their pre-landing preparation
state. This landing receipt supersedes that wording for slice 20; this cruise
remains the binding implementation queue. Their feature scope and limits remain
unchanged.

<a id="current-upstream-receipt"></a>

## Historical post-19 receipt (superseded snapshot)

Verified `main` implementation snapshot after slice 19's first adapter:
`95f290bf350742bc77c915eb0269c774069f3633`, merged in
[PR #85](https://github.com/Exculpata/exculpata/pull/85) at
2026-09-12 at 21:59:37 UTC. Its scope prerequisite
[#84](https://github.com/Exculpata/exculpata/pull/84) landed at
`126a468a707a6f9b66d3a4f2565585b54ff98244`; its separate
[post-merge Quality run](https://github.com/Exculpata/exculpata/actions/runs/34719400464)
passed all six jobs: application 2,819 passed, nine skipped; PostgreSQL seven
passed, zero skipped.

The [final PR Quality run](https://github.com/Exculpata/exculpata/actions/runs/34720656875) passed on
`95f290bf350742bc77c915eb0269c774069f3633`: application 2,945 passed, nine skipped; PostgreSQL seven passed, zero skipped; all six jobs green.
[Hosted code review](https://github.com/Exculpata/exculpata/pull/85#issuecomment-5648909015) covered that head after findings
were resolved or reconciled. Security review did **not** run: the authorized [verified quota exception](https://github.com/Exculpata/exculpata/pull/85#issuecomment-5648923555) was recorded for this exact head before maintainer acceptance.
[Full-head maintainer acceptance](https://github.com/Exculpata/exculpata/pull/85#issuecomment-5648957842) preceded merge.
Native gate approval and strict protection were verified before the normal protected fast-forward; independent readback confirmed that protections remained unchanged.

The independent [post-merge Quality run](https://github.com/Exculpata/exculpata/actions/runs/34721488683) passed
on `95f290bf350742bc77c915eb0269c774069f3633` at 2026-09-12 at 22:11:43 UTC:
application 2,945 passed, nine skipped; PostgreSQL seven passed, zero skipped; all six jobs green. This is separate from the final PR run.
The [feature contract](FULL_TEXT_SYNTHESIS.md) and
[dated acceptance](FULL_TEXT_SYNTHESIS_ACCEPTANCE_2026-09-12.md#landing-addendum)
retain synthetic source, browser, recovery and qualified model evidence. The
default model wire probe failed; the passing fixed-corpus evaluation explicitly
used `think=false`. Native baseline-equal failures remain diagnostic. This
accepts the first upstream adapter only, not an installed-node update,
deployment, greater capacity or a supported release.

## Historical post-18 receipt (superseded snapshot)

This receipt records readiness at the time of the first slice-19 promotion.

Verified `main` snapshot after slice 18:
`19d57fbec29325ae9b5ab296e6a2adcb18a5e0ec`, the exact merged head of
[PR #82](https://github.com/Exculpata/exculpata/pull/82), merged
2026-09-12 at 20:06:27 UTC. Its docs prerequisite
[#81](https://github.com/Exculpata/exculpata/pull/81) landed at
`e0301b9839b8c69d07f40c5f5827ab4873f428d6`.

The final [PR Quality run](https://github.com/Exculpata/exculpata/actions/runs/34715322212)
and [exact-branch run](https://github.com/Exculpata/exculpata/actions/runs/34715316801)
passed. Final-head hosted code review completed after its actionable findings
were corrected. Security review did **not** run: the authorized
[verified quota exception](https://github.com/Exculpata/exculpata/pull/82#issuecomment-5648357396)
preceded full-head maintainer acceptance and native approval. Branch protections
remained unchanged.

The independent [post-merge Quality run](https://github.com/Exculpata/exculpata/actions/runs/34716107938)
passed on that exact landed commit: application 2,819 passed, nine skipped;
PostgreSQL seven passed, zero skipped; all six jobs green. This independent
readback establishes readiness for the narrow slice-19 promotion below. The
[slice-18 feature contract](EVIDENCE_ASSERTIONS.md) retains its synthetic browser,
original-source, authorization and clean-restore evidence. Local native baseline
failures remain diagnostic, separate from hosted acceptance. This receipt does
not establish deployment, an installed-node update or a supported release.

## Historical post-17 receipt (superseded snapshot)

Verified `main` snapshot after slice 17:
`e9ab89c792b562c2abf27715d15b5f70d3216900`, the exact merged head of
[PR #79](https://github.com/Exculpata/exculpata/pull/79), merged
2026-09-12 at 18:52:19 UTC. Its prerequisites are merged at their reviewed heads:
13 ([#73](https://github.com/Exculpata/exculpata/pull/73),
`c1ada89bf15ea8f61906de3c62202da4d98e279f`) and 16
([#77](https://github.com/Exculpata/exculpata/pull/77),
`3f96b290ca71adbaef37b92e2d9f4d01cf7d226f`). PostgreSQL CI
([#70](https://github.com/Exculpata/exculpata/pull/70),
`4dea52ce42eeb01ac1cab188a99f7fc44b571ab9`) is also merged; it does not
complete slices 00/08 exact-search acceptance.

Slice 17's [final PR Quality run](https://github.com/Exculpata/exculpata/actions/runs/34711746368)
passed on the exact head: application 2,765 passed, nine skipped; all jobs green.
Final-head hosted code review completed with no major issues after prior findings
were corrected and reconciled. Security review did **not** run: the
[verified quota exception](https://github.com/Exculpata/exculpata/pull/79#issuecomment-5647929760)
preceded separate full-head maintainer acceptance and native gate approval.
The separate [post-merge Quality run](https://github.com/Exculpata/exculpata/actions/runs/34712490352)
completed successfully at 19:05:20 UTC on the exact landed commit: application
2,765 passed, nine skipped; PostgreSQL seven passed, zero skipped; all jobs green.
This is independent of the final PR run. This established the prerequisites for
the subsequent slice-18 promotion.

The [dated slice-17 receipt](ENTITY_DISCOVERY_VALIDATION_2026-09-12.json)
retains local synthetic browser, migration/rollback and restored-original-source
evidence, including baseline-equal local failures. Those failures are diagnostic;
the later hosted pass is separate acceptance evidence. The receipt's pending
final-head review wording describes its preparation time and is superseded by
this landing receipt. None of this establishes a supported release, deployment,
learned-model quality or confidential-casework readiness.

## Historical pre-17 receipt (superseded snapshot)

The following receipt remains evidence for slice 16, not the current tip.
Verified `main` snapshot before slice 17:
`3f96b290ca71adbaef37b92e2d9f4d01cf7d226f`, the protected fast-forward of
[slice 16 PR #77](https://github.com/Exculpata/exculpata/pull/77).
GitHub confirms MERGED at that exact commit; contract issue #76 is closed as
implemented. Final PR CI passed, hosted code findings were reconciled, and a
verified security-quota exception (not a completed security review) preceded
maintainer acceptance and native approval. The separate
[post-merge Quality run](https://github.com/Exculpata/exculpata/actions/runs/34706467703)
completed successfully on that exact landed commit. This is independent of
the already-passing final PR checks. See the
[dated slice-16 receipt](ENTITY_WORKSPACE_VALIDATION_2026-09-12.json) for tested
scope and local baseline failures; those are diagnostic, not waived gates.

## Cruise order

| Step | Work | State |
| ---- | ---- | ----- |
| 0 | Portable baseline on `main`; alpha blockers #30 / #31 closed | Done |
| 1 | Prove `main`: green Quality gates (including synthetic-browser); clean-host CPU install acceptance; install friction documented | Done |
| 2 | Slice **06** — reusable team groups with explicit matter access | Done |
| 3 | Slice **11** — evidence-driven investigation (durable plan, reasons, checkpoints, zero-hit ledger rows) | Done |
| 4 | Slice **12** — deepen full-text coverage as required to feed durable findings | Done |
| 5 | Slice **13** — hierarchical synthesis (issue- then matter-level) with citations through intermediates | Done |
| 6 | Slice **16** — manual People / Places / Things workspace | Done (#77) |
| 7 | Slice **17** — entity discovery and reviewer reconciliation | Done (#79) |
| 8 | Slice **18** — evidence-backed relationships and events | Done (#82) |
| 9 | Slice **19** — first substep: synthesize one terminal full-text run for its criterion | Done — first adapter only (#85) |
| 10 | Slice **20** — source-backed graph exploration from a saved entity | Done — landed at `e7a6847a` |
| 12 | **20b — Linux CPU node (B-lite):** fresh Ubuntu 24.04 x86-64 → service account + Docker → `./install` CPU/local/loopback HTTPS → doctor → first-admin browser sign-in and synthetic practice matter | **Done** — [brief](product-slices/20b-linux-cpu-node.md); [5m13s clean-node receipt and green Quality](LINUX_CPU_NODE_ACCEPTANCE_2026-09-19.md) |
| 13 | **Continuity A:** connect existing Case notes to authoritative saved identities and events/assertions | **Done** — PR #100 at `dd367cc`; [landing receipt](#continuity-a-landing-receipt), Quality **35610419040** |
| 14 | **Continuity B:** explicit, durable reviewer-owned context selection; no answer integration | **Done** — PR #103 at `d21c1d4`; [landing receipt](#continuity-b-landing-receipt), Quality **35722012792** |
| 15 | **Continuity C:** explicit selected context in queued focused answers, immutable submission and dispatch receipts | **Done** — PR #107 at `3c814e3`; [landing receipt](#continuity-c-landing-receipt), fixed-corpus model acceptance and full Quality passed |
| 16 | **Assistant dock passage save:** existing Case notes capture with inline feedback and retained reader context | **Done — PR #123 at `63d807f`** |
| 17 | **Manual review workspace:** persistent source reader, human notes and honest question/answer context | **Next — approved local implementation; draft publication only** |
| ∥ | Parallel release-readiness evidence (`docs/RELEASE_READINESS.md`) | Ongoing alongside active slices |

The September 19 landing completes slice 20 under
[graph and multimodal exploration](GRAPH_MULTIMODAL_EXPLORATION.md) and its
[bounded implementation brief](product-slices/20-graph-exploration.md).
The bounded **20b B-lite** node path is Done with its dated clean-node and
Quality receipt. Code-only contribution remains
`make bootstrap/check`; running the product uses the existing `./install`.
Continuity A, B and C are Done. The manual review workspace increment above is Next.
Slice 21, unrelated visual experiments and additional work on PR #34 or PR #87
remain on hold. High-resolution retrieval work remains
local and outside this PR. Existing text and transcript support is not native
image understanding. Later slice-19 work, model deployment and larger-capacity
claims require their own acceptance. Existing holds and parallel release-readiness
requirements remain.

Slice **12** needed no further product PR. **Review all extracted text** is
already on `main` from [#59](https://github.com/Exculpata/exculpata/pull/59)
(the #47 / #58 family). That work matches
[12-full-text-review-coverage.md](product-slices/12-full-text-review-coverage.md)
and is specified in [FULL_TEXT_REVIEW.md](FULL_TEXT_REVIEW.md): every eligible
extracted unit in a frozen population, durable unit/range outcomes, late-unit
and failed-unit coverage, source-change invalidation, cancellation/resume, and
backup/clean-restore. Selected-passage screening remains a separate mode by
design. Cross-source hierarchical synthesis is **13**, not a 12 gap.

Slice **13** consumes persisted investigation findings through issue groups and
cross-group matter sections, retaining original source/version/location support.
Versioned checkpoints preserve budgets across interruption; partial results,
unsupported findings and omitted groups remain explicit in pages and exports.
Synthetic acceptance covers 24 units, support and contradiction, restart and
backup/clean restore. The documented local-model evaluation retained both decisive
accounts and all 24 citations under its stated evaluation settings. See
[HIERARCHICAL_SYNTHESIS.md](HIERARCHICAL_SYNTHESIS.md) for the receipt and limits.
The final PR application suite passed 2,723 tests (nine skipped), with hosted code
review, a verified maintainer security-quota exception, acceptance and protected
fast-forward of the exact no-reply head.

## Landed slice 17 and its limits

[Entity discovery](ENTITY_DISCOVERY.md) consumes sealed slice-12 unit inventories,
retains each recognized occurrence, and offers reviewer-confirmed alias links,
merge/split and guarded undo. Coverage records visited units, not complete name
recall. Its rule-based extractor has limited recall and can misclassify prose;
no learned NER quality or automatic identity resolution is claimed. Entity work
is exported separately and does not yet feed automatic Report compilation.
The separate [single-run full-text adapter](FULL_TEXT_SYNTHESIS.md) now feeds
saved machine findings into the shared hierarchy within its frozen bounds.

## Landed slice 18 and its limits

[Events and assertions](EVIDENCE_ASSERTIONS.md) preserve typed entity roles,
raw/uncertain dates, supporting and competing originals, and explicit reviewer
status and corrections. Entity detail and paginated chronology expose the
records. Shared names, co-mentions and speaker clusters do not establish identity
or relationships. This manual workflow does not add automatic relationship
inference or entity-to-Report compilation.

<a id="active-implementation-slice-19-first-single-run-adapter"></a>

## Landed slice 19: first single-run adapter and its limits

The [first adapter](FULL_TEXT_SYNTHESIS.md) accepts one terminal full-text run,
its complete criterion and frozen authorized source population. Its versioned
receipt retains original locators, coverage, omissions and human-decision
revisions. Original passages supply evidence; rationales and earlier summaries
do not substitute for them.

Admission retains at most 48 candidates and explicitly omits complete originals
over 6,000 characters. Aggregate original-scan limits refuse incomplete validation;
existing generation bounds remain unchanged. Source, source-set, access, ledger
and worker-attempt changes fence admission, recovery and saved output. Human
decisions remain frozen context, separate from evidence.

The terminal-review action reuses the hierarchy inspector and direct JSON,
Markdown and Word exports. Report copy/compilation remains unsupported for this
adapter. The [dated receipt](FULL_TEXT_SYNTHESIS_ACCEPTANCE_2026-09-12.md)
records 24-original support and contradiction, explicit omissions, keyboard
recovery, durable charges, complete-runtime restore and matching-reader rollback.
The model result retains its explicit evaluation-setting qualification.

Only this first substep is Done. Multiple-run aggregation, new questions,
adjustable capacity, entity-to-Report integration, background/memory,
production imports, held Mac integration and deployment remain outside it.

Every subsequent slice must state its reviewer outcome, actual processed scope,
original support, human/machine distinctions, and recovery path. Manual review
and accessibility are part of delivery, including useful behavior without AI.

## Hold

Do not take these as the next ticket:

- Apple Silicon / Mac exit path (#33, #34)
- Reopenable matters and mega-integrations
- Charging-document matter frame (after cast exists)
- Agentic playbook / single-shot “Prompt 1” client-summary architecture
- One-shot “ask the corpus” as the complex-case product story
- Raising one retrieval limit instead of slices 11 / 13

## Complex-case direction (not this sprint)

Client-specific discovery summaries on large matters need multi-step hybrid
exact + semantic search, hit ledgers, and cited-or-gap synthesis—not smarter
one-shot rerank.

Ship that capability as three stages after 06:

1. **11** — multi-hop search ledger honesty (including explicit zero hits)
2. **13** — cited hierarchical summary on modest synthetic corpora
3. **16 → 18** — cast / aliases / relationships

Do not claim million-chunk Prompt-1 scale until capacity policy is a separate
gate (`docs/INVESTIGATION_BUDGETS.md`). The application owns citations, zero-hit
rows, budgets, matter isolation, and human-vs-machine provenance. Models may
propose queries and draft prose only.

## PR rules

1. One cruise step (or named sub-step) per PR stack.
2. Implement only the row marked **Next** unless this file is updated first.
3. Follow the matching brief in `docs/product-slices/`.
4. Synthetic fixtures only; no private deployment material.
5. Preserve authorization, citations, human-vs-machine provenance, offline
   posture, and explicit incomplete states.
6. When a step lands on `main`, update the State column in this file.

## Pointers

- [product-slices/README.md](product-slices/README.md)
- [06-team-groups.md](product-slices/06-team-groups.md)
- [11-evidence-driven-investigation.md](product-slices/11-evidence-driven-investigation.md)
- [FULL_TEXT_REVIEW.md](FULL_TEXT_REVIEW.md)
- [13-hierarchical-synthesis.md](product-slices/13-hierarchical-synthesis.md)
- [FULL_TEXT_SYNTHESIS.md](FULL_TEXT_SYNTHESIS.md)
- [FULL_TEXT_SYNTHESIS_ACCEPTANCE_2026-09-12.md](FULL_TEXT_SYNTHESIS_ACCEPTANCE_2026-09-12.md)
- [RELEASE_READINESS.md](RELEASE_READINESS.md)
- [CONTRIBUTOR_BACKLOG.md](CONTRIBUTOR_BACKLOG.md)
