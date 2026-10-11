# Initial supported-release target

This document defines the qualification target; it does not announce a release.
Exculpata is currently alpha. A supported production release requires the
evidence below and maintainer approval of the exact release artifacts.

The nearer community beta milestone is defined in
[release readiness](RELEASE_READINESS.md#community-beta-exit-criteria). These
full supported-deployment gates do not block reviewed source contributions or
require certification of every operator-selected model. Reference models are
test configurations, not the product architecture. Current configuration options
and incomplete custom-model workflows are explicit in
[model choice and compatibility](MODEL_CONFIGURATION.md).

| Capability | Initial support target | Evidence still required |
|---|---|---|
| Installation | Linux x86-64; fresh isolated root; documented NVIDIA runtime | Exact OS/card/driver/runtime receipt and independent operator completion |
| Sign-in | Local accounts and browser account management | TLS trust, first-admin setup, sessions, role changes and two-user separation |
| Document review | Ingestion/OCR, learned retrieval/reranking, local generation and cited exports | Frozen document gold, empty-cache/offline execution, measured resource envelope |
| Transcription | English and Spanish, large-v3 source transcription, word alignment, optional English translation | Frozen speech gold, native GPU execution, human transcript/timestamp review |
| Diarization | Ungated Nemotron with anonymous speaker labels | Frozen overlap/speaker-count cases and human label review |
| Recovery | Encrypted backup, clean-target restore, update/rollback and interrupted-job recovery | Exact source/image/model identities before and after each operation |
| Other sign-in methods and languages | Experimental | Separate acceptance before adding a support promise |

Hardware minimums, concurrency and recording-length limits must come from
measured acceptance receipts. A test on a larger GPU does not establish a
24 GiB minimum. Synthetic checks establish behavior on their frozen inputs;
they are not a general accuracy guarantee for real recordings or case material.

## Artifact gates

Before promotion, bind the source capsule and Git revision/dirty status to the
actual built image digests, OS and Python SBOMs, model/resource hashes and license
records. Distinguish configuration labels from observed container/image facts.
A missing identity remains unknown. Finish advisory applicability/remediation;
no generic vulnerability waiver follows from a successful unit test or pinned
checkpoint. Source-built dependencies need their exact source and patch as well
as the resulting wheel hash and preserved notices.

The first-party application, adapter tests and hosted CI are necessary but do
not replace full installation, offline inference or recovery. Upstream metadata
still prevents directly combining the current WhisperX release with the newer
Torch/Transformers runtime. Any maintained compatibility build must be declared,
installed with a coherent dependency contract, audited and independently
qualified. Model-data terms require their own evidence.

## Acceptance order

1. Resolve runtime compatibility, model-data terms and build inputs.
2. Freeze the candidate, synthetic corpus, expected outcomes and scoring rules.
3. Build in an isolated bounded builder; inventory exact image digests/SBOMs.
4. Install from empty state using only the public playbook; verify local sign-in.
5. Deny runtime egress and complete document review, ASR/alignment/diarization,
   citations, playback and export with two synthetic users and separate matters.
6. Restore a synthetic encrypted backup into a new empty target; exercise update,
   rollback and interrupted jobs without changing the source installation.
7. Have an unfamiliar operator complete the installation and a human reviewer
   score the frozen English/Spanish outputs. Record failures and limitations.
8. Review the exact public release payload and obtain release authorization.

Keep operator-specific measurements, paths, identities, credentials and raw
logs outside the public repository. Publish only separately reviewed,
content-free evidence and independently constructed synthetic reproductions.
