# Local model portfolio

The review pipeline uses one pinned generator tier plus the same retrieval
stack:

- portable: Qwen3.5 4B for constrained or shared-GPU nodes;
- quality: Qwen3.5 9B when the selected generator device has sufficient VRAM;
- Granite Embedding English R2 for dense retrieval;
- GTE ModernBERT cross-encoder for reranking;
- deterministic checks of citation references and text consistency after generation.

These checks do not establish that generated claims preserve source meaning or
are correct. Compare each claim with the original sources using the
[generated-answer review guidance](GENERATED_ANSWER_REVIEW.md).

Automatic selection considers visible GPU memory and whether transcription
shares the generator device. Operators may select a tier and GPU layout
explicitly, including tensor-parallel generator devices, but preflight rejects
known-unworkable combinations. The defaults are deployment candidates, not a
universal quality claim.

GPU count is not an edition boundary. The same workflows run on a supported
one-GPU node or a larger node; extra devices change placement and potential
throughput, not product behavior. Automatic placement uses at most three
service lanes. Deliberately spreading generation across more devices or adding
replicas requires acceptance on that exact topology.

Larger collection questions still use retrieval rather than placing thousands
of documents in one prompt. Collection-wide counts, exhaustive chronologies,
and document-by-document classification use dedicated research/full-review
workflows that preserve progress and per-source decisions.

Retrieval applies a bounded deterministic exact-record check after reranking.
For questions seeking an exact identifier or time, this check can rescue a
precise contemporaneous machine record from the already matter-scoped candidate
set when a later narrative summary ranked above it. It does not add candidates,
change scores, cross a matter boundary, or bypass citation validation.

An explicit request for both written and spoken support reserves evidence space
for each kind when matching passages are retrieved. The independent verifier
requires a machine-transcript caution prefix on transcript-only claims and rejects
machine-transcript attribution on document or mixed-source claims. This wording
check does not establish that the claim accurately describes the recording. If both
kinds do not survive retrieval and verification, the result is visibly partial.
Broad matter summaries use several bounded retrieval facets, diversify sources,
omit disclaimer-only passages, and disclose that the result is sampled
orientation rather than an every-source review.

The transcription profile is modular: faster-whisper/WhisperX `large-v3` ASR,
English alignment, Spanish alignment, translation through the staged ASR
artifact, and Nemotron diarization are separately described and selected.
Nemotron is ungated and supports up to eight anonymous speakers. Speaker-count
hints are retained for review but not applied by this model. Community-1 remains
an explicit gated alternative for existing installations. These models produce
anonymous speaker clusters, not identities. See [Nemotron integration](../services/transcription/docs/NEMOTRON.md)
for pinned inputs and the acceptance boundary. Transcripts describe what the machine heard and are not
automatically treated as facts.

Every model entry must retain model ID, exact immutable revision, license,
source, hashes/manifest, evaluation evidence, and GPU/runtime envelope. Gated
tokens are staging-only. Confidential runtime is offline and telemetry-free.

The [R1b claim-evaluation harness](CLAIM_EVALUATION.md) freezes synthetic challenges,
separates raw generation errors from deterministic verifier false acceptance and
rejection, and retains pinned-profile and execution evidence. Injected probes do
not establish model quality, offline inference or supported-hardware acceptance.

[Question drafting](QUESTION_DRAFTS.md) uses the same configured generator with
its own schema and check; it adds no model or revision. Its fixed synthetic set
and pass bar are recorded there, and its model gate stays outstanding until the
evaluation runs against a configured runtime.
