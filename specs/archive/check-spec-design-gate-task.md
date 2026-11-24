# Spec preflight and design gate

## Kickoff

- Goal: land the reviewed spec preflight checker and design-note checks so
  contributors can see scope, import-boundary and design evidence gaps without
  executing Python from the checkout being inspected. Repair the rule reader
  at the implementation boundary, preserving explicit UNCHECKED results.
- Expected files: `tools/check_spec.py`, `tests/test_check_spec.py`, the five
  synthetic Markdown fixtures under `tests/fixtures/check_spec/`, this task
  record, and the shared checker contract test required by coding standards.
- Acceptance check: reject unsupported rule syntax and side-effecting function
  headers without running them; compare all mapped import pairs with the real
  rule in a temporary copy; exercise design-note CLI outcomes and bounded
  selftest subprocess failures; pass focused tests, lint and publication scans.
  Full CI parity belongs to the launcher after this commit.
- Issue link: no GitHub issue was supplied. The direct approved task is retro
  item 3, with the scope and acceptance criteria restated in the launch request.
  This kickoff matches that request; no merge or push is part of this task.

## Design and regression boundary

Regression baseline: `1625376937d8`. Its rule loader executes an extracted
function, including decorators and defaults. Replace that boundary with a
validated, bounded AST interpreter. Validate the whole rule before applying it,
including unreachable branches, and report unsupported syntax as UNCHECKED.
Support the rule's simple layer-binding preamble and inert assertion diagnostic
alongside boolean tests, literal membership, equality and string prefixes.
Unsupported forms have no executable fallback.

The principal compatibility risk is changing import-boundary decisions. The
existing synthetic and real-repository cases plus every mapped source/target
pair cover that risk. Temporary synthetic repositories cover marker side effects
and design fences. Timeout probes cover both selftest setup subprocesses.
Deferred review findings remain outside this change. The application and its
evidence handling are unchanged; this contributor tool supports honest reporting
of what it can and cannot check, consistent with VISION.md.

## Validation record

- Before the repair, the marker-side-effect, unsupported-rule and selftest
  timeout regressions failed on the imported implementation. The marker was
  actually created by the baseline decorator.
- The static rule matches the trusted reference imported from a temporary copy
  for all 45,796 pairs in the 214-module layer snapshot. All five recorded spec
  fixtures produce byte-for-byte identical output and exit status to the
  baseline tool on current main.
- The original real-repository fence test also fails on the baseline because
  current main already maps the previously new module. Its expectation now
  follows the actual snapshot; the synthetic missing-entry regression remains.
- The checker and shared fail-closed tests pass with real-repository cases
  enabled: 41 passed. The design selftest reports 20 passed, 0 failed.
  Fixture policy and inventory validation reports 2,017 passed. Ruff passes.
- Local tests use the shared interpreter with this checkout's `src` on
  `PYTHONPATH`. The direct pytest CLI invocation exposes a pre-existing
  rename-checker test that parses pytest arguments. The same selection is run
  through `pytest.main` with clean process arguments; no gate code is changed.
  The final run, including rename and documentation checks plus all real-repo
  cases, reports 899 passed, 0 failed.
- Full launcher parity is pending the launcher's post-commit run. The referenced
  review log files were unavailable locally; scope follows the launch request's
  restated findings. No deferred finding, registry, exemption or inventory pin
  was changed.
