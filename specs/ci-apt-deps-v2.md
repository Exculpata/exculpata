# CI apt deps v2: shared main-produced apt state

Throwaway dry-run draft, not for merge. The kickoff is recorded in the draft PR
body. No public agent-task issue exists yet.

## Problem and root cause

At regression baseline ba359737, six Ubuntu CI jobs independently invoke the
APT helper. Four cache only archives with effectively static keys. The browser
and full corpus jobs do not cache apt state. Every invocation refreshes package
indexes online under a 60-second cap, so slow mirrors can fail unrelated gates.

## Options and choice

1. Share a composite action that restores main-produced lists and archives,
   tries them offline, and acquires packages online only when necessary.
2. Publish a prebuilt GHCR image. That requires new publication authority,
   pinning and rebuild policy, changes Docker and OCR staging assumptions, and
   still needs APT during image creation.

Choose option 1. It extends the reviewed acquisition helper without adding a
new runtime trust or egress surface. The visible benefit is fewer CI failures
caused by mirror availability, with unchanged product behavior and package sets.

## Mechanism

- `.github/actions/apt-deps/action.yml` accepts `name` and `packages` and owns
  restore, installation and save for all six call sites.
- Restore and save use exactly `${{ runner.temp }}/apt-state`, containing
  `lists/` and `archives/`. Keys are `apt-<name>-<os>-<arch>-<run_id>`;
  prefix restore selects the newest available entry.
- The helper's `--state` supplies `Dir::State::lists` and
  `Dir::Cache::archives`. It creates partial directories and returns ownership
  to the runner after success. The legacy `--archives` interface remains usable.
- Preflight runs `apt-get install --no-download --simulate` with the same
  options and package set as real installation, including
  `--no-install-recommends`. Unavailable cached dependencies fail before unpack
  or configuration and permit the online miss path. A passing preflight runs
  one `--no-download` installation, with no update, retry or timeout around dpkg.
- Online acquisition retains `update --error-on=any`, download-only acquisition,
  the one-shot official HTTPS mirror fallback, and `validate_sources`. The update
  cap becomes 120 seconds; downloads remain capped at 180 seconds. Each bounded
  acquisition allows 10 seconds to terminate remaining processes.
- A marker is cleared at the start and written only after successful online
  acquisition and installation. Pure hits cannot inherit permission to save.
- Saves require both that marker and the existing main push or schedule writer
  condition. Scheduled main runs pass `--refresh`. Branches, PRs, merge groups
  and manual dispatch never write the cache. No dispatch writer is added.
- Package-set changes are handled by preflight availability, without hash keys
  tied to source files or custom age windows.

## Trust boundary

Lists and archives are saved after a successful `apt-get update` and acquisition
on main. We rely on that authenticated producer acquisition plus the main-only
writer policy. APT can take a size-based fast path for cached archives; this
change does not claim it re-hashes every cached `.deb` against signed Packages
on a hit. There is no custom crypto, signed-Date age window or hash chain.
Signature validation, mirror trust, egress checks and hosted-runner guards stay
unchanged. No allowlists or guard exceptions are introduced.

## Files and compatibility

Migrate the five quality-gates jobs and corpus `full` to the action. Keep
publication pip dependencies, contributor setup, PostgreSQL image pull/tag,
FFmpeg and ffprobe checks, independent recognizer pip dependencies and OCR data
staging intact. Corpus `fast` stays unchanged. Update browser dependency setup
documentation, helper tests and workflow contracts. Classify every path beneath
`.github/actions/apt-deps/` as a browser dependency so action-only edits run the
synthetic browser gate.

## Verification and evidence limits

Write regressions before implementation for matching preflight/install package
selection and apt action directory classification. Both must fail against
ba359737. Existing application-shard discovery includes these tests and the
fixture-write policy tests. Local dry-run validation is compilation and the
file fence only; pytest and real APT proof belong to GitHub CI.

The workflow contract checks all six callers and unchanged package sets, no
inline apt/cache steps, identical cache paths, rotating keys, main-only saves
and scheduled refresh. Helper tests cover a hit without acquisition, a miss,
refresh, marker lifecycle, matching flags and a hard post-preflight failure.
Existing source validation and official-mirror fallback regressions remain.

A branch can prove action resolution, online installation on a cache miss with
the longer timeout, read-only restore, skipped save, and passing contracts.
It cannot prove a save or a cache hit before main has written the new keys.
A skipped job or unexecuted test is not evidence of success.

For a separate local producer/consumer experiment, build an Ubuntu 24.04 x86_64
base with sudo and runner-style mirror/source fixtures, with none of the
requested packages installed. Run the producer online, copy only apt state,
then mount it in a fresh consumer from the same base with `--network none`.
Assert the packages are initially absent, run the helper, and verify installation
and no update. `--network none` prevents external connectivity; it does not by
itself prove that no connection was attempted. Set `GITHUB_ACTIONS=true` only in
that disposable harness, without changing the hosted-runner guard.

After separate approval and a future merge, main pushes can seed the first four
quality caches. Synthetic-browser and corpus need successful main scheduled
runs first. Later branch runs must actually trigger those jobs to prove hits.
This throwaway draft does not authorize that merge or cache seeding.

## Risks and review

Cached indexes can become stale between daily refreshes; eviction or unavailable
packages take the online path. Unpack or configuration failure remains fatal.
The two application shards share a cache key and may race to save the same
successful state; only the first writer wins. Real hosted evidence is required
for permissions, cache transport and package installation.

Fix level: shared implementation plus static contracts and synthetic tests.
No new contributor judgment rule is needed. This preserves VISION.md's local
case-data boundary and AGENTS.md's neutral language, evidence and review rules.
