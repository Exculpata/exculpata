# Exculpata 0.1.0-beta.1

**Current community beta prerelease.** Install from the `v0.1.0-beta.1` tag.
The maintainer applies that tag. Checksummed source artifacts and recorded
image and model identities are follow-ups for a later beta, not prerequisites
for this prerelease.

This milestone targets a reproducible Linux GPU installation for evaluation and
contribution. The reference journey covers local accounts, document review with
cited answers, English/Spanish transcription and alignment, and ungated Nemotron
speaker attribution. Operators choose and procure their models; the documented
reference profile is the tested starting point, not an exclusive model policy.

## Changes prepared for this beta

- Reproducible, versioned WhisperX compatibility build with retained notices.
- Explicit acquisition preview, pinned artifacts and offline model verification.
- Correct cold-start native caches, retrieval startup and API/worker admission.
- Visible transcript processing limitations and notice-preserving report exports.
- Correct cleanup-process status semantics without changing retention policy.
- Managed companion-image builds that apply available distribution fixes.
- Local-account management, consistent encrypted backup and isolated restore
  verification, with public evaluation and contributor instructions.

Start with [the reference installation](BETA_REFERENCE_INSTALL.md),
[model acquisition](MODEL_ACQUISITION.md), [startup troubleshooting](RUNTIME_STARTUP.md)
and [transcript processing notices](TRANSCRIPT_PROCESSING_NOTICES.md).

## Ethics assessment kit

The [data-handling statement](ethics/DATA_HANDLING.md),
[vendor-vetting checklist](ethics/VENDOR_VETTING_CHECKLIST.md) and
[client discussion template](ethics/CLIENT_DISCLOSURE_TEMPLATE.md) connect
reference implementation evidence with jurisdiction-specific review questions.
They document network exceptions and bounded tests, not a blanket offline,
privacy or legal-compliance guarantee. Complete the assessment for the actual
installation and client before use. Not legal advice. Verify the rules in your jurisdiction.

## What this prerelease already exercised

The reference run passed synthetic GPU document review, English/Spanish
Nemotron workflows, offline-network and worker-restart checks, and replacement
node restoration of accounts, saved answers and transcript exports. These are
bounded evaluation results, not clean-host or human-quality acceptance.
Checksummed source artifacts, recorded image and model identities, and image
advisory dispositions are follow-ups for a later beta.

## Limits that remain after a community beta decision

Local accounts are the reference identity scope. OIDC and Windows-domain sign-in,
other languages, hardware and operator model combinations require separate
qualification. Independent unfamiliar-operator testing and representative human
language/speaker evaluation are not yet available. Synthetic audio demonstrates
pipeline operation, not real-world accuracy. Review transcripts, speaker labels
and cited answers against the original material.

The beta does not establish production certification, high availability,
universal model compatibility, or confidential-workload recovery guarantees.
No private deployment, credentials, records or internal runtime settings are
included. Use synthetic material when reporting bugs and follow the publication
and security guidance before sharing diagnostic information.
