# Release readiness

Exculpata is currently alpha. The next community beta should make it easier
to install, try with synthetic material, and contribute using public instructions.
The full supported-deployment requirements below remain a separate acceptance
target; they should not prevent publishing reviewed source improvements.

The [GPU beta reference journey](BETA_REFERENCE_INSTALL.md) gives the candidate
commands, concrete acceptance steps and the evidence still pending.

## Community beta exit criteria

A beta designation requires an exact reviewed candidate and explicit release
approval. It does not follow merely from documenting these criteria.

- The public contributor setup and one advertised reference installation/workflow
  must work from clean task-owned state, with limitations and recovery steps
  documented. Full GPU review/transcription remains the intended reference scope;
  do not advertise that workflow as working based only on mock or import tests.
- Required CI, publication checks, access boundaries and safe handling of source
  data remain required. Record security findings with their concrete exposure,
  mitigation or fix; neither an unassessed finding nor a blanket waiver suffices.
- Identify what ships in source/packages/images and preserve its notices. For
  operator-acquired models/resources, provide upstream terms and acquisition
  instructions, expose unresolved terms before acquisition, and document adapter
  compatibility. See [model configuration](MODEL_CONFIGURATION.md). Universal
  certification of user-selected models is not a release requirement.
- Provide reference-model results, ordinary failure/retry behavior and a useful
  feedback path. Label untested hardware, model combinations and languages.
- Independent operator review, broad human accuracy studies, comprehensive
  hardware coverage and full production recovery certification remain follow-up
  evidence. Their absence must limit the beta's claims rather than prevent safe
  source collaboration. Do not promise production support before those relevant
  acceptance requirements pass.

## Supported-profile objective

- **Evaluation:** CPU-only, synthetic data, local identity, document intake,
  extraction, lexical search, manual review, export, and deletion.
- **Review:** one or more supported NVIDIA GPUs, learned retrieval and
  reranking, and a pinned local generator.
- **Full:** Review plus modular ASR/alignment and optional separately accepted
  diarization.

GPU count is adaptive rather than an edition boundary. One supported GPU is a
valid full-profile target; larger hosts separate service lanes automatically or
accept explicit assignments. Hardware support still requires a receipt from
the exact card, VRAM, driver, runtime, collection, and concurrency shape.

## First supported release target

The initial target is the Full GPU profile with local accounts, English and
Spanish transcription/alignment, and ungated Nemotron diarization. OIDC,
Windows-domain sign-in and other transcription languages remain experimental
until separately qualified. This narrows the initial support claim, not the
requirements for confidential-data handling, recovery or disclosure review.
This supported-deployment target is not yet qualified. See
[the supported-release target](SUPPORTED_RELEASE_TARGET.md); the community beta
above has a narrower installation and contribution promise.

## First-party release authorization

The maintainer has confirmed that release of Exculpata's first-party code
under Apache-2.0 is approved. This records the maintainer-confirmed approval
status; it does not complete third-party license review, sanitization, or
deployment acceptance. Keep any supporting private approval records outside
this repository.

## Contributor source publication

The maintainer authorizes an environment-neutral public contributor alpha
under the [public-alpha requirements](PUBLIC_ALPHA.md). This separates source
collaboration from a supported binary/container release. Sanitization, private
data exclusion, ownership, applicable license notices, and review gates remain
mandatory. No supported-deployment or quality claim follows from public access.

## Supported-release requirements

The following evidence is required before declaring a supported release or
distributing first-party production images:

1. documented first-party ownership/publication authorization;
2. dependency/container notices and redistribution review for shipped artifacts;
   upstream terms/acquisition records for reference models and clear operator
   responsibility for their own model selection, without implying universal
   model compatibility or licensing approval;
3. a clean new root or explicitly approved history rewrite and author-email
   decision;
4. locked Python resolution, digest-pinned build inputs, image SBOMs,
   vulnerability scans, checksummed release artifacts, and provenance;
5. green hosted CI plus clean-host CPU evaluation acceptance;
6. acceptance receipts from a supported one-GPU review node and full media
   node;
7. browser/accessibility and local-account acceptance for the initial supported
   target; OIDC and representative Kerberos acceptance before either is promoted
   from experimental support;
8. encrypted backup, bare-metal restore, update, rollback, and queued-work
   restart evidence;
9. versioned retrieval, citation, classification, transcription, latency, and
   resource evaluation artifacts before publishing quality claims;
10. an installation performed by an unfamiliar operator using only the public
    playbook.

No bundled synthetic-policy benchmark is a production quality or throughput
claim. Until an executable evaluation artifact exists, the UI reports that
deployment evaluation is required.

## Supported-release procedure

1. Freeze and hash a private rollback bundle.
2. Run the generic scanner and an external private deny file over the tree and
   every reachable Git object.
3. Run an independent secret scanner and adjudicate every candidate without
   copying possible values into logs.
4. Inspect document, archive, image, audio, and video metadata and render
   documents visually.
5. Complete all tests and deployment acceptance lanes.
6. Review the complete local diff, GitHub access, deploy keys, Actions
   artifacts, variables, releases, Pages, and packages.
7. Obtain the required approvals.
8. Create the approved clean history, verify it again, and only then request
   explicit authorization to push or change visibility.
