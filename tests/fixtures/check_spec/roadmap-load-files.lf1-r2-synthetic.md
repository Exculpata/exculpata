# roadmap-load-files: Concordance DAT and Opticon OPT load files, import and export (#279)

STATUS: APPROVED Owner 4:43 AM ET 11/2 via Grok Bot relay, decision 19: "19 yes" (LF1, load files in parallel with email and PST). Drafted 11/1 from Owner's 10:06 AM ET direction via Grok Bot relay (load files, import and export, stay in Jan 1 scope). No lane starts before his yes.
Wave 1 spec o. Four PRs (LF1 to LF4); this spec details LF1 and outlines LF2 to LF4, each to get its own spec before its lane starts. LF1 size: about 450 lines. Lane: Codex cloud (synthetic fixtures only). Docs/test-only rule: does not qualify (new product code); batched merge card. Review: claude runner (Bates and intake).
Depends on: nothing for LF1. LF2 after spec f (email children design) merges. Order: runs in parallel with the email and PST work, not after it (approved item 3 put load files last; decision 19 asks to change that).
Targets (estimates): LF1 merged by Nov 21; LF2 Dec 5; LF3 Dec 12 (import v1 lands at M3); LF4 Dec 23 (export lands at M4).

## Goal
Bring a produced volume in exactly as produced: documents, pages, Bates numbers, text, natives and families from a Concordance DAT plus an Opticon OPT, with a dry-run QC report before anything is written. Later, export a selected set back out as DAT plus OPT. LF1 is a pure parser and QC library with no wiring.

## Prior work to reuse
- Issue #279 (open, no comments): full requirements, field list, QC list and the later formats.
- `src/case_intelligence/production_summary.py` (`parse_load_file`: today's 3-column CSV `path,begin_bates,end_bates`, per Owner 10/26 "CSV load file is the Bates source now, DAT and OPT later"), `bates_gaps.py` (`BatesDocument`, `compute_bates_gaps`), page Bates locators in `isolation.py`.
- Tests and fixtures: `tests/test_production_summary*.py`, `tests/test_bates_gaps*.py`, `tests/fixtures/production_summary/`.
- Intake for LF2: `intake_receipts.py`, `pilot_uploads.py`. Export for LF4: `work_product_exports.py`, `full_review_exports.py` (golden-bytes pattern).
- The MIT `frisk55frisk/loadfile-tool` may be read as a reference for edge cases. The parser is written in-house; no code copied.

## Files (fenced, LF1)
- New `src/case_intelligence/source_load_files.py` (ADR family `source_*`, core.sources).
- New `tests/test_source_load_files.py`.
- New `tests/fixtures/load_files/SYNTH001/` (about 50 docs, about 180 pages: DAT, OPT, TEXT, a few natives, one family) and `tests/fixtures/load_files/SYNTH002/` (a second volume plus one overlay DAT, for QC cases only). Tiny files, all invented.
- New `docs/LOAD_FILES.md`.

## What LF1 must do
1. DAT: field delimiter ASCII 20, quote ASCII 254 (the thorn), in-field newline ASCII 174; auto-detect cp1252, UTF-8 (with or without BOM) and UTF-16 LE/BE; header row names kept exactly. Report, never guess, on a mixed or broken encoding.
2. OPT: one row per page (image key, volume, path, document break, box, folder, page count); pages and Bates kept exactly as produced, including prefixes, padding and suffixes.
3. Join: DAT rows to OPT pages by begin Bates; TEXT and NATIVE path fields resolved inside the volume only (no `..`, no absolute paths, no symlinks out).
4. Families from BEGATTACH and ENDATTACH (or the mapped equivalents).
5. Field mapping: a proposed map from header names to our fields, plus a saved-profile format per producer (data only, no storage wiring in LF1).
6. Dry-run QC report, read-only: document and page counts; keys found in only one file; missing and orphan files; field-count mismatches; mixed encodings; Bates gaps, duplicates and overlaps (range logic in place; do not import `bates_gaps.py`, pin parity against `compute_bates_gaps` in tests); orphan attachments; unparseable dates; hash mismatches where a hash field exists. Every finding cites file and line.
7. Limits: streaming parse, bounded memory on a 1M-row DAT (measured on a generated file in the test, not committed), clear errors with file and line.

## Outline of later PRs (each gets its own spec)
- LF2 intake wiring: QC report then confirm; rows become sources with OPT Bates per page, TEXT as extracted text, natives as source files, families as family IDs (spec f's model), intake receipts and hashes; the existing CSV path keeps working.
- LF3 UI: pick a volume, auto-detect, preview 20 rows, field mapping with saved producer profiles, dry-run QC report, confirm.
- LF4 export (module: deliverables): DAT plus OPT (and TEXT) for a selected set, golden-bytes snapshot test first, Bates kept as produced.
- After Jan 1 (estimate): multi-volume overlays, IPRO LFP, EDRM XML 1.2, DII. Anything vendor-platform-specific stays deferred.

## Acceptance (red first)
1. Tests written first and failing: SYNTH001 parses to the exact expected documents, pages, Bates ranges, text paths and one family; all three encodings give identical results.
2. Each QC check in item 6 has at least one failing fixture case (mostly in SYNTH002) and the report names file and line.
3. Hostile cases: path escape attempts, a delimiter inside a quoted field, an embedded ASCII 174 newline, a truncated last row, a 10x field-count row, a BOM in the middle; each gives a clear error, never a crash or a silent skip.
4. No new imports from `workbench.py` or `workspace_store.py`; import-boundary ratchet green.
5. Full checks green at head. PR size is not a blocker.

## Rules (all specs)
- Base: latest main (9dda11b or later). One draft PR on its own `codex/` branch. PR size is not a blocker. Spec first, then code.
- No compose.yaml changes and no product behavior changes unless this spec says so. If one is strictly needed, stop and report it in PR.md.
- No branch-protection, ruleset or required-check changes. No new separate check that can show a red X on PRs.
- Pin any new action by full commit SHA.
- pytest `-n 4` or lower.
- Never push main. No force-push, no `--no-verify`. Draft stays draft; merge only on Owner's fresh yes at the final SHA, or under the standing docs/test-only rule where the header says it qualifies (only docs or test files, every check green at head, under 300 changed lines, private-terms check clean).
- Touch only the fenced files above. If another file must change, stop and report it in PR.md.
- Synthetic data only. Public text (code, comments, commits, PR body, issues) stays neutral: no work-host, network or employer details, and nothing that names a workplace or the kind of office that uses this (the dispatcher checks a private deny list before anything goes public). Say "local-first by default" and "measured recall"; no outcome promises.
- No models from Chinese-origin developers, in code, docs, examples or defaults. Do not name a default model; Owner picks models after hands-on testing.
- Codex self-review only, unless the PR touches Bates, export, privilege or guardrail code (then claude runner reviews).
