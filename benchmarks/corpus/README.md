# Synthetic discovery corpus

Version 1 contains four one-page PDFs (two native English, one image-only English,
one image-only Spanish), two single-voice synthetic WAV clips, seven top-k searches,
and five fact-and-citation answer cases. No real case material is used.
`v1/manifest.json` pins every media byte count and SHA-256, model revisions,
licenses, and fixture provenance. Every media file must be **strictly below
10,000,000 bytes**. Models are downloaded separately and never committed.
The gate also fixes the complete v1 case identities and model-record keys;
paired manifest/gold edits cannot silently remove or rename coverage, and
unexecuted model records cannot enter a passing receipt.

`v1/expected.json` is reviewed gold, separate from inference inputs. Normalization
uses Unicode NFKC, case folding, word characters, and collapsed whitespace.
Native PDFs require zero character error; image PDFs allow CER <= 0.03; every
PDF must retain its reviewed one-page count. Audio allows WER <= 0.15.
Insertions, deletions, and substitutions all count. Search requires the named
sources within top-k and their reviewed passage spans. Two full-only search cases require
the English and Spanish scanned sources and their validated citation support,
so accurate OCR alone cannot hide a missing searchable catalog entry. Each answer
requires its facts, the question route, persisted `kind == 'generated'`, explicit
answerability, all named source spans, and valid product citation resolution.
Persisted citation locators must match the resolved source unit before the
observer constructs normalized citation fields.
Persisted chunk IDs must also exactly match that unit. Search observations
retain all returned hits and citations; exceeding the requested top-k fails.
Every attached passage must support its complete extractive claim; document or
transcript status comes from the manifest, and observed labels must agree.
Missing, unrelated, or invalid source/text support fails without a text tolerance.
The audio parcel gold explicitly permits the equivalent written forms “locker
seven” and “Locker 7”; other numbers do not match. This is a reviewed span
alternative, not number substitution in recognition scoring or source validation.

Audio timing gold uses the original WAV durations (5,615 ms and 4,861 ms, rounded
to the nearest millisecond) as file bounds and explicit transcript-unit layouts.
Both clips allow a single passage whose reference end is the last source-signal
block: 5,400 ms for bicycle and 4,650 ms for parcel. These source-derived endpoints
use 10 ms PCM16 blocks with RMS at least 32 amplitude units, excluding trailing
file padding; a regression checks them directly against the pinned WAV bytes.
The parcel clip also retains the existing reviewed
two-unit layout: introduction at 0–2,040 ms and parcel sentence at 2,040–4,480 ms.
Each observed unit must match one complete reviewed layout in order, including
its text within the existing WER limit and both endpoints within 500 ms.
Every audio search/answer citation must match its actual projected unit and
interval. This rejects collapsed or shifted locators and misplaced passages;
it is a bounded layout regression check, not word-level alignment evaluation.
Fast runs check projection timing without claiming ASR accuracy. Timing gold
is never inferred from the run being graded.

```sh
make corpus-check
make corpus-history-check
make corpus-check-full
```

The fast lane ingests native PDFs and prepared transcript fixtures through the
application's source store, durable media-job/import/projection path, search,
the answer service, and citation validation. A separate HTTP probe uses the
real one-box dispatcher and Ask handler in synthetic test authentication mode,
requiring a durable answer job for each
question. It stops before job execution; answer completion is evaluated separately. It seeds the original WAV bytes without
acoustic preflight. It does not run OCR or ASR and cannot establish recognition
quality. Retrieval uses deterministic test adapters with lexical behavior;
answers use a deterministic extractive generator. Neither lane establishes
learned hybrid retrieval or generative-model quality. Those remain separate from
this corpus and from the diagnostics/scale work in issues #285 and #287.

The full lane additionally ingests the two image PDFs and the original WAVs,
using real OCR and local pinned ASR. This requires the Linux product dependencies
(`/usr/bin/pdftoppm`, Tesseract, ffprobe), exact English/Spanish and orientation OCR data hashes
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

`Synthetic discovery corpus` runs the fast application-contract gate on every PR.
Fast skips image-only OCR and imports prepared transcripts, so it cannot validate
recognition changes. Full runs nightly, on workflow dispatch, and on every PR
except changes limited to `docs/*.md`/`docs/*.rst` (including subdirectories) or
root `README.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, `LICENSE`, and `NOTICE`.
All product, service, runtime, configuration, dependency, script, test, workflow,
and unknown paths require full recognition. Rename classification checks both
old and new paths; an unavailable diff base also requires full recognition.
Actions are commit-pinned, permissions read-only,
and hosted jobs use only synthetic inputs. Existing quality/publication/security
checks remain unchanged. No model cache, workspace, database, or media artifacts
are uploaded.

## Deliberate updates

An optional `--report /tmp/corpus-receipt.json` records hashes of tracked and
nonignored untracked repository files before observation. Clean receipts name
the commit in `head`; edited trees set `head` to null and record `base_head`,
`dirty`, per-file hashes and their aggregate in `code_basis`. Generated output
paths are excluded and cannot overwrite tracked files. Repository changes during
observation block receipt creation. This identifies repository inputs, not
third-party dependencies or the complete runtime environment.

A normal run is read-only. To collect a proposal, explicitly choose a new file
outside the corpus:

```sh
PYTHONPATH=src .venv/bin/python scripts/corpus_check.py --full \
  --update-expected --proposal /tmp/corpus-proposal.json
```

This writes observed outputs, **not new gold**, and still returns failure for
mismatches. Review the text, page counts, source spans, timing layouts and tolerances, model/fixture identities,
and intended product change before manually editing `expected.json`. Include
that diff and the new passing run in the PR. Do not accept a broken product by
copying its output into gold. Updating model pins also requires the existing
model evaluation/license/offline-readiness review. Fixture modifications require
new hashes, byte counts, provenance and publication inspection.

## Historical failure proofs

`make corpus-history-check` overlays only the existing synthetic regression
probes onto temporary source archives. It requires current-head passes and
the expected marker in every assertion failure on `f656f13^`, `00922fc^`, and
`b0cae56^`.
The probes cover durable transcript corrections, missing text chunks in exact
search, and reviewer-language Ask routing. Import/setup errors, skips, timeouts,
wrong test populations, and unrelated failures cannot count as proof.
The accepted population pins each JUnit module and exact parameterized test name;
same-count substitutions and duplicate cases fail.
PostgreSQL transport remains replaced in the transcript probe. No shared runtime
is started. The default requires locally available history; `--current-only`
checks just the present tree. These probes complement the small media corpus.
