# Local workflow parity task

## Kickoff

- Goal: replace the external handwritten CI copy with a versioned, offline planner
  and serial runner derived from pull-request workflow YAML. Contributors must see
  actual conditions, classification outputs, isolated cells and every unavailable
  check; unsupported workflow changes must fail before execution and in hosted tests.
- Expected files: `scripts/ci_parity.py`, optional `scripts/ci_parity_*.py`,
  `scripts/ci-parity-expected-skips.toml`, `tests/test_ci_parity.py`,
  `tests/snapshots/ci_parity_plan.json`, a short Local parity section in
  `docs/QUALITY_GATES.md`, and this required task/evidence file. Only the terminal
  width test may additionally change if acceptance 15 proves an isolation defect.
- Acceptance check: all 17 criteria in the supplied work item, focused offline
  regressions, deliberate F2/F4/F9 broken variants, both scenario plans, terminal
  crash investigation, ruff, compileall, fixture checks and unchanged-allowlist
  network contract. Full application shards belong to the launcher after commit.
- Issue link: not supplied. The authoritative task is the supplied
  `ci-parity-from-workflows` work item and pasted design. No network access is
  authorized, so live issue comments, assignments, claims and PRs cannot be checked.
  The checkout starts at b47fde3; the current branch was clean. No conflicting
  local work or hold was found. The supplied scope and acceptance match this goal.

## Design and risks

The supplied design is the implementation contract: strict parsed expressions,
closed YAML/action mappings, exact visible capability omissions, real workflow
commands and one detached checkout/temp per matrix cell. No dependency, workflow,
action, classifier, shard selector, worker diagnostic, fixture inventory or egress
allowlist changes. Existing application test discovery brings the drift test into
GitHub's non-documentation PR suite without changing workflows. Unknown forms fail
closed. This follows VISION's local-first and honest-evidence requirements; NOT RUN
entries never establish acceptance. Risks include GitHub coercion/status semantics,
output propagation, shell/environment differences, timeout cleanup and stale skips.

## Implementation and regression evidence

The implementation uses a tokenized expression parser and closed workflow/job/step
key tables. It discovers workflows and axes, orders dependencies, executes the
original run commands, and propagates step file commands and real job results.
Unsupported syntax is diagnosed before execution. Exact unavailable steps are data;
capability detection is a finite review aid for trusted repository commands, not a
shell sandbox. The golden snapshot includes normalized commands, conditions,
environment, outputs and matrix definitions; action refs are intentionally omitted
so SHA-only updates are free. New steps/jobs/workflows therefore change the reviewed
snapshot even when their syntax is already supported. The existing shard selector
includes the new drift test exactly once without workflow edits.

The report streams to disk and stdout, records completed outputs in its header,
retains full step logs, prints every expected omission, and ends with SUMMARY.
Actual and hypothetical scenario outputs are distinguished. Matrix cells execute
serially with separate detached checkouts and temp/file-command paths. Matrix job
outputs follow that local completion order; hosted ordering remains a stated gap.
Timeouts terminate the step process group and finally removes the owned worktree.
The sole command substitution is the external venv path. All environment changes
live in one module; inherited pytest options and database DSN are removed, explicit
workflow env is honored, and workflow worker counts are capped at four.

Regression baseline: b47fde3. The first test-first run on the kickoff-only revision
(db82627) failed collection because the planner/parser modules did not exist.
The previous parity implementation was external and unversioned, so there is no
repository commit containing its F2/F4 logic to cite. Deliberate broken variants of
the new implementation provide behavioral negative controls instead:

| Failure | Deliberate fault | Real regression outcome | Restored outcome |
| --- | --- | --- | --- |
| F2 | Return true for every condition | `test_real_classifier_controls_runner_docs_steps` failed: the forbidden application command executed after the real classifier returned docs-only | Passed |
| F4 | Reuse one temp directory across cells, with `mkdir(exist_ok=True)` for that directory | `test_f4_cells_have_fresh_checkout_temp_and_file_commands` failed on the second cell's exclusive worker-directory creation | Passed |
| F9 | Disable unknown-key rejection | `test_f9_unknown_key_prevents_whole_plan` failed because container no longer raised UNMAPPED | Passed |

Each mutation ran separately, exited 1, and was restored byte-for-byte in a finally
block. The three restored controls passed together. No fault variant was committed.
The fix level is implementation: a parser, closed tables, and per-cell resource
ownership. Tests protect those boundaries rather than add skip exceptions.

## Terminal-width investigation (acceptance 15)

On the unchanged baseline terminal test, the isolated node under Python 3.12.15,
pytest 9.1.1 and xdist 3.8.0 reproduced `INTERNALERROR: Failed: wrong fd` with
`-n 4 --dist loadgroup -v -m "not postgres"`. The whole terminal file then reproduced
it after 88 passes. These runs used the workflow storage/test switches, native
thread limits, CI=true, the capped worker setting, and the repo/service source paths;
inherited pytest options and the database DSN were removed. No sandbox was active.

The traceback proves the test replaced the process-wide `os.get_terminal_size`.
Pytest's worker terminal reporter calls `shutil.get_terminal_size` before fixture
teardown, which calls that replacement with real stdout rather than the test's
synthetic descriptor. The test sets COLUMNS but not LINES; Python's shutil queries
the descriptor when either dimension is missing. Setting LINES=24 made the unchanged
isolated test pass, proving how ambient environment can hide the bug. The checked-in
workflow does not set LINES. Live hosted-runner inherited environment was unavailable
under the no-network constraint; no claim is made about its actual LINES value.

The minimal fix scopes only this replacement with `monkeypatch.context()` so it is
restored before pytest reports the test. The descriptor assertion remains strict.
No environment workaround, skip, allowlist, application code or diagnostic plugin
change was needed. With LINES absent, all 103 terminal tests passed under the same
flags, with none of the earlier sandbox ownership failures. This is a test-isolation
fix at the source of the shared mutation, not a product behavior change.

## Expected omission review

All 34 entries below were checked against the current workflow command or action.
The former plumbing and smoke-selection skips are removed: they execute real local
results and local Git diff logic. The browser failure-contract tests also run:
their 276 offline tests passed (354 combined with the then-current 78 parity tests).
Only actual browser journeys remain unavailable. Apt composites and hosted artifact
uploads are now explicitly enumerated instead of silently disappearing.

The table is also the per-entry evidence for a future PR description; it grants no
passing result for any omitted step. None of the named unavailable capabilities was
installed or downloaded during this task.

| Workflow/job/step | Category | Evidence |
| --- | --- | --- |
| corpus:fast:python -m pip install --disable-pip-version-check -e '.[dev]' | install | This unnamed step installs the editable dev extras; its complete command is its exact step name. |
| corpus:full:Install corpus apt dependencies | install | The local composite restores hosted apt cache and runs package acquisition on a miss; it cannot execute with no network or installs. |
| corpus:full:Install independent CPU recognizer dependencies (not production service runtime) | install | The step installs pinned independent recognition packages. |
| corpus:full:Stage and verify pinned OCR data | download | The step downloads pinned language data and installs it with sudo. |
| corpus:full:Stage pinned ASR snapshot for independent recognition | download | --stage-models acquires the pinned ASR snapshot over the network. |
| corpus:full:Product OCR and independent faster-whisper corpus (production service not exercised) | binary-missing | Full recognition requires the pinned OCR data, independent ASR runtime and model snapshot from unavailable setup. |
| network-smoke:network-smoke:Install locked smoke harness dependencies | install | The step installs the hash-locked smoke harness dependencies. |
| network-smoke:network-smoke:Exercise isolated synthetic OIDC and direct-egress denials | docker | The live isolation acceptance builds and runs Docker containers and exercises their networks. |
| network-smoke:network-smoke:Retain content-free smoke summary | github-only | The step uploads retained artifacts to the hosted workflow service; local per-step logs remain beside the report. |
| quality-gates:publication-scan:Install publication-scan apt dependencies | install | The local composite restores hosted apt cache and runs package acquisition on a miss; it cannot execute with no network or installs. |
| quality-gates:publication-scan:Install publication inspection dependencies | install | The workflow invokes pip install; the existing venv is checked without installing. |
| quality-gates:secret-scan:Install checksum-verified secret scanner | download | The workflow downloads a pinned release archive; no network acquisition is permitted. |
| quality-gates:secret-scan:Scan fetched history without publishing matched values | binary-missing | The workflow requires /tmp/gitleaks from its download step; that binary is not provisioned. |
| quality-gates:secret-scan:Verify exact historical exception and synthetic detection controls | binary-missing | GITLEAKS points to /tmp/gitleaks from the unavailable download step. |
| quality-gates:application-shards:Install lightweight documentation test dependencies | install | The workflow installs hash-pinned packages; preflight checks the existing environment instead. |
| quality-gates:application-shards:Install document and media test dependencies | install | The local composite restores hosted apt cache and runs package acquisition on a miss; it cannot execute with no network or installs. |
| quality-gates:application-shards:Bootstrap contributor environment | install | make bootstrap creates and installs an environment and hooks; bootstrap is not exercised offline. |
| quality-gates:application-shards:Verify repeatable setup | install | The scheduled repeatability step invokes make bootstrap; it remains unavailable if its condition changes. |
| quality-gates:application-shards:Retain application test report | github-only | The step uploads retained artifacts to the hosted workflow service; local per-step logs remain beside the report. |
| quality-gates:postgres-integration:Install database test dependencies | install | The workflow installs editable application and PostgreSQL extras. |
| quality-gates:postgres-integration:Run existing PostgreSQL and pgvector acceptance | service | This acceptance requires the declared PostgreSQL/pgvector service and docker exec client. |
| quality-gates:defense-eval:Write lexical stub and product retrieval scorecards plus main delta | install | --locked-environments provisions dependencies; the workflow also declares a PostgreSQL service. |
| quality-gates:defense-eval:Retain scorecards, comparison and provenance even on failure | github-only | The step uploads retained artifacts to the hosted workflow service; local per-step logs remain beside the report. |
| quality-gates:transcription:Install media test dependencies | install | The local composite restores hosted apt cache and runs package acquisition on a miss; it cannot execute with no network or installs. |
| quality-gates:transcription:Install transcription test environment | install | The workflow installs transcription dev extras; local checks reuse the existing dependency superset. |
| quality-gates:deployment-contract:Validate standard and Kerberos Compose graphs | docker | compose-check invokes Docker Compose; Docker execution is outside local parity capabilities. |
| quality-gates:deployment-contract:Verify real gateway strips forged authentication headers | docker | The gateway acceptance invokes the real nginx container, not a synthetic replacement. |
| quality-gates:deployment-contract:Install deployment-contract apt dependencies | install | The local composite restores hosted apt cache and runs package acquisition on a miss; it cannot execute with no network or installs. |
| quality-gates:deployment-contract:Install encrypted restore drill dependencies | install | The step installs dependencies and pulls/tags the pgvector image. |
| quality-gates:deployment-contract:Run both encrypted restore naming generations without skips | service | The encrypted restore acceptance requires restic and the PostgreSQL container; skips are forbidden by its receipt check. |
| quality-gates:synthetic-browser:Install synthetic-browser apt dependencies | install | The local composite restores hosted apt cache and runs package acquisition on a miss; it cannot execute with no network or installs. |
| quality-gates:synthetic-browser:Install browser and media runtimes and contributor dependencies | install | The workflow installs application dev dependencies after checking media tools. |
| quality-gates:synthetic-browser:Run pinned synthetic intake and Report journeys | browser | The journey runner requires the hosted browser runtime and synthetic browser environment. |
| quality-gates:synthetic-browser:Retain bounded synthetic diagnostics | github-only | The step uploads retained artifacts to the hosted workflow service; local per-step logs remain beside the report. |

## Validation and handoff

- Requested focused command: 2,399 passed in 122 seconds. After the final dependency
  bootstrap regression and nested matrix-axis coverage, parity plus fixture checks
  passed again: 2,038 tests (99 parity tests and 1,939 fixture tests).
- The missing-PyYAML bootstrap test first reproduced an uncaught import failure.
  YAML loading now follows stdlib-only dependency inspection, and parser dependency
  failures produce exit 2 and the required final SUMMARY. Unknown later dependency
  constraints fail closed even when an earlier constraint is unsatisfied.
- Full-tree Ruff, compileall and `make network-contract` passed. The egress allowlist
  and all other forbidden inputs remain byte-for-byte unchanged relative to b47fde3.
- Publication tree scan: CLEAN. Its first run flagged only generated Ruff cache
  files containing machine paths. Those disposable files were removed and Ruff was
  rerun with caching disabled; no publication exemption was added. Full outgoing
  history/publication acceptance remains part of the launcher's workflow run.
- Both real CLI `--plan --scenario` previews passed preflight. The scenario values
  were extracted from the golden tests' actual command outputs, not handwritten
  classification logic. Docs skips both full application suite steps, PostgreSQL,
  transcription, deployment and full recognition; classification checks and the
  application aggregate still run. Code includes both application shards and the
  downstream non-browser jobs. The third golden scenario proves browser selection.
- F2/F4/F9 deliberate faults each failed their real regression, and restored controls
  passed. Terminal acceptance passed all 103 tests without the former sandbox.
- No full application shards were run manually. The launcher owns those checks
  after the implementation commit, including publication, corpus and transcription.
  Unavailable checks remain NOT RUN; focused validation is not hosted-CI acceptance.
- No network, installs, sudo, pushes, merges, force pushes, bypassed hooks, launcher
  edits, other-branch edits, or pytest-lock acquisition were performed.
