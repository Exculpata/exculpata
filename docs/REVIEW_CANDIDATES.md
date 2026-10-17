# Candidates for review

Passages that may be worth attorney review for exculpatory or impeachment value. The tool does not decide whether any material is exculpatory, material, or subject to disclosure.

Open **Review map → Candidates for review**. Each candidate for review shows an
original passage, a highlighted trigger, source and locator, and a neutral reason.
The source link opens cited context. Read that context before making a decision.
The notice remains above the filters on desktop and mobile. Category chips scroll
inside their row; review buttons remain large on a narrow screen.

| Category | Meaning |
| --- | --- |
| `alternate_person` | Mentions another person possibly connected to the event |
| `witness_benefit` | Mentions a benefit, payment, deal, leniency or relocation for a witness |
| `prior_statement_difference` | Differs from another statement by the same person |
| `credibility_context` | Mentions information bearing on a witness's reliability (e.g., impairment, vision, distance, prior reports) |
| `identification_uncertainty` | Expresses uncertainty in an identification or observation |
| `investigation_gap` | Mentions a lead, test or record that was requested, not done or not located |
| `favorable_physical_evidence` | Mentions physical or forensic results that don't match the stated account |

These are review categories, not assessments of truth, legal significance,
importance, disclosure obligations, or outcomes. There is no score or importance
ranking. Categories follow the table order, then source order. A request for a test
can appear even when the same passage records its completion. Denials and hedges
remain in the excerpt: “no deal was offered” receives the reason “Mentions whether
a benefit was offered.” The tool does not infer that a benefit occurred.
Physical-result triggers with a negated mismatch are omitted, including “could
not be excluded” and “not incompatible with”; those phrases do not describe a
result differing from the comparison.
Earlier negation in the same clause also causes abstention regardless of
intervening modifiers. This conservative rule can omit ambiguous passages;
an explicit contrasting clause or semicolon starts a new comparison context.
Requests are matched to a lead, test, record or related investigative object;
an interview's mention of a request for water is not a candidate for review.

**Prior statement differences are disabled** pending #271 / PR #283's merge and
review. The typed adapter accepts only attributed same-person comparisons with
both citations. Cross-person comparisons do not enter this category. This PR
reuses #283's unchanged `review_wording.py` and `CandidateReviewRepository`; it
does not create a competing shared module or migration. The dependency's corrected
speaker/attribution signatures must be consumed before activation.

Dismiss hides a candidate from the default view. Mark reviewed records a review
decision; Reopen returns it to open. Status filters show these decisions. The saved
actor, time, and revision survive restart and backup. Concurrent decisions require
a reload. Decisions belong to the matter and `review_candidates` namespace, so
comparison-workflow decisions do not cross over. Source versions, extraction
digests, excerpt offsets, category and rule definitions bind each signature;
changed meaning requires a new review.

JSON, CSV and Markdown exports use `exculpata-review-candidates-v1`. Each begins
with the same notice (JSON's first field; CSV's first record; Markdown's first
paragraph). CSV has a metadata record before its header. Every candidate row
includes its citation, original passage and review state. CSV text cells guard
against spreadsheet formulas, and Markdown escapes source markup. Quoted original
records are evidence content and remain verbatim; the wording check governs the
tool's own labels, reasons, templates and documentation.

Rules live in `src/case_intelligence/data/review_candidate_rules.json`. The default
workflow needs no model and works with `UnavailableGenerator`. Integrators can
pass a `GroundedGenerationService` to `ReviewCandidateService.payload` as
`generation=` for an optional annotation of at most eight existing candidates.
Only a cited original-text reason passing the existing verifier and neutral
wording check may be added. The deterministic reason, category and signature stay
intact. Generation runs outside database locks and a fresh authorized snapshot
removes annotations whose source identity changed. The HTTP page does not invoke
a model automatically.

The scan covers at most 2,500 extracted units and 6,000 characters per unit and
returns at most 150 candidates for review. A visible limit notice identifies
truncation; filters apply to that bounded result, not the entire production.
Source text is streamed with limits of 128 Ki characters per serialized record,
32 Mi serialized characters per scan and ten seconds of cooperative reading.
Reaching a reader limit stops the scan and displays the same limit notice; it
does not load the remainder of a large source or start another source.
Missing or changed citations are omitted and counted. Extraction errors, unusual
phrasing and unprocessed sources can cause omissions. No candidates is not a
statement that a production contains nothing worth review.

Synthetic fixtures exercise at least three positive and three similar negative
examples per category. The active-category floors are precision 1.0 and recall
1.0 on this small suite, with zero allowed negative hits; these are fixture
requirements, not estimates for case material. The disabled category has separate
typed-adapter fixtures and an off-by-default check. Harbor-street coverage uses
existing records; no source counts change. Provisional evaluation questions live
under `tests/fixtures/synthetic/workflow_eval/` pending #267.

Review state uses the dependency's SQLite authoritative control table and
PostgreSQL equivalent migration. The full matter archive includes the table;
freeze and purge protections apply. Run
`.venv/bin/python scripts/review-candidate-storage-restore-drill.py` for the
stopped-runtime backup and clean-target restore regression, and
`tests/test_candidate_review_postgres.py` for the shared PostgreSQL schema.
