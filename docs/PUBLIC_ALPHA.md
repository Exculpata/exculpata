# Public contributor boundary

The maintainer authorizes publication of the environment-neutral source. The
current prerelease is community beta v0.1.0-beta.1. This is distinct from a
supported production release.
First-party Apache-2.0 approval is recorded in [release readiness](RELEASE_READINESS.md).
The repository does not publish private deployments or represent an operator's
endorsement. No model weights, credentials, or live evidence are distributed.

## Publication requirements

Before visibility changes, the maintainer checks the exact candidate and every
published branch, tag, and pull-request history; uses the generic sanitizer,
an independent secret scanner, and a private operator deny list kept outside
the repository; reviews synthetic media and third-party notices; and inspects
GitHub descriptions, discussions, logs, artifacts, releases, and settings.
False positives require a narrow, recorded disposition rather than removal of
the underlying check. New public author identities are deliberate and should use GitHub
no-reply email addresses. The maintainer explicitly accepted the existing
author/committer email on commit `d2f1e779e70063b94fece1291169f547c7c44d83`.
The scanner records that reviewed attribution by exact commit and identity
digest, suppressing only the non-example-email finding for those metadata
fields. It does not permit the address in file content, commit messages, other
commits, or unrelated identities. Operator deny terms still apply unless the
operator explicitly supplies the same exact commit/name/email through the
existing baseline-identity option. For this reviewed personal attribution, that
option covers only the named commit, never its ancestors. Keep the private audit and its deny terms outside Git.

Source publication does not ship a production image or promise installation,
hardware, recovery, legal accuracy, completeness, or confidential-workload
acceptance. The full supported-release requirements remain in
[release readiness](RELEASE_READINESS.md). Optional dependencies and models
have separate upstream terms; see [third-party notices](../THIRD_PARTY_NOTICES.md).

The maintainer also approved the exact author attribution on merge commit
`4b8d74be09f41a443abb705bed1c6ab8bb5371c7`. Its disposition follows the same
commit-and-identity boundary above and grants no exception to later commits.

The maintainer approved the existing attribution on commit
`f8ccae72b39704942c23caecb8195874a74e7699` under the same exact-commit and
identity-digest rule. This historical disposition does not rewrite published
history. New commits and merges must use the responsible contributor's validated
GitHub no-reply attribution; this exception does not authorize later
personal-address attribution. Contributors use their own public GitHub username
and no-reply address and retain credit for their work. A maintainer's identity
applies only to commits created by that maintainer or on their behalf; it is not
an identity requirement for outside contributors. Preserve contributor authorship
when landing a PR, and use the maintainer's no-reply identity for any new merge
commit the maintainer creates. Publication checks, review requirements and branch
protections apply to every contributor.

The maintainer explicitly accepted keeping merge commit
`a8f4fbe36439a374746f9c2b2ca9660d89793044` in public history with its existing
author attribution. Its disposition uses only that exact commit and identity
digest, with the same metadata-only boundary above. It does not authorize any
future use of that attribution or relax operator deny checks. Do not use a merge
endpoint that cannot enforce the validated no-reply author; use the explicit
CLI author option or protected fast-forward procedure below.

Exact-commit public merge adjudications also recognize a complete
`Co-authored-by` trailer for a configured, validated GitHub no-reply identity.
Other message text, identities and commits retain the operator deny checks.
The Nemotron dependency lock has one exact package/version-line disposition for
a reviewed public CUDA dependency version that resembles a private IP address;
other files, package names, endpoints, secret rules and deny terms remain checked.

When using the GitHub CLI to merge, explicitly pass `--author-email` with the
public no-reply address validated for the reviewed head. The account default may
use a personal address even when local commits use no-reply attribution. If
GitHub rejects the public address, use a normal protected fast-forward of the
reviewed head when possible; do not remove branch protections or silently retry
with the account default.

The maintainer approved the existing author attribution on squash merges
`624fbe4dcfbae093c442955bfb6cc4b6be6fff3e` and
`6735691a938e42173c6c7a309b700be2e19c0668` for the October 9 merge train.
Each recorded disposition covers only its exact commit and identity digest.
It does not permit email addresses in content or messages, cover other commits,
or relax operator deny checks. The recorded dispositions do not change account
email or privacy settings.

## Prevent disclosure before publishing

CI runs after content reaches GitHub. It cannot prevent an initial disclosure.
Install the local pre-push check in every publishing checkout:

```console
python3 scripts/install-publication-hook.py ~/.local/share/exculpata-publication-guard/reviewed-v1
```

Run the installer from a trusted, reviewed checkout with the Python environment
that contains the scanner dependencies. Both that environment and the new
installation directory must be outside the publishing checkout. Interpreter
symlinks must also resolve outside it. The installer
copies the reviewed scanner and hook, pins the interpreter, and enables isolated
Python execution for both hook and scanner, removing inherited Python environment
variables from the scanner process. Branch switches cannot replace this installed code. Reinstall
reviewed updates into a new directory. Do not point `core.hooksPath` at the
branch-owned `.githooks` directory.

The hook scans every outgoing branch/tag and its reachable history in an
isolated temporary repository before Git transmits objects, including every
reachable annotated tag object. CI also runs on tag pushes. Install `pypdf`,
`ffprobe`, and `exiftool` as described in the publication checklist; missing inspection
dependencies block publication. The exact public GitHub web-flow merge-service
address and Dependabot's public no-reply identity
`49699333+dependabot[bot]@users.noreply.github.com` are recognized. Dependabot's
Signed-off-by trailer is recognized only when the whole line is that bot's
sign-off; the same support mailbox anywhere else still blocks. Other non-example
addresses and configured deny terms still block. A removed file still exists in history and
still blocks. Keep sensitive work in a separate repository with no public
remote. Never use mirror pushes or push private deployment histories.

Operators developing alongside private deployments must additionally configure
an owner-only deny file outside the checkout:

```console
git config --local exculpata.requirePrivatePublicationCheck true
git config --local exculpata.publicationDenyFile /secure/path/private-deny.txt
```

Exact public clone URL and author-identity exceptions use the scanner's
documented adjudication options. The hook reads `exculpata.publicCloneUrl`,
`exculpata.publicGitIdentity` (tab-separated name/email), and
`exculpata.publicBaselineIdentity` (tab-separated commit/name/email).
The same suffixes under `recordbench.` are still read when the `exculpata.`
entry is absent.
Do not add general exceptions for private names or paths.

Hooks are local safeguards, not a security sandbox: another client, an API
upload, or `--no-verify` can bypass them. Maintainers must not bypass the check.
Review outgoing issue/PR text, comments, screenshots, attachments, workflow
logs, package metadata, and release assets separately before upload. Never
paste production output into a public report, even when filing a bug. Use a
content-free description and reproduce it with synthetic material.

GitHub secret scanning/push protection covers supported secret patterns, not
arbitrary private case facts. A passing scan is evidence of checks performed,
not a guarantee that all confidential information was recognized. If a leak is
suspected, stop publishing and report privately; removing it in a later commit
does not remove public copies or history. Revoke exposed credentials first.

## Merge and automation boundary

Follow the single [review policy](QUALITY_GATES.md#review-policy) and its
[CI scope rules](QUALITY_GATES.md#ci-scope). Publication requirements above
continue to apply independently of review.

External contributor workflows require maintainer approval. Use hosted runners
with synthetic fixtures; do not attach private infrastructure or deployment
credentials to public Actions. Default workflow permissions are read-only and
actions are pinned to full commits. Keep force pushes and branch deletion blocked.
Release tags, including historical protected tags, remain immutable.

Publication identity dispositions support SHA-1 and SHA-256 repository object IDs.

## Reviewed binary media

Compressed bytes can resemble text by coincidence. The trusted scanner's
`scripts/publication-media-reviews.json` records exact SHA-256 dispositions.
Only an unexpired matching digest **and suffix** skips raw text-pattern checks;
all extracted metadata and filenames still receive every check, including
operator deny terms. Unreviewed and changed bytes keep raw scanning. Missing,
unsafe or malformed policy and unavailable or failed metadata inspection block
publication. Existing narrow historical video dispositions remain unchanged.

The version-2 JSON object has `version: 2` and a `reviews` array. Each entry
contains exactly `sha256` (64 lowercase hex characters), `suffix` (a supported
lowercase image/audio/video extension), `reason`, `status`, `approver`,
`approved_on`, and `expires`. Dates use `YYYY-MM-DD`. An approved entry must
name a maintainer from the trusted sibling `publication-approvers.json`, have an
approval date no later than today, and expire within 180 days of that date.
Duplicate digest/suffix entries are invalid. Expired entries remain valid records
but grant no exception and contribute no active approver requirement. Protected
policy or code changes still require a fresh maintainer review.
A pending entry has null `approver` and `approved_on` and grants no exception.
The committed synthetic GIF has explicit maintainer authorization dated
2026-10-17 through 2026-11-16 for its exact digest and GIF suffix. This records
the maintainer's decision; it is not an agent approval or evidence of a hosted
GitHub review. Agents must never originate exemption approvals. The separate
final-head GitHub review and trusted CI activation requirements below remain.

Review must cover rendered media, complete metadata and embedded payloads.
A name in a JSON file is not evidence of approval. The read-only trusted
`publication-approval.yml` workflow uses verifier code and the login/numeric-user-ID
allowlist from its trusted base revision. It fetches candidate policy only as
bounded JSON data and verifies live GitHub reviews for the exact PR head. It
never checks out or executes candidate code, uses no API credentials, and
persists no checkout credentials. The live PR base SHA must match the actual
trusted checkout SHA. Changed paths come from complete immutable base/head Git
trees, including deleted paths and mode changes. Mutable PR file lists are not
used. API errors, pagination limits and oversized or truncated trees fail closed. Changes to policy, approval authority, scanner, hooks,
workflows, Makefile, CODEOWNERS or agent instructions require review, including renames.

The allowlisted maintainer must submit a GitHub PR review on the current head
with this exact standalone line, substituting the SHA-256 of the complete policy
file bytes:

```text
Publication exemptions approved for policy SHA-256: <policy-file-sha256>
```

An `APPROVED` review or an explicit `COMMENTED` review is accepted; the latter
supports a maintainer's own PR, which GitHub does not allow them to approve.
Generic comments, bot identities, mismatched user IDs, stale heads and mismatched
policy digests fail. The latest review from each maintainer controls; dismissal
or a later review without the attestation removes authorization. Every named
policy approver must have a qualifying review. This verifies control of the
maintainer's GitHub account, not human presence; agents must never issue the
attestation using a maintainer's credentials.

**Activation and enforcement:** this new workflow cannot authorize itself while
it exists only in a candidate branch. A maintainer must independently review and
bootstrap the verifier, workflow and authority list onto the trusted default
branch. CODEOWNERS documents ownership; it does not itself enforce approval.
Where the repository plan supports it, a maintainer must separately configure a
[required-workflow ruleset](https://docs.github.com/en/enterprise-cloud@latest/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets#require-workflows-to-pass-before-merging)
for this trusted workflow, not merely a candidate-spoofable check name. If that
feature is unavailable, enforcement needs a separately trusted integration; this
patch does not provide one. No repository protection settings are changed. Until those steps are complete, trusted CI
enforcement is not active. Pending exemption entries still fail the local scanner.

**Revocation limitation: a previous green run can remain stale after a review
changes. This patch does not provide automatic revocation enforcement.**
After submitting, dismissing or changing a review, run trusted verification on
the exact current head before any publication decision. Dispatch from `main`
with the PR number and expected head; if `main` has advanced, start a fresh run
rather than rerunning an old checkout. A stale trusted-base SHA fails closed.

The `pull_request_review` event executes a workflow from the PR context. A
candidate-controlled review-event workflow, even if it only wakes a trusted
`workflow_run` consumer, can be removed or altered to suppress the refresh.
It cannot establish the required revocation guarantee. The existing read-only
permissions also cannot invalidate an earlier check result. Reliable automatic
refresh/invalidation needs a separately trusted integration and separately
authorized enforcement configuration. No such relay, credentials, permissions
or settings are added here. See [GitHub review-event semantics](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request_review).
Do not treat a local run of candidate verifier code as trusted approval evidence.

The hook installer copies policy and approver authority alongside its scanner
outside the checkout. Editing candidate files cannot change an installed hook.
After independently verified maintainer approval, review and reinstall into a
fresh directory to adopt an update. The scanner enforces the trusted bundle's
policy locally; GitHub review verification is a separate trust step. The existing
bare-digest CLI option retains only its older narrow collision behavior and
cannot grant the broader reviewed-media exception. Never bypass the hook.

Image inspection requires ExifTool (`libimage-exiftool-perl` on Ubuntu,
`exiftool` on Homebrew) for GIF, PNG, JPEG and WebP. Audio/video inspection uses
ffprobe and includes format, stream and chapter tags. Metadata values are
inspected after JSON decoding; image binary metadata values are decoded too.
ExifTool configuration loading is disabled so candidate files cannot execute
Perl configuration during inspection. Missing tools, parser errors, warnings
and image type mismatches fail closed.
PDFs, archives and other files retain their existing inspection requirements.
