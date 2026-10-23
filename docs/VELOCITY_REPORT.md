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

Weeks start Monday at 00:00 UTC. `--since` is inclusive (default: seven days ago
at midnight UTC); collection time is exclusive. First and last weeks may be
partial. Empty samples print `n/a` (`null` in JSON), never a fabricated zero.

- **Open-to-merge:** median elapsed hours for PRs merged in the week.
- **Commits tested per PR:** distinct Actions head SHAs with a started run before
  merge, for each PR merged that week. Retries and multiple workflows on one SHA
  count once. This measures CI revisions, including integration revisions, rather
  than the number of author commits or successful tests.
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
  Actions runs created that week, regardless of event or conclusion. Duration is
  `updated_at - run_started_at`, an API approximation that excludes initial queue
  wait and may include later metadata updates. Only the latest retained attempt
  per run is available; unfinished runs are excluded.
- **Merge queue drops:** removal timeline events that week, including repeated
  removals and PRs still open or closed without merging. No failure cause is inferred.

PRs are discovered by last update, including all states. REST and timeline
connections are paginated. Actions history starts at the earliest creation date
of the selected PRs to include pre-window testing. Deleted/expired runs, older
attempts and runs without a PR association cannot contribute to per-PR metrics;
zero means no retained associated run was observed. This is a current API snapshot,
not an immutable historical audit. Large windows or old active PRs may be expensive.
GitHub's 1,000-run filtered Actions cap fails explicitly; narrow the window if
possible. API failures return nonzero with no partial report. Offline tests replace
one `gh_json` boundary with synthetic JSON; no live access is needed for tests.

API definitions: [workflow runs](https://docs.github.com/en/rest/actions/workflow-runs)
and [queue timeline events](https://docs.github.com/en/graphql/reference/pulls#addedtomergequeueevent).
