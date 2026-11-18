# Demo destination checks with a blocked test-client probe

## Kickoff

- Goal: Keep the checkout, symlink and nonempty demo destination checks runnable for contributors whose local pytest socket probe fails. A rejected destination must still leave existing files and the environment intact, and a usable destination must still receive the real client probe.
- Expected files: `scripts/demo.py`, `tests/test_demo.py` and `tests/demo_probe.py`.
- Acceptance check: Reproduce the three existing tests being skipped under an injected probe failure first. Then show they run locally and in CI with the same failure injected, while the valid-destination test skips locally and fails in CI. Exercise `seed_demo` to verify it rejects unsafe destinations before entering the demo environment or constructing the app or client. Run the sandbox scanner regressions and compare detected tests with actual client use in an application-suite audit.
- Issue: <https://github.com/Exculpata/exculpata/issues/354>

## Problem and approach

The test collector follows the dynamically loaded `seed_demo` into the function that validates the destination and constructs the app and client. A failing probe prevents the three rejection tests from executing even though their paths stop before the client is needed.

Give the filesystem checks a separate function that the three rejection tests can call directly. Call the same function at the beginning of demo setup and test the context entry ordering with environment, app and client sentinels. The scanner continues to follow the complete demo setup path for tests that exercise context entry.

The regression uses pytest's normal collection and setup hooks with a synthetic probe result. The injected failure tests both local and CI behavior without requiring a socket-restricted machine. The setup order regression protects deferred validation, and the existing full demo test protects the successful setup path.
