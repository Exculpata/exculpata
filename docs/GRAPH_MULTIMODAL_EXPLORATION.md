# Graph-assisted exploration and multimodal retrieval

Status: landing direction, September 19, 2026. Graph slice 20 is selected.
Slice 21 and further visual experiments remain on hold until slice 20 lands;
native visual retrieval requires separate model and source-locator qualification.

## Product outcome

Reviewers should find and follow evidence across documents, images, tables and
recordings, inspect each proposed connection, and preserve competing explanations.
Every retrieval result must resolve to the original source version and a precise
page, region, row or recording interval. Graph edges retain the evidence and the
human or machine origin that produced them.

## First delivery: explicit evidence neighborhoods

[Slice 20](product-slices/20-graph-exploration.md) makes existing identities,
events/assertions and source accounts navigable as a graph and accessible list.
It uses current provenance contracts and runs without a model. This first graph
exposes saved reviewer knowledge; it does not infer undocumented relationships.
The [feature contract and local evidence](GRAPH_EXPLORATION.md) describe the
implemented neighborhood, source navigation, bounds and acceptance limits.

## Following delivery: native visual retrieval

Native visual retrieval is future direction only. No visual adapter, model
qualification result or high-resolution experiment is part of slice 20. A later
slice must record a pinned candidate, visual adapter and locator contracts, and
required offline and synthetic qualification before changing the supported model
portfolio.

Start with document pages and images. Preserve OCR/text retrieval as an independent
lane, add native image embeddings, fuse ranked candidates, and rerank a bounded
set. Record which lane found each candidate. A rendered page is derived evidence:
its identity must include original source version, page, renderer revision and
render parameters. Model artifact, preprocessing, output dimension and index
generation belong in the index identity. A changed identity invalidates reuse.

The source application remains the authority for access and evidence. The model
worker receives only admitted local inputs, cannot choose filesystem paths or
grant itself matter scope, and returns candidate scores rather than source truth.
Derived visual vectors and thumbnails join matter deletion, backup and restore.
An optional worker is not a new mandatory dependency for ordinary review.

Evaluate pinned, license-recorded local candidates before changing the supported
portfolio. The evaluation must force offline loading and record actual model,
preprocessor, runtime, hardware and corpus identities. Synthetic controls must
include image-only needles, misleading OCR, tables whose layout changes meaning,
near-identical pages with different amounts, cross-matter distractors, and source
replacement. Compare text-only, image-only and fused recall and latency separately.
Mock transport tests establish interface behavior only, never visual recognition.

For recordings, later add bounded keyframe/scene retrieval with original time
intervals and independent transcript retrieval. Frame sampling must expose temporal
coverage gaps; an unobserved interval cannot support a negative finding.

## Scale and investigation follow-through

Complete exact retrieval remains a separate indexed-search requirement. Graph and
semantic top-k results cannot stand for every occurrence. Evolve durable processing
into versioned partitions with a global population and coverage ledger before
raising large-collection claims. Summaries can guide searches; originals remain the
basis for findings. Measure relevant-record recall, false connections, citation
resolution, reviewer effort, resources, restart and deletion on declared corpora.

## Research references

- [GraphRAG DRIFT](https://microsoft.github.io/graphrag/query/drift_search/):
  local exploration informed by broader collection context.
- [LazyGraphRAG research](https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost/):
  research direction for deferred summarization and budgeted exploration.
- [Qwen multimodal retrieval model card](https://huggingface.co/Qwen/Qwen3-VL-Embedding-2B):
  one candidate family to qualify, not a supported Exculpata portfolio change.

External benchmark claims do not establish Exculpata quality or capacity.
