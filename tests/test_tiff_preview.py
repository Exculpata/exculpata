"""Synthetic regressions for bounded TIFF preview rendering in discovery review."""
from __future__ import annotations

import hashlib
import io

from PIL import Image
import pytest

from case_intelligence.modules.discovery_review.tiff_preview import (
    MAX_PREVIEW_EDGE, TiffPreviewError, TiffPreviewStale, inspect_tiff, render_tiff_frame,
)
from tests.fixtures.load_files.generate_image_indexes import lfp_bytes
from tests.test_discovery_review_dat_opt import complete_listing, opened_review
from tests.test_discovery_review_image_load_files import upload_image_production


def _tiff_bytes(colors: tuple[tuple[int, int, int], ...], *, size: tuple[int, int] = (16, 12), mode: str = "RGB") -> bytes:
    frames = [Image.new(mode, size, color) for color in colors]
    buf = io.BytesIO()
    if len(frames) == 1:
        frames[0].save(buf, format="TIFF")
    else:
        frames[0].save(buf, format="TIFF", save_all=True, append_images=frames[1:])
    return buf.getvalue()


def test_tiff_renderer_handles_single_multiframe_and_downscale(tmp_path):
    multi = _tiff_bytes(((200, 10, 10), (10, 200, 10), (10, 10, 200)), size=(32, 24))
    path = tmp_path / "multi.tif"
    path.write_bytes(multi)
    digest = hashlib.sha256(multi).hexdigest()

    meta = inspect_tiff(path, digest)
    assert meta.frame_count == 3 and meta.width == 32 and meta.height == 24

    png1, total1 = render_tiff_frame(path, digest, frame=1)
    png3, total3 = render_tiff_frame(path, digest, frame=3)
    assert total1 == 3 and total3 == 3
    img1 = Image.open(io.BytesIO(png1))
    img3 = Image.open(io.BytesIO(png3))
    assert img1.format == "PNG" and img3.format == "PNG"
    assert img1.getpixel((4, 4))[:3] == (200, 10, 10)
    assert img3.getpixel((4, 4))[:3] == (10, 10, 200)
    assert path.read_bytes() == multi

    large = _tiff_bytes(((40, 80, 120),), size=(MAX_PREVIEW_EDGE + 400, 200))
    big_path = tmp_path / "wide.tif"
    big_path.write_bytes(large)
    big_png, _ = render_tiff_frame(big_path, hashlib.sha256(large).hexdigest(), frame=1)
    resized = Image.open(io.BytesIO(big_png))
    assert max(resized.size) == MAX_PREVIEW_EDGE


def test_tiff_renderer_fails_closed_on_bounds_corrupt_and_stale(tmp_path, monkeypatch):
    raw = _tiff_bytes(((255, 0, 0), (0, 255, 0)))
    path = tmp_path / "sample.tif"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()

    with pytest.raises(TiffPreviewStale):
        render_tiff_frame(path, "0" * 64, frame=1)
    with pytest.raises(TiffPreviewError):
        render_tiff_frame(path, digest, frame=0)
    with pytest.raises(TiffPreviewError):
        render_tiff_frame(path, digest, frame=3)

    corrupt = tmp_path / "corrupt.tif"
    corrupt.write_bytes(b"II*\x00not-a-real-tiff")
    with pytest.raises(TiffPreviewError):
        inspect_tiff(corrupt, hashlib.sha256(corrupt.read_bytes()).hexdigest())

    png_disguised = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(png_disguised, format="PNG")
    disguised = tmp_path / "disguised.tif"
    disguised.write_bytes(png_disguised.getvalue())
    with pytest.raises(TiffPreviewError):
        inspect_tiff(disguised, hashlib.sha256(disguised.read_bytes()).hexdigest())

    from case_intelligence.modules.discovery_review import tiff_preview
    monkeypatch.setattr(tiff_preview, "MAX_TIFF_PIXELS", 100)
    with pytest.raises(TiffPreviewError):
        render_tiff_frame(path, digest, frame=1)


def test_http_standalone_and_indexed_tiff_previews_and_security(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    single_tiff = _tiff_bytes(((180, 30, 30),))
    multi_tiff = _tiff_bytes(((20, 180, 20), (20, 20, 180)))
    with opened_review(tmp_path / "runtime") as review:
        opt_path = "Volume/DATA/pages.opt"
        lfp_path = "Volume/DATA/pages.lfp"
        single_path = "Volume/IMAGES/single.tif"
        multi_path = "Volume/IMAGES/multi.tif"
        upload_image_production(review, {
            opt_path: b"SYN0001,VOL,IMAGES\\single.tif,Y,,,1\n",
            lfp_path: lfp_bytes([{"key": "SYN0002", "volume": "VOL", "directory": "IMAGES",
                                  "filename": "multi.tif", "image_type": "4"}]),
            single_path: single_tiff,
            multi_path: multi_tiff,
        })
        single_token = review.token(single_path)
        multi_token = review.token(multi_path)
        slug = review.matter.slug

        # Standalone multi-frame TIFF renders frame 1 and offers frame 2 navigation.
        standalone = complete_listing(review, doc=multi_token, view="document")
        assert f"/matters/{slug}/production/tiff-preview/{multi_token}?frame=1" in standalone.text
        assert "TIFF frame 1 of 2" in standalone.text
        assert "tiff_frame=2" in standalone.text

        frame1 = review.client.get(f"/matters/{slug}/production/tiff-preview/{multi_token}?frame=1")
        frame2 = review.client.get(f"/matters/{slug}/production/tiff-preview/{multi_token}?frame=2")
        assert frame1.status_code == 200 and frame1.headers["content-type"] == "image/png"
        assert frame1.headers["cache-control"] == "no-store"
        assert frame1.headers["x-content-type-options"] == "nosniff"
        assert frame1.headers["x-tiff-frame"] == "1" and frame1.headers["x-tiff-frames"] == "2"
        assert Image.open(io.BytesIO(frame1.content)).getpixel((2, 2))[:3] == (20, 180, 20)
        assert Image.open(io.BytesIO(frame2.content)).getpixel((2, 2))[:3] == (20, 20, 180)

        # Out-of-range frame fails closed.
        assert review.client.get(f"/matters/{slug}/production/tiff-preview/{multi_token}?frame=9").status_code == 422

        # OPT single-frame and LFP multi-frame indexed TIFF records render previews inline.
        opt_view = complete_listing(review, doc=review.token(opt_path), view="document")
        assert f"/matters/{slug}/production/tiff-preview/{single_token}?frame=1" in opt_view.text
        assert "data-page-image-unavailable" not in opt_view.text

        lfp_view = complete_listing(review, doc=review.token(lfp_path), view="document")
        assert f"/matters/{slug}/production/tiff-preview/{multi_token}?frame=1" in lfp_view.text
        assert "TIFF frame 1 of 2" in lfp_view.text
        assert "does not bind a specific TIFF frame" in lfp_view.text

        # Non-TIFF source fails closed with 415.
        assert review.client.get(f"/matters/{slug}/production/tiff-preview/{review.token(opt_path)}").status_code == 415

        # Cross-matter isolation fails closed with 404.
        from tests.test_discovery_review_dat_opt import ACTOR
        other = review.bench.create_matter("second-matter", "Second Matter", ACTOR)
        assert review.client.get(f"/matters/{other.slug}/production/tiff-preview/{single_token}").status_code == 404

        # Tampering with stored original bytes fails closed with 409 (E1).
        store = review.bench.source_store(review.matter)
        orig_path = store.source_path(review.document(single_path).document_id)
        orig_path.write_bytes(single_tiff + b"\x00")
        assert review.client.get(f"/matters/{slug}/production/tiff-preview/{single_token}?frame=1").status_code == 409
