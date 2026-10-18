from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from case_intelligence.pilot_uploads import PilotDocument
from case_intelligence.workbench import CaseIntelligenceWorkbench, MatterRecord
from case_intelligence.workspace_store import NotebookReferenceRecord


def _transcript_fixture():
    matter = MatterRecord(
        "matter-legacy",
        "generated-legacy",
        "Generated legacy matter",
        "Synthetic fixture",
        "actor-legacy",
        "now",
        "now",
    )
    text = "The generated transcript records event EVT-4821."
    unit = {
        "number": 1,
        "text": text,
        "line_start": 0,
        "line_end": 2_000,
        "excerpt_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "start_ms": 0,
        "end_ms": 2_000,
        "speaker_cluster": "SPEAKER_00",
        "location_label": "00:00–00:02",
    }
    document = PilotDocument(
        "document-legacy",
        "Generated interview.wav",
        "generated-interview.wav",
        "audio/wav",
        128,
        "ready",
        "",
        [unit],
        version_id="version-legacy",
    )

    class Store:
        def ready_documents(self):
            return (document,)

        def get(self, document_id):
            if document_id != document.document_id:
                raise KeyError(document_id)
            return document

    history: dict[int, tuple[str, ...]] = {}
    workbench = object.__new__(CaseIntelligenceWorkbench)
    def source_store(_matter):
        return Store()

    workbench.source_store = source_store
    workbench.workspace = SimpleNamespace(
        transcript_support_history=lambda matter_id, document_id, version_id: (
            history
            if (
                matter_id,
                document_id,
                version_id,
            )
            == (matter.matter_id, document.document_id, document.version_id)
            else {}
        )
    )
    return workbench, matter, document, unit, history


def test_current_legacy_transcript_token_remains_valid_for_derived_work() -> None:
    workbench, matter, document, unit, _history = _transcript_fixture()
    candidate = workbench._candidate(matter, document, document.parsed_units()[0], 1)
    legacy_token = workbench._legacy_support_token(candidate)
    current = workbench._citation(matter, candidate)
    legacy = replace(
        current,
        support_token=legacy_token,
        href=(
            f"/matters/{matter.slug}?support={legacy_token}"
            "&play=1#support-pane"
        ),
    )

    reference = workbench.notebook_reference_from_support(matter, legacy_token)
    assert reference["support_token"] == legacy_token
    saved_reference = NotebookReferenceRecord(
        "reference-legacy",
        "item-legacy",
        matter.matter_id,
        1,
        document.document_id,
        document.version_id,
        document.display_name,
        candidate.citation,
        unit["number"],
        candidate.chunk_id,
        unit["excerpt_digest"],
        unit["text"],
        legacy_token,
        "now",
    )
    assert workbench.available_notebook_support_tokens(
        matter, (saved_reference,)
    ) == frozenset({legacy_token})

    resolved = workbench._current_workflow_citation(matter, legacy)
    assert resolved is not None
    assert resolved.support_token == legacy_token
    assert resolved.href == legacy.href

    frozen_source = SimpleNamespace(
        matter_id=matter.matter_id,
        document_id=document.document_id,
        version_id=document.version_id,
        display_name=document.display_name,
        media_type=document.media_type,
        source_state="ready",
    )
    frozen_job = SimpleNamespace(
        result={"evidence": [workbench._workflow_citation_payload(legacy)]}
    )
    workbench._assert_frozen_research_ledger(
        matter, frozen_job, (frozen_source,)
    )


def test_legacy_transcript_token_does_not_bypass_current_content_checks() -> None:
    workbench, matter, document, original, history = _transcript_fixture()
    candidate = workbench._candidate(matter, document, document.parsed_units()[0], 1)
    legacy_token = workbench._legacy_support_token(candidate)
    legacy = replace(
        workbench._citation(matter, candidate),
        support_token=legacy_token,
        href=(
            f"/matters/{matter.slug}?support={legacy_token}"
            "&play=1#support-pane"
        ),
    )

    with pytest.raises(KeyError):
        workbench.notebook_reference_from_support(matter, "f" * 40)
    with pytest.raises(KeyError):
        workbench.notebook_reference_from_support(
            replace(matter, matter_id="matter-other"), legacy_token
        )

    history[1] = (original["text"],)
    edited = "The reviewed transcript now records a different event."
    document.units = [
        {
            **original,
            "text": edited,
            "excerpt_digest": hashlib.sha256(edited.encode("utf-8")).hexdigest(),
        }
    ]
    assert workbench._find_support(matter, legacy_token)[2] == 0
    with pytest.raises(KeyError):
        workbench.notebook_reference_from_support(matter, legacy_token)
    assert workbench._current_workflow_citation(matter, legacy) is None
