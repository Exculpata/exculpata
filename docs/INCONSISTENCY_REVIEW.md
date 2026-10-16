# Possible inconsistencies for review

Open **Review map → Possible inconsistencies** to compare two original passages.
Filter by witness, difference type or review status. Dismissed candidates are hidden
initially. Mark reviewed, dismiss or reopen a candidate; these are review decisions,
not conclusions about a witness. A stale decision form returns a conflict and asks
you to reload before saving. Each export includes the actor, decision time and state.
Exports include all review states by default and retain witness/type filters.

The deterministic comparison works without a model. It looks for aligned statements
with explicit speakers or interview subject metadata and compares clock/ISO dates,
named locations, counts, presence/action polarity and object colors/conditions.
Original wording, including uncertainty, is retained. Object description differences
can refer to different objects; the note explicitly asks reviewers to check identity.
Ambiguous curated aliases and unattributed statements abstain. It does not score
credibility, resolve event identity or decide which account is correct.

The scan considers at most 2,500 extracted units, 6,000 characters per unit, 1,200
statements, 30,000 statement pairs and 150 candidates. A limit notice identifies
bounded scans. Record dates are labels, not event dates. No result is evidence that
accounts agree; OCR, attribution, parsing and alignment can miss differences.
Each displayed/exported pair must retain two exact validated original windows.
Missing, changed or out-of-window citations are dropped and counted.

Review state lives in the control SQLite database, including with PostgreSQL search.
It is keyed by matter, workflow namespace and source-version-sensitive signature.
Changed sources cannot inherit an old decision. The equivalent PostgreSQL migration
provides a portable schema; the application does not dual-write review decisions.
Complete stopped-runtime backups retain the control database, session state, source
registries and originals. Run `python scripts/inconsistency-storage-restore-drill.py`
for the synthetic clean-target recovery check. A PostgreSQL projection rebuild must
never replace the authoritative control database.

Validation commands:

```console
.venv/bin/python -m pytest -q tests/test_statement_comparison.py tests/test_inconsistency_wording.py tests/test_inconsistency_workflow.py
.venv/bin/python scripts/inconsistency-storage-restore-drill.py
.venv/bin/python scripts/browser-accept-inconsistencies.py --chrome-binary /path/to/chrome --chromedriver /path/to/chromedriver --output /tmp/synthetic-inconsistency-browser
```

The synthetic scorecard requires seeded recall ≥ 0.8 and at most one candidate on
the consistent set. This small regression set does not establish broader detection
quality. `tests/fixtures/synthetic/workflow_eval/inconsistencies.json` carries six
questions and expected source pairs pending the #267 harness.

For #272, `review_wording.py` exports the shared prohibited-phrase checker and
`candidate_review.py` supplies namespace-scoped decisions inside the existing
entity authorization transaction. Callers must validate their current candidates
under the source guard before applying or saving decisions.
