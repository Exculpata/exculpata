# CI apt deps v2: shared main-produced apt state

Implementation draft. The kickoff is recorded in the draft PR body; no public
agent-task issue exists yet. Readiness and merge remain separate decisions.

## Problem and root cause

At regression baseline ba359737, six Ubuntu CI jobs invoke the APT helper.
Four cache archives under effectively static keys; browser and corpus jobs
have no APT cache. Every invocation refreshes indexes online under a 60-second
cap, allowing mirror outages and slowness to fail unrelated validation gates.

## Options and choice

1. Share an action that restores main-produced lists and archives, tries an
   offline install, and retains the reviewed bounded acquisition fallback.
2. Publish a prebuilt GHCR image, requiring new publication authority, pinning
   and rebuild policy, changes to Docker and OCR staging, and APT at build time.

Choose option 1. The user-visible outcome is fewer mirror-related CI failures,
without changing product behavior, packages, case-data handling or egress rules.

## Mechanism

- `.github/actions/apt-deps/action.yml` accepts `name` and `packages` and owns
  restore, install and save for all six callers. Empty packages selects the
  helper's existing browser defaults.
- Restore and save use the identical `${{ runner.temp }}/apt-state` path,
  containing `lists/` and `archives/`. The key is
  `apt-<name>-<os>-<arch>-<run_id>`; prefix restore selects the newest entry.
- `--state` supplies `Dir::State::lists` and `Dir::Cache::archives`, creates
  partial directories, and returns ownership to the runner after success.
  The legacy `--archives` interface remains supported.
- Preflight runs `apt-get install --no-download --simulate`. Preflight and
  installation share one command builder, including `--no-install-recommends`
  and every package-selection flag, so missing Recommends do not force a miss.
- Only a failed simulation enters the online miss path. A passing simulation
  runs one no-download install; failures there are hard errors without retry
  or timeout around unpack/configuration.
- Online acquisition retains `update --error-on=any`, download-only acquisition,
  `validate_sources`, and the one-shot official HTTPS mirror fallback. Update
  is bounded to 120 seconds and downloads to 180 seconds, each with 10 seconds
  to terminate remaining acquisition processes. No signed-Date age window is added.
- A restored online marker is removed at entry. Successful online acquisition
  and installation create a new marker; pure hits cannot inherit it.
- Saves require the marker and the existing main push or schedule writer
  condition. Scheduled main runs force `--refresh`. Branches, PRs, merge groups
  and manual dispatch cannot save. No workflow_dispatch writer is added.

## Trust and compatibility

Main's authenticated acquisition and main-only cache writers are the trust
boundary. APT can use a size-based fast path for cached archives; this change
must not claim every restored archive is re-hashed against signed Packages.
Signature validation, source validation, mirror trust, egress checks and the
hosted-runner guard stay unchanged. No custom crypto or guard exception is added.

Migrate five quality-gates jobs and corpus `full`. Preserve publication pip
setup, contributor dependencies, PostgreSQL image pull/tag, FFmpeg/ffprobe
checks, recognizer pip dependencies and OCR staging. Corpus `fast` is unchanged.
Every path under `.github/actions/apt-deps/` requires synthetic-browser coverage.

## Verification and evidence limits

Required failing-first regressions on ba359737 check identical preflight/install
package selection and browser classification of all action paths. Workflow
contracts check all six package sets, no inline APT/cache steps, identical cache
paths, rotating keys, main-only writes and scheduled refresh. Helper tests cover
hits without acquisition, simulation failures, refresh, marker lifecycle and
hard installation failures. Existing mirror and source rejection tests remain.

For real APT evidence, build an Ubuntu 24.04 x86_64 base with sudo, Python and
runner-style source fixtures, with none of the requested packages installed.
Run a producer online, copy only its state, and mount that in a fresh consumer
from the same base with `--network none`. Assert packages are initially absent,
then verify installation and no update. Network isolation prevents external
connectivity but alone does not prove no connection attempt. Set
`GITHUB_ACTIONS=true` only in the disposable harness; never edit the guard.

A branch can prove action resolution, online miss installation with the longer
timeout, read-only restore, skipped save and passing contracts. It cannot prove
a cache save or hit before main has produced the new keys. Skips are not passes.
After a separately authorized merge, main pushes seed the first four quality
caches. Synthetic-browser and corpus require successful main scheduled runs.
Later branch runs must trigger those jobs to demonstrate their restored hits.

## Risks and review

Indexes can become stale between daily refreshes; eviction requires online
acquisition. Installation failures after successful preflight remain fatal.
The application shards share a key, so only the first successful save wins.
Real runner evidence remains necessary for permissions and cache transport.
Fix level: shared code plus workflow contracts and synthetic regressions,
without a new judgment rule. The change preserves VISION.md and AGENTS.md.
