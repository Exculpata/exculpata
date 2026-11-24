# Bounded TIFF preview rendering in discovery review (Item 114 Slice 5a)

## Kickoff

- Goal restated: Render safe, browser-viewable PNG previews for admitted TIFF images in discovery review (both standalone TIFF items and OPT/LFP-indexed TIFF page records), with explicit frame navigation for multi-frame TIFFs and strict fail-closed resource and integrity limits so attorneys can read produced TIFF pages directly in the workspace without altering originals.
- Expected files: `specs/tiff-preview-rendering.md`, `src/case_intelligence/modules/discovery_review/tiff_preview.py`, `src/case_intelligence/modules/discovery_review/reader.py`, `src/case_intelligence/modules/discovery_review/production/service.py`, `src/case_intelligence/modules/discovery_review/presentation.py`, `src/case_intelligence/modules/discovery_review/routes.py`, `src/case_intelligence/templates/workbench_production_document_item.html`, `tests/snapshots/import_layers.json`, `tests/test_production_image_file_links.py`, `tests/test_tiff_preview.py`.
- Acceptance check: Synthetic HTTP and unit tests prove single-frame TIFFs, multi-frame TIFFs (explicit frame 1..N navigation with non-binding page-record notice), OPT-indexed and LFP-indexed TIFF page records render valid PNG previews; corrupt, disguised non-TIFF, oversized-pixel (decompression bomb), out-of-range frame, changed-original, and cross-matter requests fail closed; original TIFF bytes remain unchanged; `test_import_boundaries.py`, `test_production_image_file_links.py`, `test_tiff_preview.py`, and `publication-check.py` pass.
- Issue link: [Item 114 #503](https://github.com/Exculpata/exculpata/issues/503) and [Volume import #279](https://github.com/Exculpata/exculpata/issues/279).

## Problem and root cause

Discovery productions routinely deliver page images as single-frame or multi-frame TIFF files alongside OPT and LFP page indexes, but browsers cannot render `image/tiff` inline. Currently `EMBEDDED_MEDIA_TYPES` in `reader.py` and `ProductionService.record_window` exclude `image/tiff`, leaving `workbench_production_document_item.html` showing `TIFF preview unavailable`.

## Approach

1. Add `modules/discovery_review/tiff_preview.py` (registered in `modules.discovery_review` in `tests/snapshots/import_layers.json`) to inspect and render one explicit TIFF frame to PNG purely in memory from the guarded original file (`O_RDONLY | O_NOFOLLOW | O_CLOEXEC`), verifying SHA-256 digest and stat identity before and after read (`E1`).
2. Enforce fail-closed limits before pixel decoding: `MAX_TIFF_BYTES` (32 MiB), `MAX_TIFF_FRAMES` (500), `MAX_TIFF_PIXELS` (25,000,000 per inspected frame), `MAX_PREVIEW_EDGE` (2048 px downscale), and `MAX_PREVIEW_PNG_BYTES` (12 MiB). Reject non-TIFF formats and out-of-range frames. Handle 1-bit, grayscale, 16-bit grayscale (`I;16`), and RGB/RGBA TIFFs safely.
3. Add `GET /matters/{slug}/production/tiff-preview/{token}?frame=N` in `modules/discovery_review/routes.py` behind live matter authorization, membership check, and response lease, returning PNG with `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, and `Content-Security-Policy: default-src 'none'`.
4. Include `image/tiff` in `EMBEDDED_MEDIA_TYPES` and OPT/LFP page image selection, wire `_tiff_embed` in `presentation.py` with explicit frame navigation (`tiff_frame`), and update `workbench_production_document_item.html` so previewable TIFFs render inline while corrupt/oversized TIFFs still fall back to `TIFF preview unavailable`. Preserve the invariant that OPT/LFP page ordinals do not bind a specific TIFF frame.

## Vision check

Directly serves `VISION.md` and `docs/IDEAL.md` (`A1`, `E1`): reviewers inspect produced TIFF evidence inline inside the local workspace without modifying originals or trusting unverified image inputs.

## What it could break

- `tests/test_import_boundaries.py`: requires registering `tiff_preview` under `modules.discovery_review`.
- `tests/test_production_image_file_links.py`: previously asserted `data-page-image-unavailable` for an OPT-indexed TIFF; updated to assert inline TIFF preview rendering and valid PNG bytes.
- Existing JPEG/PNG/PDF embeds and DII image-name browsing must remain unchanged.

## Proving test

`tests/test_tiff_preview.py` and updated `tests/test_production_image_file_links.py` verify single-frame and multi-frame TIFF rendering, OPT/LFP TIFF page preview, frame stepping, corrupt/disguised/decompression-bomb/out-of-range refusals, changed-original `409`, and cross-matter `404` isolation.
