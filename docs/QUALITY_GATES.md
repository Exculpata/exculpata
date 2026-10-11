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
on merge groups, schedules and manual dispatch; a skipped PR browser job is not
a browser test pass. Fork PRs use the upstream PR run; forks need not enable Actions.

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

CI retains only `application-results.xml` for five days, with the run ID and attempt
in its artifact name. Hard cancellation can prevent report creation or upload;
use the streamed log for incomplete runs. Missing XML is not evidence of success.
No workspace, database, media or environment dump is uploaded by this step.
