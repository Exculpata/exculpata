# Full-text synthesis acceptance — September 12, 2026

This receipt covers the first slice-19 adapter: one terminal full-text review,
its exact criterion, frozen source and human-decision context, the existing
hierarchy, inspector, and direct exports. All inputs are synthetic. The
[feature contract](FULL_TEXT_SYNTHESIS.md) defines the bounds and lifecycle.

Status note: Preparation-time pending-gate statements below describe the local
candidate when recorded. The [landing addendum](#landing-addendum) records the
later merge and independent post-merge checks. Earlier measurements and
limitations, including the refreshed review-correction evidence, are unchanged.

Scope: [issue #83](https://github.com/neilofneils404/exculpata-oss/issues/83) and
the prerequisite scope-documentation [PR #84](https://github.com/neilofneils404/exculpata-oss/pull/84).
These references do not assert that either the scope PR or implementation has
landed. Final candidate-head validation, hosted CI, hosted review, and maintainer
acceptance remain pending in this receipt. Local evidence is not a release gate
substitute, and no installed node or deployment was changed.

## Deterministic application evidence

The integrated fixture creates actual stored originals, extraction-unit metadata,
a full-text review ledger, and a terminal review through the application. Only
extraction/classification providers and synthesis responses are controlled for
these tests. They exercise the adapter, durable worker lifecycle, source verifier,
and exports; they do not measure model quality.

| Case | Observed result |
| --- | --- |
| Support and contradiction over 24 original units | Unit 1 support and unit 24 contradiction survive issue and matter synthesis with original citations |
| No-finding, failed, unavailable, empty, and zero-unit inputs | Coverage and partial status remain inspectable; none becomes positive evidence |
| Unsupported saved rationale | Rejected against the original, counted as `unsupported_finding`, and absent from generation evidence |
| Both decisive statements after character 6,000 in one original | Entire original receives `oversized_original`; receipt includes its identity and full length; no prefix or rationale substitutes for it |
| 105 candidate findings | 48 admitted and 57 counted as `finding_limit`; an omitted late contradiction is not displayed as recognized |
| Changed input or access | Human revisions, original bytes, source versions, source-set additions/removals, and revoked membership refuse stale admission or writes |
| Superseded, cancelled, or failed attempt | Durable charges persist; stale worker checkpoint and completion paths cannot bypass attempt validation |
| Interruption and exhaustion | Saved nodes are reused, requests and first-start deadline are preserved, and exhausted output is explicitly partial |
| Frozen historical context | Completed output retains its input receipt after a human edit or original review deletion, while source and reader checks remain mandatory |
| Deletion and active purge | Missing input before admission creates no job; deletion during work refuses subsequent writes; active synthesis prevents purge |
| Complete criterion | Include/exclude guidance reaches generation and exports; oversized combined criteria refuse without truncation; instruction-only 2,000-character boundary is preserved |
| Cancellation and restart races | Two database connections cannot overwrite a newer request charge or saved node with older JSON |
| Missing, zero or unknown version | Reserved adapter records cannot enter legacy processing/retry or erase charged work; derived output is refused |
| Final source-scan revocation | Inspector/export recheck membership after the potentially long source scan and refuse revoked readers |
| Atomic replay and audit | Concurrent duplicate requests create one job and one queue event; operation attribution excludes criterion, evidence and human-note text |
| Direct exports | JSON, Markdown, and Word preserve receipt, hierarchy, partial status, and original citation identities; corrupt input is refused |

The initial three adapter suites contained **114 synthetic cases**. A focused run
including the new version fences and legacy investigation, export and Report
coverage passed **225 tests**; the final lifecycle-fence suite, with later audit
and replay cases, passed **52 tests**. The adapter-bound suite passed **29 tests**,
including complete criterion guidance and post-admission receipt bytes.
Report-conversion guards hide unsupported adapter choices and refuse direct copy
or compilation before creating a Report or calling generation. Existing full-text
review Report material remains available. These are local candidate checks, not
hosted final-head acceptance.

Reproducible focused entry points:

```console
python -m pytest -q tests/test_full_text_synthesis.py \
  tests/test_full_text_synthesis_adapter.py \
  tests/test_full_text_synthesis_fences.py
```

Use the project's isolated dependency environment. The accepted native baseline
`19d57fbec29325ae9b5ab296e6a2adcb18a5e0ec` had **125 failures, 2,694 passes,
and 9 skips** in the broad application run. The later candidate had **125
failures, 2,785 passes, and 9 skips** in 307.53 seconds. An exact failure-node
comparison found no new or resolved failures. This broad run collected 91 new
cases; the subsequent 23 version, receipt-bound, audit and replay cases are
covered by the focused suites above. Native runs use only the command-scoped
`CASE_INTELLIGENCE_STORAGE_RESERVE_GIB=0` adjustment for the bounded disk-reserve
issue. Equal native baseline failures are diagnostic comparison evidence, not
a clean pass or a waiver. Hosted results must cover the final reviewed head.

## Browser acceptance

The [browser harness](../scripts/browser-accept-full-text-synthesis.py) passed
seven recorded checks using Chrome and ChromeDriver **153.0.8010.36**. Screenshots
were inspected at desktop and 390-pixel widths.

1. A terminal six-source review with 24 admitted original units exposes the
   synthesis action alongside its separate coverage outcomes.
2. Keyboard submission reaches a controlled interruption after two issue nodes
   were saved and three requests charged.
3. Keyboard Resume preserves those nodes and spend. Completion records 13 charged
   requests and 12 actual responses; the interrupted request remains charged.
4. An unsupported rationale and the oversized original with decisive late text
   remain inspectable omissions and do not enter model evidence.
5. The shared inspector, source drawer, and full reader expose supporting and
   competing accounts on original pages 1 and 24, with narrow-screen reflow.
6. Browser downloads of JSON, Markdown, and Word preserve the frozen receipt,
   hierarchy, original accounts, and partial status; receipt hashes are recorded.
7. With AI unavailable, keyboard-driven human validation and original navigation
   remain usable.

This is deterministic workflow and browser evidence. It neither changes the
6,000-character original limit nor qualifies model quality.

## Backup, clean restore, and matching rollback

The [restore drill](../scripts/full-text-synthesis-restore-drill.py) passed with
the exact pre-upgrade reader
`19d57fbec29325ae9b5ab296e6a2adcb18a5e0ec`.

- The earlier reader created a terminal synthetic review and a complete stopped
  pre-upgrade runtime backup. Every file hash was verified.
- Current code interrupted synthesis after two saved intermediate nodes and
  durable request charging, then created a complete stopped upgraded backup.
  Both snapshots contained six runtime files, including source originals.
- The original runtime and original backup paths were made unavailable. A clean
  restored runtime reused both saved nodes, completed with 13 charged requests,
  and preserved the input receipt exactly.
- All 24 source passages and their original bytes were readable after restoration.
  JSON, Markdown, and Word exports completed and their hashes were recorded.
- A separate clean restore of the matching pre-upgrade backup opened successfully
  with the pinned earlier reader. Integrity and foreign-key checks passed.

The drill uses stopped synthetic SQLite runtimes and generated source files.
It is complete local backup/restore and matching-reader rollback evidence; it
does not establish PostgreSQL recovery, installed-node readiness, or deployment
acceptance. No SQL migration was added.

## Pinned local model and default-wire failure

The model was the already staged **qwen3.5:4b Q4_K_M**, served by **Ollama 0.33.3
on Apple M4**, with Apache-2.0 license recorded. Its exact Ollama manifest is:

```text
sha256:2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd
```

The installed artifact verifier rechecked manifest and blob sizes and hashes.
The evaluation used isolated temporary copies and a dedicated loopback process,
with the inherited home environment unchanged and writes to the user home denied.
No model was downloaded and no installed node was modified. The owned model
process was stopped and its loopback port was independently confirmed closed.

The [evaluation harness](../scripts/evaluate-full-text-synthesis.py) first sent
one real request through the terminal-ledger adapter using the application's
default Ollama wire request. It failed after **48.73 seconds** with
`GenerationUnavailable: The answer service did not return structured content.`
One request was durably charged. The probe deliberately stopped before repair
retries or quality evaluation. Its `probe_completed` status and successful probe
process exit indicate an observed probe, not a quality pass; the receipt's
`passed` field is false.

## Qualified synthesis evaluation

A separate run used the explicit evaluation-only `--disable-thinking` switch:
`think=false`, temperature 0.1, 8,192 context tokens, 1,200 output tokens,
90-second request timeout, and five-minute model keep-alive. It flowed through
an actual terminal full-text ledger, the new adapter, the real hierarchy, and
the application source verifier. Extraction and classification were controlled
fixtures; synthesis responses came from the pinned local model.

The fixed corpus had 26 candidate findings: 24 admitted originals, one unsupported
saved rationale, and one original containing both decisive accounts after
character 6,000. The latter two were explicitly omitted. Two no-finding ranges,
one failed range, an unavailable source, an empty unit, and a sealed zero-unit
source remained accounted for. The result remained partial.

| Measure | Result |
| --- | --- |
| Original statements retained | 24 / 24 |
| Decisive supporting and competing accounts | 2 / 2, including unit 24 |
| Displayed claims exactly matching their cited original statement | 24 / 24 |
| False claims in this fixed corpus | 0 |
| Injected unsupported Jupiter claim | Absent |
| Saved hierarchy | Six issue groups and six matter sections |
| Charged generation-service requests | 12 |
| Total elapsed time | 117.95 seconds |

An independent audit of the saved receipt confirmed all 24 distinct original
unit citations, exact claim-to-source matches, admission accounting, partial
status, and inspectable oversized omission. The audited model receipt SHA-256 is
`9c87d7907e5cbdfd6ff34dc866ca6447cfd502e7520de0a909095d9876dcf2e9`.
The harness subsequently gained stronger corpus-digest, accounting, and exact
claim-to-citation checks. The existing receipt passed the independent checks,
and a deterministic harness regression covers the updated checks; the model run
was not repeated after that harness-only strengthening.

The engine's unchanged cap is 32 generation-service requests. A request permits
one existing verification repair, so the upper bound is 64 provider attempts,
each with at most 1,200 output tokens. The model receipt records service requests;
it does not separately measure provider attempts or assert that no repairs ran.

The harness requires an explicitly served loopback endpoint and verifies the
expected model digest. Its `--default-wire-probe` and `--disable-thinking` modes
are separate, mutually exclusive runs; each requires `--output` pointing to a
new generated receipt outside the repository. The scripts do not stage or
download models. The restore harness accepts `--baseline-ref` to pin the earlier
reader; the browser harness requires explicit `--chrome-binary`, `--chromedriver`,
and an external generated-output directory.

This qualifies the stated small corpus under the explicit evaluation settings.
It does not qualify the application's default request, arbitrary long originals,
other portfolios, different hardware, or confidential material. Production
generation settings, model selection, admission capacity, and installed nodes
remain unchanged. Hosted final-head checks, required review, and maintainer
acceptance are separate outstanding gates.

## Review corrections and refreshed model evidence

Review of implementation candidate `1c6870d1a880998ee70eb9fb91db84328dc2f85f`
identified a clipped duplicate of the criterion in working context, a source-set
edit window during original scanning, and missing aggregate scan limits. The
primary generator question already preserved the complete criterion. The
correction removes its duplicate from working context and keeps stage
instructions separate. Four transport regressions verify the complete
2,000-character criterion, ending in exclusion guidance, in both issue and
matter requests for Ollama and OpenAI-compatible clients. Existing question,
working-context, evidence and output limits remain unchanged. An independent
review of this correction found no actionable regression; its transport and
hierarchy checks passed 26 tests.

The source scanner now uses one aggregate budget across all batches, including
uncited and unsealed sources. Preparation's original resolver uses the same
bounded reader; admitted citations are checked within the population pass.
Selected-set scope is rechecked after scanning. The
[feature contract](FULL_TEXT_SYNTHESIS.md#frozen-input-and-original-evidence)
records the character, unit, record and cooperative time limits. Exhaustion
refuses admission or derived output without accepting a partial source scan.

The corrected three adapter suites passed **126 tests**. Eight new scan
regressions use actual bounded readers: 1,000 derived-unit files scan once each;
a cancelled, unsealed 21,000-unit population refuses at the first unit above
20,000; preparation refuses text, serialized-input, record and deadline
exhaustion; and separate workspace connections change selected-set membership
during the final inspector/export scan, receiving 409 without source text.
Broader full-text, Report streaming and reader coverage passed **166 tests**.
The criterion correction's adapter, hierarchy, generation and legacy planner
run passed **203 tests**. Compile and working-tree publication checks passed.
The corrected source scanner also passed all seven pinned-browser checks and
a repeated complete-runtime clean-restore/matching-reader rollback drill, with
the same 24 originals, two reused nodes and 13 retained request charges.

Because the working-context prompt changed, both model modes were repeated
with the same pinned artifact, isolated runtime and settings above. The default
wire again failed structured output, this time after **48.57 seconds**, with one
charged request and no quality evaluation. The explicit `think=false` run
completed in **107.48 seconds** with **24/24 exact original statements**, both
decisive accounts, zero false displayed claims and 12 charged service requests.
The same unsupported and oversized candidates remained omissions and the
result remained partial. Independent validation checked all 24 distinct
original units, exact claim-to-citation matches, hierarchy state and admission
accounting. The refreshed model receipt SHA-256 is
`7f691f7b2a10a80d5c9a07da45462d06a748142443d89561629135d832989b27`.
The owned model process was stopped and its port independently confirmed closed.
This supersedes the earlier prompt's model evidence for the corrected candidate;
it retains the same qualification limits and does not establish default-wire
quality or hosted acceptance.

## Landing addendum

Recorded 2026-09-12. [PR #85](https://github.com/neilofneils404/exculpata-oss/pull/85)
merged at `95f290bf350742bc77c915eb0269c774069f3633` on 2026-09-12 at 21:59:37 UTC; the final reviewed
head was `95f290bf350742bc77c915eb0269c774069f3633`. The
[cruise receipt](EXIT_ALPHA_CRUISE.md#current-upstream-receipt) records final-head
Quality, hosted code review, the actual security gate, maintainer acceptance,
and the separate successful post-merge Quality run.

That landing supersedes the preparation-time pending hosted gates. It does not
change the native baseline failures, default-wire failure, qualified `think=false`
model result, or complete-original omissions over 6,000 characters. Only the
first single-run adapter is Done. Report copy/compilation, multiple runs, new
questions and greater capacity remain outside it. No installed-node update,
deployment, supported release or subsequent product priority is claimed.
