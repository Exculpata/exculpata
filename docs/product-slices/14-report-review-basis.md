# 14: Preserve review scope, gaps, and decisions in Reports

Status: implemented on `main` as
[#38](https://github.com/Exculpata/exculpata/pull/38). See
[report review basis](../REPORT_REVIEW_BASIS.md). No dependencies.
Deeper hierarchical synthesis remains 13.

## Finding and outcome

`research_to_report` copies the summary and evidence citations into one section
named "Verified research synthesis". It does not preserve the saved search-pass,
gap, and coverage records as report sections. `full_review_to_report` primarily
copies criterion/counts and up to 100 citations from machine-included decisions.
The report does not communicate the full reasoning and human-review context.

## Small implementation

Convert a saved investigation into separate editable sections for question,
findings, conflicting/unresolved results, scope and coverage, and method/stop
reason. Preserve the exact source run identifier and version basis. Label an
abstained/unsupported result accurately instead of giving it an unconditional
verified-synthesis heading.

For every-source reports, distinguish machine decisions, human validation,
unreviewed work, and disagreements. State any citation-detail cap and link to
the complete decision ledger; do not imply a capped appendix contains all
support. Preserve the existing manual editing and conflict guards.

## Code and acceptance

Start with `workbench.py::research_to_report`, `full_review_to_report`,
`workspace_store.py` report sections, `work_product_exports.py`,
`docs/REPORT_EDITING.md`, and `docs/REPORT_EXPORTS.md`.

Test a successful run with gaps, an abstention, a partial source population,
human/machine disagreement, and over 100 citation entries. Word, Markdown,
individual exports, and complete bundles must preserve the same explicit basis.
Use existing section storage if possible; no new synthesis model is needed.
Existing Reports must not be silently rewritten. Browser acceptance checks
conversion, manual edit, download, and stale-source repair behavior.
