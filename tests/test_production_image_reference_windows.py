"""Read-only navigation of images actually declared by a production index."""

import os

import pytest

from case_intelligence.modules.discovery_review.production import ProductionStale
from tests.fixtures.load_files.generate_image_indexes import dii_bytes
from tests.test_discovery_review_dat_opt import import_paths, opened_review, upload_production


def test_dii_image_windows_verify_the_complete_source_and_selected_block(tmp_path, monkeypatch):
    first = {"record_id": "first", "begin_bates": "A1", "directory": "First", "images": ("other.png",)}
    names = [f"frame-{number}.png" for number in range(13)]
    selected = {"record_id": "second", "begin_bates": "A2", "directory": "Images",
                "images": names, "page_count": "19"}
    path = "Volume/metadata.dii"
    data = dii_bytes([first, selected])
    with opened_review(tmp_path / "runtime") as review:
        upload_production(review, {path: data})
        attempt = import_paths(review, [path])[path]
        document = review.document(path)

        def inspect(**query):
            return review.service.record_window(review.matter.matter_id, attempt, category="document",
                document_id=document.document_id, ordinal=2, **query)["current"]

        from case_intelligence.modules.discovery_review.production import image_references
        with monkeypatch.context() as guard:
            guard.setattr(image_references, "parse_dii", lambda *args, **kwargs: pytest.fail("Unrequested DII scan"))
            assert inspect().get("image_files") is None
        window = inspect(image_start=1)["image_files"]
        assert window["start"] == 1 and window["total"] == 13 and window["previous_start"] is None
        tail = inspect(image_start=window["next_start"])["image_files"]
        assert tail["next_start"] is None and tail["previous_start"] == 1
        rows = [*window["rows"], *tail["rows"]]
        assert [row["ordinal"] for row in rows] == list(range(1, 14))
        assert [row["path"] for row in rows] == ["Images/" + name for name in names]
        assert all(row["status"] == "unavailable" and row["source"] is None for row in rows)
        # Verify that agreement on a block ordinal alone never authorizes names.
        current = inspect()
        from case_intelligence.modules.discovery_review.production.service import source_fingerprint
        original = review.store.source_path(document.document_id)
        identity = source_fingerprint(original, document.digest)
        bad_count = [(key, "7" if key == "observed_image_entries" else value)
                     for key, value in current["optional_values"]]
        for change in ({"line": current["line"] - 1}, {"ordinal": 1}, {"optional_values": bad_count}):
            with pytest.raises(ProductionStale):
                image_references.dii_paths(original, {**current, **change}, 1, identity)
        with pytest.raises((KeyError, ValueError)):
            inspect(image_start=14)
        with pytest.raises(ValueError):
            inspect(image_start=True)
        with monkeypatch.context() as limit:
            limit.setattr(review.store, "document_file_limit", len(data) - 1)
            with pytest.raises(ProductionStale):
                inspect(image_start=1)
        metadata = original.stat()
        original.write_bytes(data.replace(b"frame-0.png", b"other-0.png"))  # fixture-write: Change generated retained bytes.
        os.utime(original, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))  # fixture-write: Keep generated size and mtime stable.
        with pytest.raises(ProductionStale):
            inspect(image_start=1)
