# Synthetic discovery corpus

Version 1 contains four one-page PDFs (two native English, one image-only English,
one image-only Spanish), two single-voice synthetic WAV clips, five top-k searches,
and five fact-and-citation answer cases. No real case material is used.
`v1/manifest.json` pins every media byte count and SHA-256, model revisions,
licenses, and fixture provenance. Every media file must be **strictly below
10,000,000 bytes**. Models are downloaded separately and never committed.

`v1/expected.json` is reviewed gold, separate from inference inputs. Normalization
uses Unicode NFKC, case folding, word characters, and collapsed whitespace.
Native PDFs require zero character error; image PDFs allow CER <= 0.03; audio
allows WER <= 0.15. Insertions, deletions, and substitutions all count. Search
requires the named sources within top-k. Each answer requires its facts, the
question route, all named source spans, and valid product citation resolution.
Missing, unrelated, or invalid citations fail without a citation tolerance.

```sh
make corpus-check
make corpus-history-check
make corpus-check-full
```

The fast lane ingests native PDFs and prepared transcript fixtures through the
application's source store, durable media-job/import/projection path, search,
the answer service, and citation validation. A separate HTTP probe uses the
real one-box dispatcher and Ask handler, requiring a durable answer job for each
question. It stops before job execution; answer completion is evaluated separately. It seeds the original WAV bytes without
acoustic preflight. It does not run OCR or ASR and cannot establish recognition
quality. Retrieval uses deterministic test adapters with lexical behavior;
answers use a deterministic extractive generator. Neither lane establishes
learned hybrid retrieval or generative-model quality. Those remain separate from
this corpus and from the diagnostics/scale work in issues #285 and #287.

The full lane additionally ingests the two image PDFs and the original WAVs,
using real OCR and local pinned ASR. This requires the Linux product dependencies
(`/usr/bin/pdftoppm`, Tesseract, ffprobe), exact English/Spanish OCR data hashes
from the manifest at `/usr/share/tesseract-ocr/5/tessdata`, and the existing
approved large-v3 snapshot. Install `faster-whisper==1.2.1`,
`ctranslate2==4.8.2`, `huggingface-hub==1.33.0`, and `av==18.1.0`, then explicitly stage:

```sh
python scripts/corpus_recognition.py --stage-models
HF_HUB_OFFLINE=1 make corpus-check-full
```

Staging uses the manifest's immutable revision; inference uses local files only.
Missing packages, models, OCR tools, or data fail rather than substituting
fixtures. CPU ASR is limited to two threads and one worker. The clips have one
synthetic voice each, so diarization is not evaluated. Full recognition can need
several GB of RAM/model storage and runs separately from ordinary PR checks.

`Synthetic discovery corpus` runs the fast gate on every PR, covering ingestion,
OCR, transcription, search, and answer changes without fragile path exclusions.
Full runs nightly, on workflow dispatch, and on PR changes to the corpus,
its runners, or its workflow. Actions are commit-pinned, permissions read-only,
and hosted jobs use only synthetic inputs. Existing quality/publication/security
checks remain unchanged. No model cache, workspace, database, or media artifacts
are uploaded.

## Deliberate updates

A normal run is read-only. To collect a proposal, explicitly choose a new file
outside the corpus:

```sh
PYTHONPATH=src .venv/bin/python scripts/corpus_check.py --full \
  --update-expected --proposal /tmp/corpus-proposal.json
```

This writes observed outputs, **not new gold**, and still returns failure for
mismatches. Review the text, source spans, tolerances, model/fixture identities,
and intended product change before manually editing `expected.json`. Include
that diff and the new passing run in the PR. Do not accept a broken product by
copying its output into gold. Updating model pins also requires the existing
model evaluation/license/offline-readiness review. Fixture modifications require
new hashes, byte counts, provenance and publication inspection.

## Historical failure proofs

`make corpus-history-check` overlays only the existing synthetic regression
probes onto temporary source archives. It requires current-head passes and
specific assertion failures on `f656f13^`, `00922fc^`, and `b0cae56^`.
The probes cover durable transcript corrections, missing text chunks in exact
search, and reviewer-language Ask routing. Import/setup errors, skips, timeouts,
wrong test populations, and unrelated failures cannot count as proof.
PostgreSQL transport remains replaced in the transcript probe. No shared runtime
is started. The default requires locally available history; `--current-only`
checks just the present tree. These probes complement the small media corpus.
