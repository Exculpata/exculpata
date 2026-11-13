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
