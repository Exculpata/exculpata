# Synthetic corpus generator v1

Eval-only, synthetic data, local-first by default. This fixture supports measured
recall and makes no claim about results on other material.

Run `python -m case_intelligence.synthetic_corpus_gold --out NEW_DIRECTORY --seed 7 --scale ci`.
Output must be new. No network, model or database is used for generation.
`ci`, `small`, `medium`, `large`, `xl` retain the research prototype's seeded
`random.Random` and added-noise counts of 0, 2,000, 50,000, 250,000 and 1,000,000.
`10k`, `100k`, `1m` instead target total transactions, including the planted core.
`--filler-chunks N` overrides added noise. Published corpora are immutable.

The fixed fraud core uses spec a's eight patterns and fictional namespace.
Amounts use decimal strings and XCU; the synthetic structuring threshold is
10,000. Dormant accounts open in January and first activate in April; neutral
noise uses separate operating accounts in February. Larger scales preserve the
core. Each noise item adds one CSV ledger, transaction and passage.

`sources.jsonl` records paths, hashes, versions and attachment parents;
`chunks.jsonl` records extracted text, byte spans (PDF spans refer to extracted
text), page locators and identity mentions. CSV row 2 resolves each transaction.
`gold.jsonl` streams typed section rows: every row carries answer-key version
1.0.0, corpus id, seed, scale, `kind` and `value`. Section names and value shapes
follow [the fraud design](design/fraud-case-and-answer-key.md). Reassemble arrays
by `kind`; no full noise array is retained during generation.

Real EML, nested attachments, MBOX, CSV, short native-text PDF and TXT artifacts
are accompanied by bounded extracted passage files. `suite.json` uses the v2 eval
contract: call `defense_eval.load_suite(output / 'suite.json', root=output)`.
It measures the fixed CI core, including competing near-duplicate mail and
contradictory times. It does not benchmark ingestion or retrieval over all noise.
The transcript question uses a synthetic text transcript, with no audio or ASR.
Per-lane diagnostics require the existing hybrid eval adapter; generation does
not initialize that adapter. Tests exercise it with synthetic in-memory lanes.

Spreadsheet and statement generation, scans, OCR and the 1M measurement remain
later work. No statement formats from spec h are implemented here.
