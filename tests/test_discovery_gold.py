from __future__ import annotations


from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
from collections import Counter, defaultdict

from case_intelligence.discovery_gold import (
    DEFAULT_DOCUMENT_COUNT,
    GOLD_SCHEMA_VERSION,
    GOLD_SUITE_ID,
    classification_cases,
    retrieval_cases,
    retrieval_documents,
    review_criteria,
    suite_fingerprint,
)


EXPECTED_FINGERPRINT = "6c098ca86a6b268b1b8894f5db42df2b22f1cdc083766eca3b283fd367ed4796"


def test_discovery_gold_is_versioned_unique_and_stable() -> None:
    documents = retrieval_documents()
    queries = retrieval_cases()
    classifications = classification_cases()
    criteria = review_criteria()
    assert GOLD_SUITE_ID == f"{LEGACY_STEM}-discovery-gold-v1"
    assert GOLD_SCHEMA_VERSION == 1
    assert len(documents) == DEFAULT_DOCUMENT_COUNT == 1_200
    assert len(queries) == 25
    assert len(criteria) == 8
    assert len(classifications) == 96
    assert len({item.document_id for item in documents}) == len(documents)
    assert len({item.case_id for item in queries}) == len(queries)
    assert len({item.case_id for item in classifications}) == len(classifications)
    assert suite_fingerprint() == EXPECTED_FINGERPRINT


def test_gold_queries_bind_expected_sources_and_answer_contracts() -> None:
    document_ids = {item.document_id for item in retrieval_documents()}
    for item in retrieval_cases():
        assert set(item.expected_documents).issubset(document_ids)
        if item.answer_sample and item.answerable:
            assert item.expected_documents
            assert item.required_concepts
        if not item.answerable:
            assert item.answer_sample
            assert not item.expected_documents
            assert not item.required_concepts


def test_classification_gold_is_balanced_within_every_criterion() -> None:
    criteria = {item.criterion_id for item in review_criteria()}
    grouped: dict[str, Counter[str]] = defaultdict(Counter)
    challenges: dict[str, set[str]] = defaultdict(set)
    for item in classification_cases():
        assert item.criterion_id in criteria
        assert item.expected in {"include", "not_identified"}
        assert item.text.startswith("This is a generated discovery-review fixture.")
        grouped[item.criterion_id][item.expected] += 1
        challenges[item.criterion_id].add(item.challenge)
    assert set(grouped) == criteria
    for criterion_id in criteria:
        assert grouped[criterion_id] == Counter({"not_identified": 8, "include": 4})
        assert len(challenges[criterion_id]) >= 8


def test_media_gold_uses_a_bounded_timestamp_location() -> None:
    media = [item for item in retrieval_documents() if item.source_name.endswith(".mp3")]
    assert len(media) == 1
    assert media[0].line_start == 342_000
    assert media[0].line_end == 366_000
