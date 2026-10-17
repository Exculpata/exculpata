"""Product markers fail closed on malformed JSON values."""

from __future__ import annotations

import json

from case_intelligence.legacy_names import (
    CURRENT_PRODUCT,
    CURRENT_STEM,
    LEGACY_PRODUCT,
    LEGACY_STEM,
    accepts_product,
)
from tests.test_oss_installer import installer


def test_accepts_product_rejects_non_strings_without_raising():
    assert accepts_product(LEGACY_PRODUCT) is True
    assert accepts_product(CURRENT_PRODUCT) is True

    class Unhashable:
        __hash__ = None

    for value in (None, 1, [], {}, Unhashable()):
        assert accepts_product(value) is False


def test_storage_marker_with_array_product_fails_closed(tmp_path):
    for marker_name, storage_id in (
        (".exculpata-managed-storage.json", f"{CURRENT_STEM}-storage-synthetic"),
        (f".{LEGACY_STEM}-managed-storage.json", f"{LEGACY_STEM}-storage-synthetic"),
    ):
        storage = tmp_path / marker_name
        storage.mkdir()
        (storage / marker_name).write_text(json.dumps({
            "format_version": 1,
            "product": [],
            "storage_id": storage_id,
        }))
        assert installer._matter_storage_layout_valid(storage) is False


