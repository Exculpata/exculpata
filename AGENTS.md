# Exculpata contributor judgment rules

Follow [coding standards](CODING_STANDARDS.md) for development, synthetic data,
publication safeguards and validation. The [review policy](docs/QUALITY_GATES.md#review-policy)
remains the merge gate. Keep work aligned with the [product north star](docs/PRODUCT_NORTH_STAR.md).
Use the [ideal architecture contract](docs/IDEAL.md) to review supported paths,
evidence integrity and the rules that still require human judgment.

## Restate the goal before coding

Read the issue, current comments, assignees, claims and related PRs. Respect
existing work and holds. In your first kickoff comment, restate the goal and
user-visible outcome in your own words. Compare it with the issue's scope and
acceptance criteria; code starts only after they match. Resolve any mismatch
with the issue author before proceeding. Keep changes focused on that outcome.
The issue author's form entry is not the agent's kickoff. Link the agent's first
kickoff comment in the PR. Before marking it ready, the reviewer checks the
comment's author, timestamp before the first implementation commit, and scope
against the issue; an absent or mismatched kickoff blocks readiness.

## Keep public language neutral

Use neutral criminal-defense terminology in code, comments, docs, commits and
PR text. Never disclose private organizational affiliations, employer details,
office names or professional titles. Use synthetic examples only.

## Correct the environment, not the agent

When a mistake happens a second time, change the repo so the next contributor
can't repeat it. Fix it at the highest level that works:

1. Code: types, invariants, one owner per piece of state, boundaries that make the wrong call fail.
2. A static check or CI test whose error names the file or function to use instead.
3. A rule in this file, only for judgment calls that nothing can check.
4. Human review, as a last resort.

Every fix comes with a small synthetic regression test that fails on the commit
that had the mistake. Name that commit in the PR description. If you fix at a
lower level, say in one line why a higher level didn't work.
TestClient route tests (for example tests/test_timeline_routes.py) can hang in a network-less workspace-write sandbox, so agents should run the non-route tests there and leave route tests to CI or the outer runner.

## Encode intent in the implementation

Express intent in names, types, invariants or tests rather than comments.
A comment that explains a workaround needs a linked issue and an expiry
(date or explicit removal condition). Delete stale comments; convert missing
behavioral guarantees into tests or track the remaining work in an issue.

## Rule enforcement

| Rule | Enforced by |
| --- | --- |
| Match the agent's kickoff goal and user-visible outcome to the issue before coding. | The issue form records the author's goal; the PR template requires the agent's kickoff permalink. Before readiness, a reviewer verifies its author, timing and scope. `tests/test_issue_templates.py` checks these prompts, not the later comment; human review verifies the actual evidence. |
| Keep public language neutral and exclude private organizational details. | The installed publication guard checks configured private terms; review checks terminology and public text outside Git because a generic scan cannot identify every private affiliation. `tests/test_issue_templates.py` guards the guidance and this enforcement row. |
| Fix repeated mistakes at the highest effective level and explain lower-level choices. | Required fix-level field; PR review checks the choice because effectiveness depends on the failure. |
| Supply a synthetic regression and name the failing commit. | Required regression field; the application CI suite runs tests; PR review verifies the historical failure evidence. |
| Encode intent in implementation; give workaround comments an issue and expiry. | Code review checks intent, workaround relevance and expiry because syntax alone cannot establish them. |
| Follow the linked coding, publication and validation standards. | `make check-fast`, the installed pre-push publication guard and the linked review policy; review covers public text outside Git. |

Add a row for each new judgment rule; delete its row once the mistake cannot happen.
