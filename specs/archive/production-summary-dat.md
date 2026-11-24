# Concordance DAT support in Production Summary and Bates-gap audit

## Kickoff

- Goal restated: Allow reviewers to select a Concordance `.dat` load file (in addition to 3-column CSV) in Production Summary so real discovery productions get lossless Bates-gap detection, unreadable-file reporting, and cited Markdown/JSON exports using the shared validated DAT parser.
- Expected files: `specs/production-summary-dat.md`, `src/case_intelligence/production_summary.py`, `src/case_intelligence/production_summary_routes.py`, `src/case_intelligence/templates/workbench_production_summary.html`, `tests/test_production_summary_dat.py`.
- Acceptance check: Synthetic unit and HTTP tests prove UTF-8 and cp1252 Concordance `.dat` productions (with `BEGBATES`/`ENDBATES`/`NATIVELINK` and Windows backslashes) appear in load-file choices, resolve documents safely within the same production root, compute exact `compute_bates_gaps` missing/possible gaps, export Markdown and JSON with source citations, fail closed on malformed/rejected DAT records, path traversal, ambiguous matches, or changed-file digests, and leave all existing CSV oracle and golden tests unchanged.
- Issue link: [Volume import #279](https://github.com/Exculpata/exculpata/issues/279), [Production summary #273](https://github.com/Exculpata/exculpata/issues/273), [Item 114 #503](https://github.com/Exculpata/exculpata/issues/503).

## Problem and root cause

`src/case_intelligence/production_summary.py` currently accepts only `.csv` files with exact columns `path,begin_bates,end_bates` and exact `relative_path` matching. Real discovery productions arrive with Concordance `.dat` load files (ASCII 20/254/174 delimiters, UTF-8/UTF-16/cp1252, producer headers like `BEGDOC`/`ENDBATES`/`NATIVELINK`, and Windows backslash paths relative to the production root), leaving teams unable to run the existing Bates-gap engine directly on uploaded `.dat` productions.

## Approach

1. Reuse `case_intelligence.source_load_files.parse_dat` (`A3` single parser, allowed `modules.deliverables -> core.sources` import) inside `production_summary.py` to parse `.dat` load files with bounded records, encoding detection, and fail-closed rejection on any `file_failure`, invalid header, or rejected record.
2. Map DAT columns to `(path, begin_bates, end_bates)` via optional explicit `field_map` or unambiguous case-insensitive standard aliases (`BEGDOC`/`BEGBATES`/`PRODBEG`, `ENDDOC`/`ENDBATES`/`PRODEND`, `NATIVE`/`NATIVEPATH`/`NATIVELINK`/`PATH`/`TEXT`/`TEXTPATH`). Ambiguous or missing required headers fail closed with `ValueError`.
3. Resolve DAT paths conservatively within the DAT's production root (normalizing `\` to `/`, rejecting absolute paths, drive letters, `..`, and control characters, and requiring a unique admitted match). Keep `.csv` parsing and exact path matching 100% unchanged.
4. Preserve before-and-after SHA-256 verification (`E1`), update `production_summary_routes.py` and `workbench_production_summary.html` wording, and feed mapped documents into the existing `compute_bates_gaps` engine.

## Vision check

Advances `VISION.md` and `docs/IDEAL.md` (`A3`, `E1`, `E2`): reuses the shared DAT parser and lossless Bates-gap engine so attorneys get honest, source-cited gap coverage on real discovery productions without inventing a parallel parser or fabricating Bates numbers.

## What it could break

- Existing CSV golden-file (`tests/test_production_summary.py`), HTTP route (`tests/test_production_summary_routes.py`), and 500-seed oracle (`tests/test_production_summary_oracle.py`) tests must remain byte-for-byte identical.
- `tests/test_import_boundaries.py`: `production_summary` (`modules.deliverables`) importing `source_load_files` (`core.sources`) is an allowed `modules.* -> core.*` edge.

## Proving test

`tests/test_production_summary_dat.py` uploads synthetic UTF-8 and cp1252 Concordance `.dat` productions over HTTP, verifies `choices`, exact missing/possible Bates gaps, Markdown/JSON exports, path-traversal and malformed-DAT refusal, and changed-file `409` handling.
