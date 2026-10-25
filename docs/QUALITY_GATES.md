# Quality gates

## Review policy

Required to merge: all **6 required CI checks** are green, all review
conversations are resolved, and a maintainer performs the merge.

Hosted Codex review is **OPTIONAL**. When used, request it once on the final head.
It is expected only for changes to authentication/authorization, schema or
migrations, exports, model pinning, publication or secret tooling, or
`.github/workflows`. Apply the `needs-hosted-review` label to those PRs; the label
signals review scope and does not add a merge gate.

There is no acceptance-comment ritual and no exact-SHA reconciliation. Address
findings through the normal PR discussion. Security-sensitive changes should
explain the affected boundary, abuse cases, mitigation and synthetic regression
evidence. Do not claim a review passed if it did not run.

Do not request reviews automatically on PR open or synchronize. Keep automatic
Codex review disabled in the integration settings; this document does not change
those settings. [Publication safeguards](PUBLIC_ALPHA.md#prevent-disclosure-before-publishing)
apply before upload, independently of review.

## Local validation

Check a PR description locally with
`python scripts/check-pr-body.py --head <full-head-sha> --base main body.md`.
Pass `--allow <full-base-sha> --allow <full-merge-base-sha>` to also accept the
base and merge-base revisions; the caller supplies these, without a Git lookup.
Omit the file (or use `-`) to read UTF-8 stdin. The checker prints one line per
problem and exits 1 on findings or unreadable input, 0 on clean text. CLI usage
errors exit 2. It is not wired into CI.

This literal-text check recognizes standalone 7-40-character hex tokens containing
both a digit and an a-f letter, case-insensitively. It ignores URL contents,
all-digit IDs, all-letter words and 64-hex digests (including `sha256:` values).
Head and allowed inputs must be full 40- or 64-hex object IDs; their 7-40-character
citations match by prefix. Only explicit current-head claims are checked:
`current head`, `current PR head`, `current branch head`, `latest head`, `Head:`,
`Current:`, and `on/against/at/for this head`. Context ends at a line break,
semicolon or sentence-ending punctuation followed by whitespace or end of line.
Within that context, the nearest preceding current or historical marker owns a
citation; a citation before any marker uses the first marker. Historical markers
are `Based on main` (also `Based on current main`), `Regression baseline`,
`starting commit`, `Earlier evidence` and `earlier head`. They allow historical
citations until the next marker or context boundary. Unlabelled earlier evidence
outside a current-head claim needs no special label.
It also rejects em dashes and, when the base is `main` or
`refs/heads/main`, the phrases `Stacked on #N` and `after rebase`, ignoring case
and allowing whitespace between words. Unsupported head/base/allowed inputs fail
explicitly through `check_body`; the shared bounded checker contract exercises
invalid-head and invalid-allowance probes. This does not verify test results, resolve Git history or
judge whether prose accurately describes earlier evidence; review still does.

Run `make bootstrap` to install dependencies and the external pre-push hook, then
`make check-fast` before pushing. See [development setup](DEVELOPMENT.md) for
Python 3.12, host packages and custom environment paths.

`check-fast` runs compilation, standard and Kerberos Compose validation,
read-only installer diagnostics, working-tree and committed-history publication
checks, Gitleaks 8.30.1, parallel application tests, transcription tests and the
PostgreSQL integration suite. It needs Docker Compose, the pinned Gitleaks binary
and `CASE_REVIEW_POSTGRES_TEST_DSN` pointing to a disposable synthetic PostgreSQL
17/pgvector database. Missing prerequisites fail; they are not passes. Browser
journeys run separately as described in [browser validation](BROWSER_ACCEPTANCE.md).
No GPU or model weights are required for the contributor suites.

## CI scope

The [Quality workflow](../.github/workflows/quality-gates.yml) runs on PRs,
`main` pushes, merge groups, a daily schedule and manual dispatch. Ordinary
feature-branch pushes do not duplicate PR runs. Synthetic browser journeys run
on merge groups, schedules and manual dispatch, and on PRs changing
any path under `scripts/` containing `browser`, or any file beneath a `static/`
or `templates/` directory under `src/`, including nested assets and new names.
There are no extension or example-file exceptions: even
`scripts/run-browser-acceptance.py.example` requires browser coverage. The existing
browser harness dependencies also require coverage: the runner, synthetic
environment, dependency installer,
demo and theme-contrast helpers, `config/browser-testing.json`, `pyproject.toml`,
runner contract tests, scope classifier, and the quality workflow itself. The complete PR diff
includes added and modified scripts, deletions and both sides of renames;
unavailable comparisons conservatively require browser coverage. A skipped PR
browser job is not a browser test pass. Fork PRs use the upstream PR run; forks
need not enable Actions.

The separate network isolation smoke runs on labeled `network-smoke` PRs,
merge groups, weekly on Monday at 06:43 UTC, and manual dispatch. It tests
synthetic OIDC and independent TCP/DNS boundaries, including internal DNS
controls, six causal container-loopback DNS challenges, a reachable recorder and
a deliberately forwarding negative control. It builds the production application
and relay images with production network and routing settings, and tests real
Authlib redirect/TLS behavior and cleartext/suffix proxy denials. It retains a
content-free summary. A skipped run is not acceptance; the weekly schedule uses
the default branch once the workflow is merged.

Plain documentation changes use a bounded fast path. The `application` check runs
lightweight documentation and scope contracts with pytest. PostgreSQL,
transcription and deployment suites are explicitly not applicable; publication
and secret scanning still run. The fast path allows regular, non-executable
Markdown under `docs/` and root `README.md`, `CONTRIBUTING.md`, `CHANGELOG.md`,
`THIRD_PARTY_NOTICES.md`, `SECURITY.md` and `CODE_OF_CONDUCT.md`. Both sides of a
rename must qualify. Assets, symlinks, executable files, agent instructions,
fixtures, unknown paths and mixed changes run the full suites.

PR classification compares the complete head against its merge base with the
target. Main pushes compare the previous and new revision. Git's complete
NUL-delimited diff is used without rename heuristics or a paginated API file list.
Unavailable history, unrecognized metadata, empty or oversized diffs and force
pushes conservatively run the full suites. No label or commit message bypasses
classification. The workflow always publishes its checks rather than using path
filters that could leave a required result missing.

The application suite and publication scan run concurrently. Classification
failure blocks `application`; `deployment-contract` requires successful
classification and publication scanning, including on documentation-only changes.
Publication scans the exact PR head and complete reachable history. Application
and integration suites test GitHub's integration candidate. These publication
and test boundaries do not impose an additional review protocol.

For documentation-only runs, verify successful `change-scope`, `publication-scan`
and `secret-scan`, passing lightweight `application` contracts, and the explicit
suite skips. Unknown scope must run the full gates. Record results in the PR;
never describe a cancelled or skipped suite as an executed pass.

## Source size ratchet

`tests/test_source_size_ratchet.py` checks Python modules under `src/` as part
of the application suite. Modules at or below 1,500 lines can grow or shrink freely
and need no entry in `tests/snapshots/source_line_counts.json`. The baseline records
only existing oversized modules: they may shrink but may not grow beyond their
recorded count. Lowering a baseline after a reduction is optional; never raise it
to admit growth. Split a module that crosses 1,500 lines and remove entries for
deleted files. Counts include comments, blank lines and a final unterminated line.

## Diagnose incomplete application runs

The parallel application suite prints test nodes and outcomes with unbuffered
output, short tracebacks, failure/skip summaries and the 25 slowest phases. Worker
crashes fail the run without automatic worker restart. Captured streams stay out
of failure output and JUnit logs; fixtures and assertions must still be synthetic.
Each test, including fixture setup and teardown, has 360 seconds. The
`pytest-timeout` signal handler reports the active test as FAILED (or ERROR for
fixtures), includes the blocking stack and lets pytest finish its report and run
following tests. If Python cannot handle the signal, faulthandler dumps every
thread and exits the worker at 420 seconds; disabled worker restart fails the run.
Both limits exceed the suite's longest in-test subprocess budget (300 seconds).

CI retains `application-results.xml` and bounded `application-workers/*.json`
diagnostics for five days, with the run ID and attempt in the artifact name.
The JSON records worker exits/signals, current test phases, sampled process
metrics, runner limits and cgroup-v2 OOM deltas. Missing or partial measurements
are explicit; SIGKILL alone does not establish OOM. See
[parallel worker diagnostics](DEVELOPMENT.md#parallel-worker-diagnostics).
Hard cancellation can prevent report creation or upload;
use the streamed log for incomplete runs. Missing XML is not evidence of success.
No workspace, database, media or environment dump is uploaded by this step.

## Known-good discovery corpus

For claims about the corpus's exercised application paths, **proof required** means `make corpus-check` passes on
that PR head and the PR links its `Synthetic discovery corpus` CI run. State
whether the evidence is the fast application-contract subset or the full
product-OCR and independent faster-whisper run; a fast pass does not establish
OCR/ASR accuracy. Full runs nightly, on workflow dispatch, and on every PR except
the documented documentation-only exemption. A missing or failed full run remains
an open corpus recognition-quality gap.

Neither lane executes the production transcription service, its worker, WhisperX
audio loading/decoding, VAD, alignment, diarization, or service segment conversion.
Requiring this corpus on service changes is a conservative trigger, not evidence
that those service paths passed. Receipt `coverage` fields and CLI output state
this boundary. Production-service claims require the separate transcription CI
checks and applicable runtime qualification evidence; a passing corpus cannot
substitute for them or for GPU/model acceptance.

The [corpus guide](../benchmarks/corpus/README.md) records file-size/publication
requirements, pinned models, expected outputs, tolerances, historical regression
proofs, and the explicit `--update-expected --proposal` review workflow. Ordinary
runs cannot rewrite expectations. Fixture and expected-output changes need a
reviewed diff and a passing new run. This gate supplements all existing security,
publication, application, PostgreSQL, transcription, deployment and review gates.
