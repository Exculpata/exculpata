"""Synthetic regressions for bounded TIFF preview rendering in discovery review."""
from __future__ import annotations

import hashlib
import io

from PIL import Image
import pytest

from case_intelligence.modules.discovery_review import tiff_preview
from case_intelligence.modules.discovery_review.tiff_preview import (
    MAX_PREVIEW_EDGE, TiffPreviewError, TiffPreviewStale, inspect_tiff, render_tiff_frame,
)
from tests.fixtures.load_files.generate_image_indexes import lfp_bytes
from tests.test_discovery_review_dat_opt import complete_listing, opened_review
from tests.test_discovery_review_image_load_files import upload_image_production


def _tiff_bytes(colors: tuple[tuple[int, int, int], ...], *, size: tuple[int, int] = (16, 12), mode: str = "RGB") -> bytes:
    frames = [Image.new(mode, size, color) for color in colors]
    buf = io.BytesIO()
    kwargs = {"save_all": True, "append_images": frames[1:]} if len(frames) > 1 else {}
    frames[0].save(buf, format="TIFF", **kwargs)
    return buf.getvalue()


def _write(path, raw: bytes) -> tuple[object, str]:
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def test_tiff_renderer_handles_single_multiframe_and_downscale(tmp_path):
    multi = _tiff_bytes(((200, 10, 10), (10, 200, 10), (10, 10, 200)), size=(32, 24))
    path, digest = _write(tmp_path / "multi.tif", multi)
    meta = inspect_tiff(path, digest)
    assert (meta.frame_count, meta.width, meta.height) == (3, 32, 24)
    (png1, total1), (png3, total3) = render_tiff_frame(path, digest, frame=1), render_tiff_frame(path, digest, frame=3)
    img1, img3 = Image.open(io.BytesIO(png1)), Image.open(io.BytesIO(png3))
    assert (total1, total3, img1.format, img3.format) == (3, 3, "PNG", "PNG")
    assert img1.getpixel((4, 4))[:3] == (200, 10, 10) and img3.getpixel((4, 4))[:3] == (10, 10, 200)
    assert path.read_bytes() == multi
    big_path, big_digest = _write(tmp_path / "wide.tif", _tiff_bytes(((40, 80, 120),), size=(MAX_PREVIEW_EDGE + 400, 200)))
    assert max(Image.open(io.BytesIO(render_tiff_frame(big_path, big_digest, frame=1)[0])).size) == MAX_PREVIEW_EDGE


def test_tiff_renderer_fails_closed_on_bounds_corrupt_and_stale(tmp_path, monkeypatch):
    path, digest = _write(tmp_path / "sample.tif", _tiff_bytes(((255, 0, 0), (0, 255, 0))))
    with pytest.raises(TiffPreviewStale):
        render_tiff_frame(path, "0" * 64, frame=1)
    for bad_frame in (0, 3):
        with pytest.raises(TiffPreviewError):
            render_tiff_frame(path, digest, frame=bad_frame)
    for name, raw_bytes in (("corrupt.tif", b"II*\x00not-a-real-tiff"),):
        with pytest.raises(TiffPreviewError):
            inspect_tiff(*_write(tmp_path / name, raw_bytes))
    png_buf = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(png_buf, format="PNG")
    with pytest.raises(TiffPreviewError):
        inspect_tiff(*_write(tmp_path / "disguised.tif", png_buf.getvalue()))
    buf16 = io.BytesIO()
    Image.new("I;16", (4, 4), 32768).save(buf16, format="TIFF")
    png16, frames16 = render_tiff_frame(*_write(tmp_path / "gray16.tif", buf16.getvalue()), frame=1)
    assert frames16 == 1 and Image.open(io.BytesIO(png16)).mode == "L"
    with pytest.raises(TiffPreviewError, match="frame chain"):
        tiff_preview._bounded_frame_count(type("_S", (), {"seek": lambda self, n: (_ for _ in ()).throw(SyntaxError("bad"))})())
    monkeypatch.setattr(tiff_preview, "MAX_TIFF_FRAMES", 2)
    with pytest.raises(TiffPreviewError, match="frame count"):
        inspect_tiff(*_write(tmp_path / "three.tif", _tiff_bytes(((10, 0, 0), (0, 10, 0), (0, 0, 10)))))
    monkeypatch.setattr(tiff_preview, "MAX_TIFF_BYTES", 10)
    with pytest.raises(TiffPreviewError, match="byte limit"):
        tiff_preview.read_verified_tiff_bytes(path, "0" * 64)
    monkeypatch.setattr(tiff_preview, "MAX_TIFF_BYTES", 32 * 1024 * 1024)
    monkeypatch.setattr(tiff_preview, "MAX_TIFF_PIXELS", 100)
    with pytest.raises(TiffPreviewError):
        render_tiff_frame(path, digest, frame=1)


def test_http_standalone_and_indexed_tiff_previews_and_security(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    single_tiff = _tiff_bytes(((180, 30, 30),))
    multi_tiff = _tiff_bytes(((20, 180, 20), (20, 20, 180)))
    with opened_review(tmp_path / "runtime") as review:
        opt_path, lfp_path = "Volume/DATA/pages.opt", "Volume/DATA/pages.lfp"
        single_path, multi_path = "Volume/IMAGES/single.tif", "Volume/IMAGES/multi.tif"
        upload_image_production(review, {
            opt_path: b"SYN0001,VOL,IMAGES\\single.tif,Y,,,1\n",
            lfp_path: lfp_bytes([{"key": "SYN0002", "volume": "VOL", "directory": "IMAGES",
                                  "filename": "multi.tif", "image_type": "4"}]),
            single_path: single_tiff,
            multi_path: multi_tiff,
        })
        single_token, multi_token, slug = review.token(single_path), review.token(multi_path), review.matter.slug

        # Standalone multi-frame TIFF renders frame 1 and offers frame 2 navigation.
        standalone = complete_listing(review, doc=multi_token, view="document")
        assert f"/matters/{slug}/production/tiff-preview/{multi_token}?frame=1" in standalone.text
        assert "TIFF frame 1 of 2" in standalone.text and "tiff_frame=2" in standalone.text

        # Applying a review code on frame 2 preserves tiff_frame=2 in form state and redirect.
        assert review.client.post(f"/matters/{slug}/production/codes", data={"label": "Key"}).status_code in {200, 303}
        frame2_page = complete_listing(review, doc=multi_token, view="document", tiff_frame="2")
        assert 'name="tiff_frame" value="2"' in frame2_page.text
        from case_intelligence.modules import ModuleDatabase
        from case_intelligence.modules.discovery_review.codes import CodeRepository
        code_id = CodeRepository(ModuleDatabase(review.bench.workspace.connection, review.bench.workspace._lock)).list(review.matter.matter_id)[0].code_id
        coded = review.client.post(
            f"/matters/{slug}/production/items/{multi_token}/codes/{code_id}",
            data={"action": "apply", "view": "document", "tiff_frame": "2"}, follow_redirects=False,
        )
        assert coded.status_code == 303 and "tiff_frame=2" in coded.headers["location"]

        frame1 = review.client.get(f"/matters/{slug}/production/tiff-preview/{multi_token}?frame=1")
        frame2 = review.client.get(f"/matters/{slug}/production/tiff-preview/{multi_token}?frame=2")
        assert frame1.status_code == 200 and frame1.headers["content-type"] == "image/png"
        assert frame1.headers["cache-control"] == "no-store" and frame1.headers["x-content-type-options"] == "nosniff"
        assert frame1.headers["x-tiff-frame"] == "1" and frame1.headers["x-tiff-frames"] == "2"
        assert Image.open(io.BytesIO(frame1.content)).getpixel((2, 2))[:3] == (20, 180, 20)
        assert Image.open(io.BytesIO(frame2.content)).getpixel((2, 2))[:3] == (20, 20, 180)
        assert review.client.get(f"/matters/{slug}/production/tiff-preview/{multi_token}?frame=9").status_code == 422

        # OPT single-frame and LFP multi-frame indexed TIFF records render previews inline.
        opt_view = complete_listing(review, doc=review.token(opt_path), view="document")
        assert f"/matters/{slug}/production/tiff-preview/{single_token}?frame=1" in opt_view.text
        assert "data-page-image-unavailable" not in opt_view.text
        lfp_view = complete_listing(review, doc=review.token(lfp_path), view="document")
        assert f"/matters/{slug}/production/tiff-preview/{multi_token}?frame=1" in lfp_view.text
        assert "TIFF frame 1 of 2" in lfp_view.text and "does not bind a specific TIFF frame" in lfp_view.text

        # Non-TIFF fails closed (415) and cross-matter fails closed (404).
        assert review.client.get(f"/matters/{slug}/production/tiff-preview/{review.token(opt_path)}").status_code == 415
        from tests.test_discovery_review_dat_opt import ACTOR
        other = review.bench.create_matter("second-matter", "Second Matter", ACTOR)
        assert review.client.get(f"/matters/{other.slug}/production/tiff-preview/{single_token}").status_code == 404

        # Rejected standalone TIFF shows explicit unavailable state and tampered bytes fail closed (409).
        store = review.bench.source_store(review.matter)
        store.source_path(review.document(single_path).document_id).write_bytes(single_tiff + b"\x00")
        assert review.client.get(f"/matters/{slug}/production/tiff-preview/{single_token}?frame=1").status_code == 409
        rejected = complete_listing(review, doc=single_token, view="document")
        assert "data-page-image-unavailable" in rejected.text and "TIFF preview unavailable" in rejected.text
