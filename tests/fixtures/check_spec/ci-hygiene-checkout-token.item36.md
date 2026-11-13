# Fix: weekly hygiene workflows fail at checkout (empty token)
APPROVED: Owner 9:44 AM ET 11/2 via Grok Bot relay, item 36: "36 yes". Widened: fix the filer step args in deps-audit and stale-report to match the filer; change only .github/workflows/ and tests/.
Status: AWAITING Owner (drafted 6:50 AM ET 11/2 by Exculpata after main CI red alert).

Goal: the two weekly scheduled workflows added in #422 (e274bb4) run green.
- Dependency hygiene audit (.github/workflows/deps-audit.yml, line ~49)
- Stale hygiene report (.github/workflows/stale-report.yml, line ~81)

Symptom: first scheduled runs 11/2 (41439200200, 41438930601) at 8de77b1 fail in actions/checkout with "Input required and not supplied: token". Both set `token: ""` on checkout. Quality gates on main are green.

Files: only those two workflow files (.github/, so merge needs Owner's yes at exact SHA).

Acceptance: `gh workflow run` (workflow_dispatch, or a temporary dispatch trigger if absent) of both workflows on the PR branch passes checkout and completes; persist-credentials stays false; permissions stay minimal; no new secrets. Add a CI or lint check that rejects `token: ""` on actions/checkout so it can't recur.

Route: codex runner, local, medium effort, one attempt, Owner-approved once he says yes.
