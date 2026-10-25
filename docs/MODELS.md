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
tokens are staging-only. Confidential runtime is offline and telemetry-free.

Bring-your-own answer servers and hardware suggestions are described in
[model configuration](MODEL_CONFIGURATION.md#existing-answer-endpoints). The
suggestion file is [`config/model_profiles.json`](../config/model_profiles.json).
Embeddings and reranking remain the in-process Granite and ModernBERT models,
including when `CASE_REVIEW_MODEL_DEVICE=cpu`. A model without a recorded
claim-evaluation pass is labeled `unvalidated model`. The qualification seam is
[`config/model_qualifications.json`](../config/model_qualifications.json).

## Hardware model tiers

After the identity questions, the installer shows detected hardware and
recommends one of these local tiers.
Press Enter to accept it, choose another tier ID, or pass
`./install install --non-interactive --model-tier gpu-12` with your other
installation options. The selected ID is saved as `model_tier` in
`installation.json` and retained when resuming an installation.
If hardware detection fails, the recommended tier is `potato`, with an explicit
CPU fallback message. The `ultra` tier requires at least two GPUs, each reporting
at least 46068 MiB of VRAM; aggregate VRAM alone does not qualify. For CPU-only
hosts, `cpu` starts at 14336 MiB of reported RAM and `potato` ends at 14335 MiB.

These tiers record hardware guidance. Installed capabilities still use
`--models`; bundled model selection and GPU admission still use the existing
`--review-model-profile` and preflight checks. Selecting a tier does not
download or configure an answer server. Use the existing
[bring-your-own configuration](MODEL_CONFIGURATION.md#existing-answer-endpoints)
to opt into an endpoint. Remote endpoints require explicit consent to send
matter text and are never recommended or selected by default.

This table is checked against `config/model_profiles.json` by
`tests/test_installer_profile_prompt.py`; update both together.

<!-- model-profile-table:start -->
| Tier ID | Hardware | Generator | Embeddings and reranking | Speech recognition | Trade-off |
| --- | --- | --- | --- | --- | --- |
| `potato` | CPU only, below 14 GiB reported RAM | Qwen3.5-4B Q4_K_M or Q5_K_M through your Ollama or llama.cpp server. Answers are slow; keep them short. | Granite embedding and the ModernBERT reranker on CPU, inside Exculpata. | faster-whisper small or medium, int8, or skip transcription. | Search and source review keep working. Generated answers are the slowest tier and are easier to mis-ground. This is a suggestion, not a measured quality result. |
| `cpu` | CPU only, at least 14 GiB reported RAM | Qwen3.5-4B Q4_K_M or Q5_K_M through your Ollama or llama.cpp server. Answers are slow; keep them short. | Granite embedding and the ModernBERT reranker on CPU, inside Exculpata. | faster-whisper small or medium, int8, or skip transcription. | A nominal 16 GB machine reporting at least 14 GiB can hold a 4B quantized model beside the application. Answers are still slow. This is a suggestion, not a measured quality result. |
| `gpu-8` | 8 GB GPU | Qwen3.5-4B Q5 or Q8, or Qwen3.5-9B Q4 with partial offload, through Ollama or llama.cpp. | Granite embedding and the ModernBERT reranker on CPU or GPU, inside Exculpata. | faster-whisper medium, int8. | A 4B model leaves more room and answers faster. A 9B Q4 model may place some layers in system RAM and run slower. This is a suggestion, not a measured quality result. |
| `gpu-12` | 12 GB GPU | Qwen3.5-9B Q4_K_M through Q6, through Ollama or llama.cpp. | Granite embedding and the ModernBERT reranker on GPU, inside Exculpata. | faster-whisper large-v3, int8_float16. | A quantized 9B model fits more comfortably than the full-precision bundled profile. Keep the KV cache at 8-bit or higher. This is a suggestion, not a measured quality result. |
| `gpu-16` | 16 GB GPU | Qwen3.5-9B Q6, or the bundled vLLM Qwen3.5-9B profile when the installer accepts the card. | Granite embedding and the ModernBERT reranker on GPU, inside Exculpata. | faster-whisper large-v3, float16 when the card has room. | 16 GB is enough for a quantized 9B model. The bundled full-precision profile still has to pass the installer's existing free-VRAM check. This is a suggestion, not a measured quality result. |
| `gpu-24` | 24 GB GPU and up | Qwen3.5-9B through the bundled vLLM service. This is the current reference profile. | Granite embedding and the ModernBERT reranker on GPU, inside Exculpata. | faster-whisper large-v3, float16. | This matches the bundled generator and is the reference deployment shape. This is a suggestion, not a measured quality result. |
| `apple-16` | Apple Silicon, 16 GB unified memory and up | Qwen3.5-4B or Qwen3.5-9B through Ollama on Metal. MLX is not part of this version. | Granite embedding and the ModernBERT reranker on CPU, inside Exculpata. | faster-whisper on CPU, int8. whisper.cpp is not part of this version. | Unified memory is shared with the rest of the machine. A 4B model leaves more room than 9B. This is a suggestion, not a measured quality result. |
| `apple-32` | Apple Silicon, 32 GB unified memory and up | Qwen3.5-9B through Ollama on Metal. MLX is not part of this version. | Granite embedding and the ModernBERT reranker on CPU, inside Exculpata. | faster-whisper on CPU, int8. whisper.cpp is not part of this version. | The model budget reserves 25 percent of unified memory for the operating system and application work. This is a suggestion, not a measured quality result. |
| `ultra` | Two or more 48 GB GPUs | Qwen3.5-9B through the bundled local vLLM service, with capacity for evaluated larger local profiles when available. | Granite embedding and the ModernBERT reranker on GPU, inside Exculpata. | faster-whisper large-v3, float16. | Multiple data-center GPUs provide additional local capacity, but no larger profile has a tier evaluation yet. This is a suggestion, not a measured quality result. |
<!-- model-profile-table:end -->

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
