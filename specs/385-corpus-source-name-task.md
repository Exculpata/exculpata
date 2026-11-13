# Corpus citation source name task

## Kickoff

- Goal: make the corpus observer reject missing, empty or incorrect persisted citation source names so the gate catches saved answers that would display an unlabeled or wrong source. Preserve the observation shape and manifest aliases.
- Expected files: `scripts/corpus_product.py` (only the `_citation` validity predicate), `tests/test_corpus_product.py` (existing corruption regression), and this task record. Keep local validation evidence in uncommitted `last.md`.
- Acceptance check: first demonstrate source-name deletion and replacement regressions on the unchanged observer; then require missing, empty and wrong names to fail all five answer cases with the existing invalid source span diagnostic while nine non-answer cases pass. Run the six requested corpus test modules, Ruff and network-contract. Confirm normal document and transcript names come from the resolved document display name for search and answers.
- Issue: https://github.com/Exculpata/exculpata/issues/385, only the source-name advisory from https://github.com/Exculpata/exculpata/pull/315#discussion_r4325076924. Leave the issue open for its other items.

The supplied scope and acceptance criteria match this kickoff. Network access is prohibited for this task, so live comments, assignees, claims and related PR status cannot be refreshed; the supplied issue and review context are the available authority.

## Regression scope

Regression baseline: `9eb35b9970ad768f6e5c5889536b7bb2f99e8360`. Missing and empty names incorrectly pass the corpus gate before the fix. Wrong nonempty names already fail through the shared resolver and remain a control. Product finalization rejects empty and wrong names before saving, so those test cases corrupt the saved answer returned by real `ask`; the missing-name case uses the existing saved citation payload patch. No validator is bypassed. Local red/green details remain in `last.md`.

## Validation

All six requested corpus modules pass (494 tests), with `PYTHONPATH=src` so the shared interpreter imports this worktree. Ruff, network-contract and the installed private-terms scan pass. No product, workflow, corpus, configuration or other test files changed. Full CI parity remains with the launcher.
