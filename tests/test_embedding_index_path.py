"""Keep the embedding record path stable across naming generations."""
from pathlib import Path

import pytest

from case_intelligence.embedding_index import embedding_index_path
from case_intelligence.legacy_names import CURRENT_STEM, LEGACY_STEM, name_value


@pytest.mark.parametrize("generation", [LEGACY_STEM, CURRENT_STEM])
def test_embedding_record_contract_retains_the_complete_path(generation):
    assert name_value("embedding_index_record", generation) == "/var/lib/recordbench/state/embedding-index.json"


@pytest.mark.parametrize("parent_exists", [False, True])
def test_embedding_record_default_requires_existing_parent(monkeypatch, parent_exists):
    monkeypatch.delenv("CASE_INTELLIGENCE_EMBEDDING_INDEX_RECORD", raising=False)
    checked = []

    def is_dir(path):
        checked.append(path)
        return parent_exists

    monkeypatch.setattr(Path, "is_dir", is_dir)
    expected = Path("/var/lib/recordbench/state/embedding-index.json")
    assert embedding_index_path() == (expected if parent_exists else None)
    assert checked == [expected.parent]


def test_embedding_record_override_precedes_default_parent_check(monkeypatch, tmp_path):
    expected = tmp_path / "synthetic-embedding-index.json"
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_INDEX_RECORD", f"  {expected}  ")

    def unexpected_parent_check(path):
        pytest.fail("An explicit embedding record path must not check the default parent")

    monkeypatch.setattr(Path, "is_dir", unexpected_parent_check)
    assert embedding_index_path() == expected
