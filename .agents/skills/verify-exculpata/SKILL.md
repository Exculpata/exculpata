---
name: verify-exculpata
description: Capture paired before and after synthetic HTTP evidence for Exculpata changes using separate base and head checkouts.
---

# Verify Exculpata

Use this skill to compare the synthetic workbench flow across a base revision and
a proposed change. Run the head checkout's `scripts/verify-exculpata.py` for both
phases; the base need not contain the verifier. `--checkout` selects the source,
demo seeder and corpus to run, independently of where the verifier lives.

## Before and after

Use a development environment with the repository's Python dependencies and
extraction tools installed. Choose the base revision required by the task; the
merge base with `origin/main` is a useful default. Prefer clean checkouts so each
recorded revision identifies the tested code. Do not modify either tree while
its verification is running.

From the head checkout, create a disposable base worktree and an evidence file
outside both checkouts. Keep temporary runtime storage outside them as well:

```bash
head_checkout="$(git rev-parse --show-toplevel)"
base_revision="$(git merge-base origin/main HEAD)"
verify_dir="$(mktemp -d)"
base_checkout="$verify_dir/base"
evidence="$verify_dir/evidence.json"
verify_python="$head_checkout/.venv/bin/python"
verify_script="$head_checkout/scripts/verify-exculpata.py"
mkdir "$verify_dir/tmp"
git worktree add --detach "$base_checkout" "$base_revision"

TMPDIR="$verify_dir/tmp" "$verify_python" "$verify_script" \
  --phase before --checkout "$base_checkout" --evidence "$evidence"
TMPDIR="$verify_dir/tmp" "$verify_python" "$verify_script" \
  --phase after --checkout "$head_checkout" --evidence "$evidence"
```

Run before first and inspect its result before running after. A failed baseline
can be useful evidence; keep its failure explicit. A missing or incompatible base
dependency is a limitation to report, not permission to substitute head code.
Use `--timeout SECONDS` if the synthetic processing deadline needs adjustment.
Existing phases cannot be overwritten; use a fresh evidence file for a new pair.

After must have a different revision from the recorded before phase. Use
`--allow-same-revision` only for an intentional same-revision smoke check; the
flag is recorded in the phase evidence. Describe that result as a smoke check,
not evidence of a change between revisions. A revisionless baseline must be rerun.

## Interpret the evidence

Inspect both phase records: revision, dirty state, override flag, overall result,
HTTP statuses, selected fields, timings and any partial checks or error type.
The verifier seeds fresh disposable storage with the Harbor Street synthetic
corpus, serves on an ephemeral loopback port, loads the case, checks processing
coverage, searches for the synthetic name and opens matching extracted text.
It removes runtime storage after shutdown. The JSON keeps selected observations,
not response bodies, sessions or tokens. Retain the evidence outside the repo
and summarize the observed changes and limitations in the review record.

Passing evidence shows those HTTP checks succeeded for that synthetic corpus and
the recorded trees in the local environment. It does not establish browser
rendering or interaction, production authentication, model quality, performance
at scale, exhaustive retrieval, or behavior on real case material. Dirty state
does not capture a patch or attest an immutable source snapshot. Different commit
IDs alone do not prove that an application behavior changed. These checks do not
replace the applicable test, publication and review gates.

## Cleanup

Inspect and retain the evidence in the task's external artifact location first.
Then remove only the disposable base worktree created above:

```bash
git worktree remove "$base_checkout"
rmdir "$verify_dir/tmp"
```

If removal reports modifications, inspect them before deciding how to proceed;
do not force removal of unknown work. Delete the retained JSON and its now-empty
temporary parent only when the task's evidence retention needs are satisfied.
