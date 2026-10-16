from __future__ import annotations


from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import copy
import json
import re
from pathlib import Path

import pytest

from benchmarks.review_acceptance.integrity import (
    PackIntegrityError,
    canonical_sha256,
    referenced_fixture_files,
    referenced_test_nodes,
    safe_repo_path,
    verify_pack_integrity,
)
from case_intelligence.generation import EvidenceItem, GroundedGenerationService
from case_intelligence.review_quality import research_synthesis_question


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "benchmarks/review-acceptance-v1.json"
EXPECTED_CATEGORIES = {
    "abstention",
    "broad_summary_scope",
    "browser_task_success",
    "citation_resolution",
    "cross_modal_completeness",
    "every_source_screening",
    "generator_objective_satisfaction",
    "ingestion_ocr",
    "lifecycle_authorization",
    "optional_overview_recovery",
    "research_conversational_follow_up",
    "retrieval_recall_rank",
    "sampled_frame_ocr_contract",
    "transcript_speaker_review",
}


def test_review_acceptance_pack_is_frozen_synthetic_and_executable():
    payload = json.loads(PACK.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["pack_id"] == f"{LEGACY_STEM}-review-acceptance-v1"
    assert payload["provenance"] == "synthetic"
    assert payload["confidential_data"] is False
    assert payload["requires_network"] is False
    assert payload["frozen"] is True
    assert payload["case_fingerprint"] == canonical_sha256(payload["cases"])
    assert verify_pack_integrity(ROOT, payload) == payload["integrity"][
        "content_fingerprint"
    ]
    assert {case["category"] for case in payload["cases"]} == EXPECTED_CATEGORIES
    assert len({case["case_id"] for case in payload["cases"]}) == len(payload["cases"])
    withheld_cases = [
        case for case in payload["cases"] if case.get("synthesis_source_withheld")
    ]
    assert len(withheld_cases) == 1

    for case in payload["cases"]:
        assert re.fullmatch(r"[a-z0-9-]+", case["case_id"])
        assert case["contract"] and case["node_ids"]
    assert referenced_test_nodes(payload)
    fixture_payloads = [
        json.loads(safe_repo_path(ROOT, path).read_text(encoding="utf-8"))
        for path in referenced_fixture_files(payload)
        if path.endswith(".json")
    ]

    serialized = json.dumps([payload, *fixture_payloads]).casefold()
    assert "/home/" not in serialized


def test_review_acceptance_pack_rejects_changed_test_and_fixture_digests():
    payload = json.loads(PACK.read_text(encoding="utf-8"))
    node_id = next(iter(payload["integrity"]["test_node_sha256"]))
    changed_node = copy.deepcopy(payload)
    changed_node["integrity"]["test_node_sha256"][node_id] = "0" * 64
    with pytest.raises(PackIntegrityError, match="test node changed"):
        verify_pack_integrity(ROOT, changed_node)

    fixture_path = next(iter(payload["integrity"]["fixture_file_sha256"]))
    changed_fixture = copy.deepcopy(payload)
    changed_fixture["integrity"]["fixture_file_sha256"][fixture_path] = "0" * 64
    with pytest.raises(PackIntegrityError, match="fixture changed"):
        verify_pack_integrity(ROOT, changed_fixture)


def test_withheld_source_scenario_excludes_prepared_synthesis():
    payload = json.loads(PACK.read_text(encoding="utf-8"))
    case = next(
        item for item in payload["cases"] if item.get("synthesis_source_withheld")
    )
    scenario = json.loads(
        safe_repo_path(ROOT, case["scenario_path"]).read_text(encoding="utf-8")
    )
    assert scenario["provenance"] == "synthetic"
    assert scenario["confidential_data"] is False
    sources = {item["source_id"]: item for item in scenario["sources"]}
    input_ids = tuple(scenario["input_source_ids"])
    withheld_ids = tuple(scenario["withheld_source_ids"])
    assert input_ids and withheld_ids
    assert set(input_ids).isdisjoint(withheld_ids)
    assert set(input_ids) | set(withheld_ids) == set(sources)
    assert all(sources[source_id]["role"] == "component" for source_id in input_ids)
    assert all(
        sources[source_id]["role"] == "prepared_synthesis"
        for source_id in withheld_ids
    )
    assert case["withheld_source_ids"] == list(withheld_ids)

    component_evidence = tuple(
        EvidenceItem(
            f"S{index}",
            sources[source_id]["source_name"],
            sources[source_id]["location"],
            sources[source_id]["text"],
            sources[source_id]["evidence_kind"],
        )
        for index, source_id in enumerate(input_ids, 1)
    )
    evidence_source_ids = {
        item.evidence_id: source_id
        for item, source_id in zip(component_evidence, input_ids, strict=True)
    }
    supplied: list[tuple[str, ...]] = []

    class ComponentExtractor:
        available = True

        def generate(self, **kwargs):
            evidence = kwargs["evidence"]
            supplied_source_ids = tuple(
                evidence_source_ids[item.evidence_id] for item in evidence
            )
            supplied.append(supplied_source_ids)
            assert set(supplied_source_ids) == set(input_ids)
            assert set(supplied_source_ids).isdisjoint(withheld_ids)
            supplied_text = "\n".join(
                f"{item.source_name}\n{item.excerpt}" for item in evidence
            )
            assert all(
                sources[source_id]["source_name"] not in supplied_text
                and sources[source_id]["text"] not in supplied_text
                for source_id in withheld_ids
            )
            identifier = re.search(r"\b[A-Z]+-\d+\b", kwargs["question"])
            assert identifier is not None
            selected = next(
                item
                for item in evidence
                if identifier.group(0) in item.excerpt
                and re.search(r"\b\d{2}:\d{2}:\d{2}\b", item.excerpt)
            )
            return {
                "answerable": True,
                "claims": [
                    {"text": selected.excerpt, "evidence_ids": [selected.evidence_id]}
                ],
                "limitation": None,
                "missing_information": "",
            }

    answer = GroundedGenerationService(ComponentExtractor()).answer(
        research_synthesis_question(scenario["objective"]), component_evidence
    )
    assert supplied
    assert scenario["expected"]["identifier"] in answer.text
    assert scenario["expected"]["clock_time"] in answer.text
    assert {
        evidence_source_ids[evidence_id] for evidence_id in answer.used_evidence_ids
    }.issubset(input_ids)
    assert all(
        sources[source_id]["text"] not in answer.text for source_id in withheld_ids
    )
