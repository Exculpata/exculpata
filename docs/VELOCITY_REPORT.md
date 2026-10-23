# Weekly velocity report

Run from the checkout with Python 3.12+ and an authenticated GitHub CLI:

```console
python scripts/velocity-report.py --since 2026-10-01
python scripts/velocity-report.py --since 2026-10-01T00:00:00Z --json
```

The repository is fixed to `Exculpata/exculpata`. The script only reads REST
endpoints and a GraphQL query through `gh api`; it does not change GitHub or
handle credentials. Use your existing CLI authentication with repository and
Actions read access. No third-party Python packages are needed.

Weeks start Monday at 00:00 UTC. `--since` is inclusive and defaults to the Monday
of the previous full week at midnight UTC; collection time is exclusive. For
example, a run on Friday 2026-10-23 defaults to 2026-10-12, covering the previous
full week and the current week to date.

Each JSON week includes `window_start` (inclusive) and `window_end` (exclusive)
as UTC ISO timestamps, plus the boolean `partial`. A week is partial when its
window is shorter than Monday to the next Monday or ends at the report's current
time cutoff. Text headers show the same bounds and `partial` or `full`, for
example `Week 2026-10-12 (partial: 2026-10-16T00:00:00+00:00 to
2026-10-19T00:00:00+00:00)`. The Monday label identifies the calendar week;
metrics cover only the displayed window. Empty samples print `n/a` (`null` in
JSON), never a fabricated zero.

- **Open-to-merge:** median elapsed hours for PRs merged in the week.
- **Commits tested per PR:** distinct Actions head SHAs matching the PR's commit
  list, with a started run before merge, for each PR merged that week. Retries and
  multiple workflows on one SHA count once. Matching uses `pulls/N/commits`, since
  merged PRs' runs can have empty `pull_requests`. This measures observed tested
  revisions, rather than successful tests; force-pushed-away and integration SHAs
  absent from the PR's current commit list cannot contribute.
- **Review comments per PR:** current inline review-comment count for each PR
  merged that week, including replies. Conversation comments and review summaries
  are excluded; deleted comments and historical counts cannot be reconstructed.
- **Green-to-merge-queue gap:** median minutes from observed green to each enqueue
  that week, with a sample count. This is an **Actions-only proxy**, not historical
  branch-protection attestation: choose the most recently tested SHA before enqueue,
  then require every observed workflow's latest run on it to have succeeded before
  enqueue. Missing, failed or unfinished observations produce no gap sample.
  External status checks, workflows not yet started and historical required-check
  configuration are unavailable. Re-enqueues are separate samples.
- **CI duration:** median and nearest-rank p90 minutes for completed repository
  Actions runs created that week, both overall and split by workflow name in
  `ci_duration_minutes_by_workflow`, regardless of event or conclusion. Duration is
  `updated_at - run_started_at`, an API approximation that excludes initial queue
  wait and may include later metadata updates. Only the latest retained attempt
  per run is available; unfinished runs are excluded.
- **Merge queue drops:** removal timeline events that week, excluding events whose
  GraphQL `reason` is `merged`. Repeated removals and PRs still open or closed
  without merging count; a later merge does not erase earlier drops. No failure
  cause is inferred for other reasons, including null reasons.

PRs are discovered by last update, including all states. REST and timeline
connections are paginated. Actions history starts at the earliest creation date
of the selected PRs to include pre-window testing. Deleted/expired runs, older
attempts and runs without a matching commit cannot contribute to per-PR metrics;
zero means no retained associated run was observed. This is a current API snapshot,
not an immutable historical audit. Large windows or old active PRs may be expensive.
Actions queries use bounded `created=A..B` ranges. Ranges above GitHub's 1,000-run
cap split recursively into disjoint ranges at one-second precision. More than
1,000 runs within one second still fails explicitly. The PR commits endpoint has
a 250-commit maximum; larger PRs or incomplete commit lists also fail explicitly.
API failures return nonzero with no partial report and include the exception
message with GitHub token values redacted; raw CLI stderr is never printed.
Offline tests replace one `gh_json` boundary with synthetic JSON; no live access
is needed for tests.

API definitions: [workflow runs](https://docs.github.com/en/rest/actions/workflow-runs),
[PR commits](https://docs.github.com/en/rest/pulls/pulls#list-commits-on-a-pull-request)
and [queue timeline events](https://docs.github.com/en/graphql/reference/pulls#removedfrommergequeueevent).
