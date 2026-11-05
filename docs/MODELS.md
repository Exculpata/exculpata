# Local model portfolio

Exculpata is local-first by default. The shipped retrieval and review stack is:

- Bundled generator: the generator pinned in `config/models.json` fails the project's origin rule, so do not use it for real cases.
- Dense retrieval: `ibm-granite/granite-embedding-english-r2`.
- Reranking: GTE ModernBERT cross-encoder; a Granite reranker swap is pending.
- Speech recognition: faster-whisper `large-v3`, orchestrated by WhisperX.
- Diarization: `nvidia/Nemotron-3-Diarization` by default; pyannote Community-1 is an optional gated alternative.
- Review: deterministic checks of citation references and text consistency after generation.

## Allowed for real cases

The origin rule excludes Chinese-origin models from real cases, including the bundled generator and reranker.
Do not use the bundled generator or reranker for real cases. The pending reranker
swap is not part of the shipped stack.

The origin-eligible components today are Granite embedding, faster-whisper ASR,
and Nemotron diarization. Origin eligibility does not replace license, offline
readiness or runtime acceptance checks. Community-1 and its dependencies need
origin and terms review before real-case use; its availability is not approval.

No generator is picked yet. Picks per tier are pending hands-on tests on the
test machines; until then, a model or endpoint you choose is your own call.
[`config/model_qualifications.json`](../config/model_qualifications.json) has no
entries, so no generator has a recorded claim-evaluation pass. A model without
such a pass is labeled `unvalidated model`.

## Review limits

These checks do not establish that generated claims preserve source meaning or
are correct. Compare each claim with the original sources using the
[generated-answer review guidance](GENERATED_ANSWER_REVIEW.md).

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

For an explicit `before` or `after` clock question, a stable coverage check also
promotes existing document passages that contain all query subject terms and a
clock on the requested side of the cutoff. It runs before primary-result and
answer-anchor truncation, without increasing either budget. Attribution and
uncertainty remain in the original passage. This limited clock comparison does
not resolve dates, time zones, clock reliability, object identity or uninterrupted
custody; transcript playback offsets are not treated as wall clocks. Ambiguous
multi-clock questions retain the underlying ranking. Evaluate selected packet
coverage separately from generation quality or whether a model answers.

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
tokens are staging-only. Stage pinned artifacts for offline runtime and disable
telemetry. Remote endpoints require explicit consent to send matter text.

Bring-your-own answer servers are described in
[model configuration](MODEL_CONFIGURATION.md#existing-answer-endpoints).
Embeddings and reranking remain in-process, including when
`CASE_REVIEW_MODEL_DEVICE=cpu`; endpoint selection does not replace them or
resolve their origin eligibility. Remote endpoints require explicit consent to
send matter text and are never recommended or selected by default.

## Hardware tiers by usable model memory

Detection uses the bounds below, not nominal hardware labels. Each budget must
cover weights, the KV cache that holds prompt and generated-token state, runtime
buffers and headroom. Precision, context length, concurrent requests and other
services change what fits; these rows make no model-fit, speed or quality promise.

Apple Silicon uses unified memory shared with the operating system and the app.
Detection allocates 75 percent to usable model memory, reserving 25 percent for
other work. The Apple rows describe that usable amount, not total installed RAM.
CPU rows use reported RAM; no fixed model-memory fraction is assigned, so subtract
the operating system, app and other services before budgeting model allocations.
GPU figures are reported VRAM per card, not free memory: subtract other service
allocations and runtime overhead. Tensor-parallel placement needs acceptance on
that exact topology; aggregate VRAM alone does not establish that a model fits.

The tier IDs, order and detection bounds in this table are checked against
`config/model_profiles.json` by `tests/test_installer_profile_prompt.py`.

<!-- model-profile-table:start -->
| Tier ID | How detection reads it | Usable model memory | What fits in the budget | Generator status |
| --- | --- | --- | --- | --- |
| `potato` | CPU only, 0 MiB through 14335 MiB reported RAM | Less than 14 GiB RAM before OS and app reservations | Weights plus KV cache and headroom must share the remaining RAM. | pending tests |
| `cpu` | CPU only, at least 14336 MiB reported RAM | At least 14 GiB RAM before OS and app reservations | Budget weights, KV cache and headroom after other RAM use. | pending tests |
| `gpu-8` | GPU, 8192 MiB through 12287 MiB reported VRAM per card | 8 to less than 12 GiB per card before reservations | Weights and KV cache must leave room for runtime and shared services. | pending tests |
| `gpu-12` | GPU, 12288 MiB through 16383 MiB reported VRAM per card | 12 to less than 16 GiB per card before reservations | Weight precision and KV cache length must fit with headroom. | pending tests |
| `gpu-16` | GPU, 16384 MiB through 24575 MiB reported VRAM per card | 16 to less than 24 GiB per card before reservations | Budget weights and KV cache together with runtime and service headroom. | pending tests |
| `gpu-24` | GPU, at least 24576 MiB reported VRAM per card | At least 24 GiB per card before reservations | Larger allocations still need measured room for weights, KV cache and headroom. | pending tests |
| `apple-16` | Apple Silicon, 12288 MiB through 24575 MiB usable model memory | 12 to less than 24 GiB, 75 percent of unified memory | Weights, KV cache and runtime headroom share the usable allocation. | pending tests |
| `apple-32` | Apple Silicon, at least 24576 MiB usable model memory | At least 24 GiB, 75 percent of unified memory | Weights and KV cache must leave headroom within the shared allocation. | pending tests |
| `ultra` | At least 2 cards, each at least 46068 MiB reported VRAM | At least 46068 MiB per card before reservations | Weights, KV cache and headroom need accepted placement on each card. | pending tests |
<!-- model-profile-table:end -->

### Installer note

The installer's tier prompt still prints generator text from
[`config/model_profiles.json`](../config/model_profiles.json). That text predates
this page and is not a pick for real cases; a separate change will update it.
The table contract checks tier IDs and detection bounds, not that generator text.

After the identity questions, the installer shows detected hardware and suggests
a tier ID. Press Enter to accept the hardware tier, choose another ID, or pass
`./install install --non-interactive --model-tier gpu-12` with your other options.
The ID is saved as `model_tier` in `installation.json` and retained on resume.
If hardware detection fails, the suggested tier is `potato`, with an explicit
CPU fallback message.

These tiers record hardware guidance. Installed capabilities still use
`--models`; bundled model selection and GPU admission still use the existing
`--review-model-profile` and preflight checks. Selecting a tier does not download
or configure an answer server or qualify a model for real cases.

The [R1b claim-evaluation harness](CLAIM_EVALUATION.md) freezes synthetic challenges,
separates raw generation errors from deterministic verifier false acceptance and
rejection, and retains pinned-profile and execution evidence. Injected probes do
not establish model quality, offline inference or supported-hardware acceptance.

[Question drafting](QUESTION_DRAFTS.md) uses the same configured generator with
its own schema and check; it adds no model or revision. Its fixed synthetic set
and pass bar are recorded there, and its model gate stays outstanding until the
evaluation runs against a configured runtime. A passing receipt is bound to a clean
commit, implementation hashes, the model's content digest, and a runtime profile
linking that digest to the pinned generator revision.
Its portable question-generation policy explicitly disables thinking. Quality,
large and custom question policies can enable thinking with independently
allocated planning and final-answer requests; capacities are operator-configurable.
See [question generation profiles](QUESTION_DRAFTS.md#question-generation-profiles).
These policies do not change model pins or ordinary answer/classification defaults,
and a transport test does not establish acceptance on a larger model or topology.
