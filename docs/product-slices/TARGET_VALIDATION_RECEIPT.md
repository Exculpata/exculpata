# Target validation receipt template

Copy this template outside the checkout. All lanes begin as **not run**. Fill it
only from observed results for the exact candidate. Keep private host names,
addresses, paths, credentials, identities, and raw logs outside public Git.

- Full candidate commit: _not recorded_
- Model/profile and pinned model revision: _not recorded_
- Synthetic fixture/version: _not recorded_
- Validation date: _not recorded_

| Lane | Result | Concise evidence or synthetic failure |
| --- | --- | --- |
| Linux bootstrap, compile, full application/transcription tests | not run | |
| Intended profile preflight and doctor | not run | |
| Separate synthetic installation and first login | not run | |
| Setup handoff, source preparation, readiness | not run | |
| Account creation, roles, reset/disable session revocation | not run | |
| Matter membership and cross-matter denial | not run | |
| Exact/phrase/Boolean/proximity matches, totals, paging, source links | not run | |
| Selected-passage budget and honest coverage | not run | |
| All-extracted-text review, late findings, failed-unit ledger | not run | |
| Durable work interruption, retry/cancellation, source changes | not run | |
| Full-text resource exhaustion, gaps, saved-ledger retention and deletion | not run | |
| Full-text saved work in Reports, source mutation and size refusal | not run | |
| Timeline, people/places/things, focused topic draft compilation | not run | |
| Human edits and source/review basis in Word and Markdown | not run | |
| Real pinned local model offline; relevance and unsupported claims | not run | |
| Encrypted backup and clean restore including new state | not run | |
| Update and documented rollback with synthetic state | not run | |

Use **passed**, **failed**, or **not run**. Add separate rows for each exercised
auth/media/model profile. A passing workflow with a deterministic test client
is not a model-quality or GPU-readiness result. A skipped lane is not a pass.

## Decision

- Unresolved synthetic failures: _not assessed_
- Target-machine acceptance: **not yet given**
- [Review policy](../QUALITY_GATES.md#review-policy): _check before merge_
- Merge/deployment: **not performed by this receipt**

See [the walkthrough](TARGET_VALIDATION.md) for the subsequent acceptance and
merge process. Re-test affected lanes after any candidate change; record the new
full commit and retain the previous receipt rather than silently replacing it.
