from __future__ import annotations

import wave
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient

from case_intelligence.review_bench import (
    PACKAGE_ROOT, Citation, ExtractiveAnswerProvider, ReviewBench, create_app,
)
from case_intelligence.review_bench_v2 import Candidate


def test_broad_position_duties_are_bounded_multi_page_and_role_scoped():
    provider = ExtractiveAnswerProvider()
    evidence = (
        Citation("Invented roles.pdf", "Page 4", "Network Support Specialist. Maintains desk phones and replaces printer toner.", "/p4"),
        Citation("Invented roles.pdf", "Page 10", "Supervisory Records Systems Coordinator. Responsible for directing records-platform operations and setting service priorities. Network Support Specialist. Maintains desk printers and replaces toner supplies for staff workstations.", "/p10"),
        Citation("Invented roles.pdf", "Page 11", "Supervisory Records Systems Coordinator. Develops continuity plans and ensures required backups are tested. Coordinates security reviews with office leadership.", "/p11"),
        Citation("Invented roles.pdf", "Page 12", "Supervisory Records Systems Coordinator. Manages vendor support and oversees system upgrades. Provides technical guidance to staff about records systems and approved operating procedures.", "/p12"),
    )

    answer = provider.answer(
        "Describe the duties of the Supervisory Records Systems Coordinator", evidence
    )

    assert answer.text.startswith("The cited position description identifies these duties:")
    assert sum(line.startswith("• ") for line in answer.text.splitlines()) >= 5
    assert len(answer.text) < 1800
    assert {citation.location for citation in answer.citations} == {"Page 10", "Page 11", "Page 12"}
    assert "printer toner" not in answer.text
    assert "toner supplies" not in answer.text
    assert provider.answer(
        "Describe the duties of the Supervisory Orbital Archives Coordinator", evidence
    ).citations == ()


def test_position_duties_stop_at_next_newline_role_heading():
    provider = ExtractiveAnswerProvider()
    evidence = (
        Citation(
            "Invented roles.pdf",
            "Page 20",
            "Network Support Specialist\nMaintains reception printers and replaces toner.\n"
            "Supervisory Records Systems Coordinator\n"
            "Oversees records-platform access and coordinates continuity testing.\n"
            "Desktop Support Technician\nMaintains loaner laptops and peripheral inventory.",
            "/p20",
        ),
    )

    answer = provider.answer(
        "Describe the duties of the Supervisory Records Systems Coordinator", evidence
    )

    assert answer.citations == evidence
    assert "continuity testing" in answer.text
    assert "reception printers" not in answer.text
    assert "loaner laptops" not in answer.text


def test_ambiguous_shortened_role_title_abstains():
    excerpts = (
        "Records Systems Coordinator\n"
        "Oversees records-platform access and coordinates continuity testing for the office.\n"
        "Records Operations Coordinator\n"
        "Manages retention schedules and directs archival quality reviews for the office.",
        "Records Systems Coordinator. Oversees records-platform access and coordinates "
        "continuity testing for the office. Records Operations Coordinator. Manages retention "
        "schedules and directs archival quality reviews for the office.",
        "Records Systems Coordinator\n"
        "Oversees platform access and coordinates continuity testing for staff.\n"
        "Records Operations Coordinator. Manages retention schedules and directs archival "
        "quality reviews for staff.",
        "Records Systems Coordinator. Oversees platform access and coordinates continuity "
        "testing for staff.\nRecords Operations Coordinator\n"
        "Manages retention schedules and directs archival quality reviews for staff.",
    )
    for excerpt in excerpts:
        answer = ExtractiveAnswerProvider().answer(
            "Describe the duties of the Records Coordinator",
            (Citation("Invented roles.pdf", "Page 1", excerpt, "/p1"),),
        )

        assert answer.citations == ()
        assert "could not find support" in answer.text.casefold()


def test_ambiguous_shortened_role_across_pages_abstains():
    evidence = (
        Citation(
            "Invented roles.pdf", "Page 1",
            "Records Systems Coordinator\nOversees platform access and continuity testing.",
            "/p1",
        ),
        Citation(
            "Invented roles.pdf", "Page 2",
            "Records Operations Coordinator\nManages retention schedules and archival reviews.",
            "/p2",
        ),
    )

    answer = ExtractiveAnswerProvider().answer(
        "Describe the duties of the Records Coordinator", evidence
    )

    assert answer.citations == ()
    assert "could not find support" in answer.text.casefold()


def test_exact_role_title_wins_over_matching_longer_title():
    excerpts = (
        "Records Coordinator\nCoordinates office record schedules and approved retention work.\n"
        "Records Systems Coordinator. Maintains the records platform and account permissions.",
        "Records Coordinator. Coordinates office record schedules and approved retention work.\n"
        "Records Systems Coordinator\nMaintains the records platform and account permissions.",
    )
    for excerpt in excerpts:
        answer = ExtractiveAnswerProvider().answer(
            "Describe the duties of the Records Coordinator",
            (Citation("Invented roles.pdf", "Page 1", excerpt, "/p1"),),
        )

        assert answer.citations
        assert "retention work" in answer.text
        assert "account permissions" not in answer.text


def test_duty_summary_caps_multiple_citations_from_one_page_at_two_items():
    evidence = (
        Citation(
            "Invented roles.pdf",
            "Page 7",
            "Records Coordinator. Manages retention schedule alpha and coordinates approved "
            "archive checks for alpha division. Oversees records training alpha and directs "
            "quality reviews for alpha division.",
            "/p7?chunk=1",
        ),
        Citation(
            "Invented roles.pdf",
            "Page 7",
            "Records Coordinator. Manages retention schedule beta and coordinates approved "
            "archive checks for beta division. Oversees records training beta and directs "
            "quality reviews for beta division.",
            "/p7?chunk=2",
        ),
    )

    answer = ExtractiveAnswerProvider().answer(
        "Describe the duties of the Records Coordinator", evidence
    )
    bullets = [line for line in answer.text.splitlines() if line.startswith("• ")]

    assert len(bullets) == 2
    assert {citation.location for citation in answer.citations} == {"Page 7"}


def test_review_bench_ask_preserves_line_boundaries_for_role_sections(tmp_path):
    bench = ReviewBench(tmp_path / "runtime")

    class RolePageRetriever:
        def search(self, matter_id, query, limit):
            return (
                Candidate(
                    matter_id,
                    "invented-roles",
                    "page-20",
                    "Invented roles.pdf",
                    20,
                    "Supervisory Records Systems Coordinator\n"
                    "Oversees records-platform access and coordinates continuity testing.\n"
                    "Desktop Support Technician\n"
                    "Maintains loaner laptops and peripheral inventory.",
                ),
            )

    cast(Any, bench).hybrid = RolePageRetriever()
    try:
        evidence = bench.search(
            "alpha", "Describe the duties of the Supervisory Records Systems Coordinator"
        )
        assert "\n" in evidence[0].excerpt
        answer = bench.ask(
            "alpha", "Describe the duties of the Supervisory Records Systems Coordinator"
        )
        assert "continuity testing" in answer.text
        assert "loaner laptops" not in answer.text
    finally:
        bench.close()


def test_workspace_health_source_states_and_staff_projection(tmp_path):
    app = create_app(tmp_path / "runtime")
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert {key: health[key] for key in ("status", "product", "data")} == {
            "status": "ok",
            "product": "Case Review Bench",
            "data": "local pilot",
        }
        assert health["capabilities"]["source_pages"] == "ready"
        response = client.get("/matters/alpha")
        assert response.status_code == 200
        body = response.text
        assert "North Entrance Review" in body
        assert "Ready and searchable" in body
        assert "Transcript ready" in body
        assert "Synchronized transcript" in body
        assert "<audio" in body
        for forbidden in (
            "matter-alpha",
            "source_version_id",
            "segment-",
            "sha256",
            "synthetic_root",
            "/home/",
            "rrf",
            "processor_version",
            "model_id",
        ):
            assert forbidden not in body


def test_matter_search_filters_documents_and_transcript_citations(tmp_path):
    bench = ReviewBench(tmp_path / "runtime")
    try:
        alpha = bench.search("alpha", "shared red bicycle")
        bravo = bench.search("bravo", "shared red bicycle")
        assert {item.source_name for item in alpha} == {"report.txt", "Recorded interview"}
        assert any("north entrance" in item.excerpt for item in alpha)
        assert all("south entrance" not in item.excerpt for item in alpha)
        assert any("south entrance" in item.excerpt for item in bravo)
        assert all("north entrance" not in item.excerpt for item in bravo)
        assert any(item.href.startswith("/matters/alpha/sources/report") for item in alpha)
        assert any(item.media_start == 2.0 and "#transcript-2" in item.href for item in alpha)
    finally:
        bench.close()


def test_grounded_answer_contains_only_retrieved_synthetic_records(tmp_path):
    bench = ReviewBench(tmp_path / "runtime")
    try:
        evidence = bench.search("alpha", "Where was the shared red bicycle logged?")
        answer = bench.ask("alpha", "Where was the shared red bicycle logged?")
        assert answer.citations
        assert answer.citations == evidence[:3]
        assert "north entrance" in answer.text
        assert "south entrance" not in answer.text
        assert answer.text == " ".join(
            {item.excerpt.strip(): None for item in answer.citations}
        )
        assert all(asdict(item)["href"].startswith("/matters/alpha") for item in answer.citations)
    finally:
        bench.close()


def test_search_answer_and_source_routes_render_clickable_support(tmp_path):
    app = create_app(tmp_path / "runtime")
    with TestClient(app) as client:
        search = client.get("/matters/alpha", params={"q": "shared red bicycle"})
        assert search.status_code == 200
        assert "Play this moment" in search.text
        assert "Open excerpt" in search.text
        assert 'data-seek="2.0"' in search.text

        answer = client.get(
            "/matters/alpha",
            params={"question": "Where was the shared red bicycle logged?"},
        )
        assert answer.status_code == 200
        assert "Grounded in this matter" in answer.text
        assert "/matters/alpha/sources/report?line=" in answer.text
        assert "#transcript-2" in answer.text

        source = client.get("/matters/alpha/sources/report", params={"line": 2})
        assert source.status_code == 200
        assert "Source excerpt" in source.text
        assert "CITRINE-FALCON-731" not in source.text
        assert "shared red bicycle was logged at the north entrance" in source.text
        assert "document-line focused" in source.text


def test_media_cues_are_bounded_serializable_and_audio_is_playable_pcm(tmp_path):
    app = create_app(tmp_path / "runtime")
    with TestClient(app) as client:
        payload = client.get("/api/matters/alpha/transcript").json()
        assert payload["matter"] == "alpha"
        assert [cue["start"] for cue in payload["cues"]] == [0.0, 2.0, 4.0, 6.0]
        assert all(cue["start"] < cue["end"] <= 8 for cue in payload["cues"])
        assert "south entrance" not in repr(payload)

    audio_path = PACKAGE_ROOT / "static/demo-audio.wav"
    with wave.open(str(audio_path), "rb") as audio:
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        assert audio.getframerate() == 16_000
        assert audio.getnframes() == 128_000


def test_javascript_seek_contract_sets_current_time_and_exposes_test_hook():
    script = (PACKAGE_ROOT / "static/review-bench.js").read_text(encoding="utf-8")
    assert "media.currentTime = value" in script
    assert "window.caseReviewBench" in script
    assert 'cue.addEventListener("click"' in script
