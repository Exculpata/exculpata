# Synthetic workflow investigation and repairs

Public baseline tested: `d43e142769b5d57a901fb58439bf841ed954b8b4`.
The fetched public `main` matched that revision at the start of this investigation.
All reproductions use newly generated fictional Pump Cedar material. Historical
audit totals are not included in the results below. Changes are review candidates;
no deployment or external application environment was changed.

## Findings and changes

| Issue | Classification | Evidence and disposition |
| --- | --- | --- |
| Investigation rejects its final checkpoint | Reproduced and repaired | Real primary and modality retrieval deduplicate three anchors. The selector inspects two adjacent transcript passages, producing five selected passages while the old checkpoint records only three candidates. The producer-driven regression and Chrome journey reproduce the reported counter and checkpoint errors. Accounting now records actual inspected neighbors before admission and deduplicates them against retrieved anchors. Source counts remain distinct from passage counts; validators remain active. |
| Mixed document/transcript conversation cannot become a Report | Reproduced and repaired | Unchanged transcript-only and mixed synchronous/queued answers have working source links but fail Report compilation on the baseline. Answer persistence used display-only citation fields. It now saves the existing canonical identity/version/location and exact full-unit text digest. The existing strict resolver can verify the text without repeating excerpts in every claim or increasing the message-size limit. PDF-only compilation already worked. |
| Stale saved answer copied to note/direct Report | Reproduced during investigation and repaired | Editing the transcript through the application leaves its moment token stable. The old capture paths then resolved current text behind an old claim. Capture now applies the existing exact-support resolver under source and authorization guards. Independently cited source qualifications are also retained and checked when copying a whole answer. |
| Full-text completion misses relevant evidence | Needs more evidence for learned-model behavior | All extracted packets and original-source links are checked. Controlled gold decisions retain all four relevant sources and exclude the control. An intentionally injected miss still completes coverage, demonstrating that completion cannot establish recall. No actual-model miss is attributed to prompts or extraction. |
| Repetitive or unhelpful follow-up/synthesis | Needs more evidence for semantic usefulness | The current verifier coalesces exact duplicate claims and retains truthful omission notices. The new boundary test rejects an invented purchase-order claim and keeps supported uncertainty. Exact-duplicate handling is already present in public commit `a663397`; this does not establish useful consolidation of paraphrases or overall synthesis quality. No speculative prompt/model changes were made. |
| Discovery suggestion burden | Reproduced as current occurrence-level behavior | Five generated sources yield ten suggestions and six labels, including repeated Pump Cedar/date mentions. Navigation, source attribution, review candidates, manual merge and Undo pass. These are suggestions, not ten adjudicated identities. Automatic reconciliation was not changed. |

Full lower-priority rubrics, corpus boundaries and continuation are in
[workflow quality evaluation](WORKFLOW_QUALITY_HANDOFF.md). The 30-source matrix
contains 24 relevant variants and six controls from one fictional matter. It is
not an independently sampled representative domain evaluation. No live model
precision, recall, follow-up or synthesis score was produced.

## Verification boundaries

New regressions are in:

- `tests/test_investigation_candidate_accounting.py`: real producer, primary and
  supplemental retrieval, duplicate/overlapping candidates, zero-hit/unavailable
  outcomes, bounded admission, resume, unchanged old invalid history, original
  citations, changed transcript rejection and investigation/Report exports.
- `tests/test_saved_answer_report_support.py`: persisted answer readback through
  a separate connection, PDF/transcript/mixed and note compilation, exact export
  content, supported transcript edits, stale recapture, qualifications, legacy
  compatibility, recovery, forged provenance and matter/actor rejection.
- `tests/test_saved_answer_payload_budget.py`: a real verified eight-claim answer
  retains all exact citation digests within the unchanged persistence limit.
- `tests/test_workflow_quality_handoff.py`: gold scoring, real extraction/source
  packets, coverage versus controlled recall, stale human override, verifier
  omissions/duplicates/abstention, discovery attribution and reversible merge.

`scripts/browser-accept-workflow-handoff.py` creates fresh PDFs, text documents
and locally generated speech with matching authored segment timing. It clicks
Answer, Make report and Investigate more deeply in actual Chrome. Primary search
and transcription transport are controlled to exercise the accounting boundary;
generation echoes supported sentences and still uses production verification.
The source readers, recording seek position, persisted jobs, Report conversion,
JSON counts and Word/Markdown support content are checked. Baseline Chrome shows
both original failures; candidate Chrome completes both workflows. The existing
intake and Report bundle/readiness Chrome journeys also pass.

Native macOS requires explicitly disclosed fixture accommodations: installed
FFmpeg/FFprobe replace missing Linux executable paths, and the unchanged PDF
child helper omits only the unsupported native `RLIMIT_AS` call. Other PDF
limits and actual extraction remain. New unit tests use fresh synthetic PCM and
controlled transcripts; the browser uses generated speech. Neither evaluates
speech recognition. These checks do not qualify Linux process isolation.

The full unadapted native baseline application suite returned **2,820 passed,
125 failed, 9 skipped**. All **194 transcription tests** passed. Compileall,
Compose graph validation and publication content checks passed. The focused
combined new workflow/quality suite returned **43 passed**, including the
eight-claim/four-source payload-and-Report regression. Existing focused investigation/export and
Report/media suites passed. The full unadapted native candidate returned
**2,835 passed, 127 failed, 26 fixture errors, 9 skipped**. All 125 baseline
failure nodes are identical. The 28 added failures/errors are the new PDF/media
fixture paths hitting the same native tool/resource limitations; all 43 new
tests pass with the disclosed fixture accommodations. This is diagnostic
comparison, not a green contributor gate. The accompanying delivery receipt
records exact nodes, commits and content hashes.

No model revision, storage schema, source-token contract, authorization policy,
or deployment behavior changes are included. No acceptance-pack files were
changed.

## Returned Linux validation

The returned Linux package validates the exact four production-file changes on
the same public baseline. Its patch and all nine artifact checksums were checked
on receipt. The native Linux contributor gate reports **2,945 application tests
passed, 9 skipped**, **194 transcription tests passed**, and successful
compilation, Compose and publication checks. Its independently authored 25-case
suite passed; both new browser workflows completed while the baseline reproduced
both failures. The baseline full gate separately failed one unchanged
timestamp-fixture precondition, which does not establish a workflow regression.

Linux received the inline patch, not the local bundle or 43 new local tests.
Do not attribute its results to the complete unpublished commit. The returned
tests largely overlap the existing stronger regressions and support no additional
production change. See the [returned evidence and precise limits](WORKFLOW_LINUX_FEEDBACK.md)
and the [Linux validation handoff](WORKFLOW_LINUX_VALIDATION.md) for further runs.

Final preparation for public review found a separate regression in the candidate:
the shared Report resolver applied its 6,000-character full-unit limit to saving
Notes. The public baseline could save a longer unchanged source unit as a bounded
preview with its full-unit digest. The follow-up preserves that Note behavior
through explicit preview handling after full identity and text-basis validation.
Direct and compiled Reports retain their existing full-unit limit. This follow-up
is not covered by the earlier four-file Linux hashes; its regression and current
PR checks are separate evidence.

The follow-up's four HTTP Note-capture cases pass on the public baseline, fail on
the former candidate, and pass after correction. Its 23 new cases also exercise
changes beyond the preview, forged saved support, unchanged Report limits and
complete source-scan budgets. All 66 focused workflow cases and 194 transcription
tests pass on the corrected candidate. The generated-speech Chrome journey also
passes with the disclosed native accommodations. These are separate from the
earlier Linux receipt and do not replace the [review policy](QUALITY_GATES.md#review-policy).

The corrected candidate's unadapted macOS full application run returned
2,848 passed, 127 failed, 36 fixture errors and 9 skipped. Every previously
collected node retained its outcome. The new test file adds 13 native passes and
10 transcript-fixture errors from the documented native media prerequisites;
all 23 pass with the disclosed fixture accommodations. Compilation, Compose,
publication content and independent secret scans also pass. This remains a
bounded native comparison, not a green application gate.

## Hosted validation and evaluation review corrections

Public CI for `bbdaaef5a2a72183eef140b7c6541b5e5b751102` completed all six
Quality gates successfully. Its unadapted Linux application run returned
**3,011 passed, 9 skipped**. The [exact application job](https://github.com/Exculpata/exculpata/actions/runs/34732508452/job/103657780581)
covers the complete candidate at that revision, including the Note-preview
follow-up; it is separate from the earlier returned patch-only receipt.

Hosted code review at that revision identified two evaluation-tool provenance
gaps. The runner checked its mutable model tag only once, and the fixture
fingerprint omitted the evaluated questions and semantic rubric. Synthetic
transport reproduces a mid-run tag change that still writes the original
digest on the old runner. The correction checks tag identity during availability,
around every inference and repair request, and before receipt creation. A lost
identity aborts without a scored receipt; a generation failure with verified
identity retains its ordinary uncertainty outcome. Tag snapshots do not provide
immutable per-response attestation, as the receipt and quality handoff explain.

Both questions and every rubric definition now contribute to the fingerprint,
and the authored corpus manifest includes these controls. Mutation tests fail
against the previous fingerprint for both questions and all seven rubric
definitions. Earlier receipts remain intact with their original, narrower hash.
These corrections change no application workflow, source-support validator,
model artifact or stored user work. The [review policy](QUALITY_GATES.md#review-policy) applies to the
corrected commit; the successful checks above apply to their recorded revision.

Local validation of these review corrections passed **103 focused checks**
(100 workflow/evaluator cases and three frozen acceptance-pack checks) with the
existing disclosed native PDF/media accommodations. The unadapted macOS suite
returned **2,882 passed, 127 failed, 36 fixture errors and 9 skipped**. All 34
added tests passed; every previously collected node retained its outcome, with
identical failed/error node sets. This is still a bounded native comparison.
All 194 transcription tests, compilation, Compose and publication content
checks passed. No additional browser run is claimed for this evaluator-only
follow-up; the existing workflow browser evidence above retains its scope.

## Fixture readiness correction

At `0251b3093b4bfc25c3d160c0df9c00ccba6dc0a9`, the pull-request application
job passed **3,045 tests with 9 skips**, while the separate push application
job failed two investigation tests and three Report fixture setups. Both jobs
tested the same source tree. The [failed run](https://github.com/Exculpata/exculpata/actions/runs/34758515661)
remains part of the evidence; the passing run does not erase its fixture races.

The investigation fixture treated a ready source document as permission to
query, although the media worker had not yet finished its durable ingestion job.
The Report fixture could submit the recording-check Continue action twice:
the document still showed needs-review after the first action had queued its
job. The second action correctly received a conflict response. These failures
occurred during fixture setup/readiness, before the intended workflow checks;
they do not establish product source-verification or investigation defects.

The fixtures now wait for the application's canonical matter readiness and
all expected searchable sources. Recording decisions require the committed
cancelled preflight state and are submitted only once per inspection. Real
workers and HTTP recording decisions remain active. Deterministic event-gated
regressions hold the affected publication transitions so that both old races
can be tested without relying on scheduler timing or increasing sleep delays.
No production code or validation contract changes in this correction.

Both new regressions fail on the old fixtures with the original readiness
rejection and HTTP 409, then pass with the correction. Both affected test files
pass all 36 cases, and the two boundary tests pass in five separate pytest
processes (10 passes). The combined workflow/evaluator and frozen-pack run passes
105 checks; all 194 transcription tests also pass. These local fixture runs use
the previously disclosed native media/PDF accommodations. The Linux handoff's
existing file-level commands automatically include the new cases.

The unadapted macOS full suite returned **2,883 passed, 128 failed, 36 fixture
errors and 9 skipped**. Every existing node kept its previous outcome. The new
investigation boundary test passed; the new Report boundary test encountered
the same native recording-inspection HTTP 400 already reported by that fixture.
Both boundary tests pass with the disclosed native tool accommodations.
Compilation, Compose and publication checks passed. This bounded native
comparison requires fresh unadapted Linux CI for the corrected commit.

## Compatibility and recovery

Historical investigation counters are never clamped, inflated or rewritten.
An already-invalid checkpoint remains invalid on resume. Start a new
investigation with the same question and scope; keep the old run for history.

Existing document and qualifying legacy digest-bound transcript references
continue to work when their exact source basis matches. A current moment token
without a saved text digest or exact excerpt cannot be safely upgraded from
playback alone. Review its source, ask again in a **new conversation**, and
compile that new work, or create a newly reviewed note directly from the source.
Reopening the old answer does not establish the missing text basis. Old answers,
notes, human decisions and review history remain preserved. Changed transcript
support remains rejected for compilation, copying and affected Report exports.

Live independent-user authorization, destructive closure/recovery operations,
other ingestion formats, full accessibility, throughput and actual-model quality
were not qualified by these scoped changes.
