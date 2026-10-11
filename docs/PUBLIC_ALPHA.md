# Public contributor alpha

The maintainer authorizes publication of the environment-neutral source as a
development alpha. This is distinct from a supported production release.
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
python3 scripts/install-publication-hook.py ~/.local/share/recordbench-publication-guard/reviewed-v1
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
reachable annotated tag object. CI also runs on tag pushes. Install `pypdf`
and `ffprobe` as described in the publication checklist; missing inspection
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
git config --local recordbench.requirePrivatePublicationCheck true
git config --local recordbench.publicationDenyFile /secure/path/private-deny.txt
```

Exact public clone URL and author-identity exceptions use the scanner's
documented adjudication options. The hook reads `recordbench.publicCloneUrl`,
`recordbench.publicGitIdentity` (tab-separated name/email), and
`recordbench.publicBaselineIdentity` (tab-separated commit/name/email).
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
