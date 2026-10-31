# Exculpata contributor judgment rules

Follow [coding standards](CODING_STANDARDS.md) for development, synthetic data,
publication safeguards and validation, including the
[synthetic fixture boundary](CODING_STANDARDS.md#synthetic-fixture-writes). The [review policy](docs/QUALITY_GATES.md#review-policy)
remains the merge gate. Keep work aligned with the [product north star](docs/PRODUCT_NORTH_STAR.md).
Use the [ideal architecture contract](docs/IDEAL.md) to review supported paths,
evidence integrity and the rules that still require human judgment.
For hand-written policy checks, follow the
[bounded fail-closed contract](CODING_STANDARDS.md#bounded-fail-closed-checker-contract).

## Restate the goal before coding

Read the issue, current comments, assignees, claims and related PRs. Respect
existing work and holds. Before coding, write a "Kickoff" section in your
task file or PR body with four parts: goal restated in your own words (including
the user-visible outcome), expected files to touch, acceptance check, and issue link.
Compare it with the issue's scope and acceptance criteria; code starts only after they match. Resolve any mismatch
with the issue author before proceeding. Keep changes focused on that outcome.
The issue author's form entry is not the agent's kickoff. When the kickoff is in a task file, it must be in the branch's first commit.
When the kickoff is in the PR body, its timing is taken on trust.
Link the task file in the PR if used. Before marking it ready, the reviewer checks
the timing rule and confirms its goal and user-visible outcome match the issue;
an absent or mismatched kickoff blocks readiness.
See [Agent working practice](https://github.com/Exculpata/exculpata/blob/main/AGENTS.md#agent-working-practice) for the related goal and pass/fail check practice.

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

## Encode intent in the implementation

Express intent in names, types, invariants or tests rather than comments.
A comment that explains a workaround needs a linked issue and an expiry
(date or explicit removal condition). Delete stale comments; convert missing
behavioral guarantees into tests or track the remaining work in an issue.

## Agent working practice

Coding agents state a goal and a pass/fail check before starting. Write a
failing test first where a cheap test exists. Check what else the change could
break before pushing, and prove the change works against the real artifact
before calling it done.
Use the repository/DB interface, not raw cursor reads; `tests/test_db_access_ratchet.py` enforces it.

Keep each PR to about 500 changed lines or less. Split bigger work into ordered
small PRs.

The PR author owns the PR to green: fix review findings, CI flakes and stale PR
body text without waiting to be asked, with one push per round.

After two review rounds, only security, privacy or evidence-integrity findings
block; move other findings to follow-up issues. The linked review policy remains
the merge gate.

Merges, pushes to main and ruleset changes need the maintainer's explicit yes.
Never force-push or use `--no-verify`.

## Rule enforcement

| Rule | Enforced by |
| --- | --- |
| Match the agent's kickoff goal and user-visible outcome to the issue before coding. | The issue form records the author's goal; the agent records a Kickoff section in its task file or PR body. The PR template prompts for the goal, expected files, acceptance check and issue link. Before readiness, a reviewer verifies timing and scope. `tests/test_issue_templates.py` checks these prompts; human review verifies the actual evidence. |
| Keep public language neutral and exclude private organizational details. | The installed publication guard checks configured private terms; review checks terminology and public text outside Git because a generic scan cannot identify every private affiliation. `tests/test_issue_templates.py` guards the guidance and this enforcement row. |
| Fix repeated mistakes at the highest effective level and explain lower-level choices. | Required fix-level field; PR review checks the choice because effectiveness depends on the failure. |
| Supply a synthetic regression and name the failing commit. | Required regression field; the application CI suite runs tests; PR review verifies the historical failure evidence. |
| Encode intent in implementation; give workaround comments an issue and expiry. | Code review checks intent, workaround relevance and expiry because syntax alone cannot establish them. |
| Follow the linked coding, publication and validation standards. | `make check-fast`, the installed pre-push publication guard and the linked review policy; review covers public text outside Git. |
| State a goal and pass/fail check, test first where cheap, assess breakage and verify the real artifact. | PR review checks the goal, regression sequence, affected paths and artifact evidence; document checks cannot establish that the work occurred. |
| Keep PRs to about 500 changed lines or less; split bigger work into ordered small PRs. | PR review checks diff size and the split order because coherent boundaries require judgment. |
| The author owns the PR to green, including findings, flakes and current body text, with one push per round. | PR review checks revision history, CI results and body accuracy; repository tests cannot establish author follow-through. |
| After two review rounds, only security, privacy or evidence-integrity findings block; track other findings in follow-up issues. | Reviewers classify findings and verify follow-up issues under the linked review policy. |
| Obtain the maintainer's explicit yes for merges, pushes to main and ruleset changes; never force-push or use `--no-verify`. | Maintainer review and repository protections govern authority; review checks actions and hook use beyond repository test visibility. |

Add a row for each new judgment rule; delete its row once the mistake cannot happen.
