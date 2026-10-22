"""Synthetic modality notices describe citation coverage without promising truth."""
from __future__ import annotations

from tests.fixture_write import fixture_write

import copy
import io
import json
from types import SimpleNamespace
import zipfile
from xml.etree import ElementTree

import pytest

from case_intelligence import answer_presentation
from case_intelligence.review_quality import modality_coverage
from tests.test_report_review_basis import ACTOR, saved_research, workspace

QUESTION = "Compare both the written report and what was said in the recording."
COMPLETE = "This answer cites written and spoken passages. Check each claim against the original sources."
PARTIAL = ("This result is partial: spoken evidence reached the answer packet, but "
    "no generated claim citing those passages was retained. Review the matching source "
    "or refine the question before treating the comparison as complete.")
UNAVAILABLE = ("This result is partial: no matching spoken evidence was retrieved in "
    "the selected passages. Review the generated result and search that source "
    "type directly before treating the comparison as complete.")
CASES = (
    ("complete", "This answer includes source-verified written and spoken support.", COMPLETE),
    ("partial", "This result is partial: spoken evidence reached the answer packet, but "
        "no source-verified spoken claim was retained. Review the matching source "
        "or refine the question before treating the comparison as complete.", PARTIAL),
    ("partial", "This result is partial: no matching spoken evidence was retrieved in "
        "the selected passages. Review the supported result and search that source "
        "type directly before treating the comparison as complete.", UNAVAILABLE),
    ("partial", "Synthetic custom coverage notice: source-verified is a quoted label.",
        "Synthetic custom coverage notice: source-verified is a quoted label."),
)


def coverage(mode, notice):
    return dict(mode=mode, requested_evidence_kinds=["written", "spoken"],
        available_evidence_kinds=["written", "spoken"],
        used_evidence_kinds=["written", "spoken"] if mode == "complete" else ["written"],
        missing_evidence_kinds=[] if mode == "complete" else ["spoken"], notice=notice)


def readable(body, format_name):
    if format_name == "docx":
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            return " ".join(ElementTree.fromstring(archive.read("word/document.xml")).itertext())
    return body.decode()


@pytest.mark.parametrize("available,used,expected", [
    (("S1", "S2"), ("S1", "S2"), COMPLETE),
    (("S1", "S2"), ("S1",), PARTIAL),
    (("S1",), ("S1",), UNAVAILABLE),
])
def test_new_modality_notice_describes_selection_and_preserves_metrics(available, used, expected):
    evidence = {key: SimpleNamespace(evidence_kind=kind) for key, kind in
        (("S1", "document"), ("S2", "transcript")) if key in available}
    result = modality_coverage(QUESTION, evidence, used)
    assert result["notice"] == expected
    assert result["mode"] == ("complete" if len(used) == 2 else "partial")
    assert result["requested_evidence_kinds"] == ["written", "spoken"]
    assert result["available_evidence_kinds"] == (["written", "spoken"] if len(available) == 2 else ["written"])
    assert result["used_evidence_kinds"] == (["written", "spoken"] if len(used) == 2 else ["written"])
    assert result["missing_evidence_kinds"] == ([] if len(used) == 2 else ["spoken"])


@pytest.mark.parametrize("names", ["written", "spoken", "written and spoken"])
def test_historical_notice_mapping_is_exact_for_every_generated_label(names):
    historical = f"This answer includes source-verified {names} support."
    expected = f"This answer cites {names} passages. Check each claim against the original sources."
    assert answer_presentation.modality_coverage_notice(historical) == expected
    partial = (f"This result is partial: {names} evidence reached the answer packet, but "
        f"no source-verified {names} claim was retained. Review the matching source "
        "or refine the question before treating the comparison as complete.")
    assert answer_presentation.modality_coverage_notice(partial) == (
        f"This result is partial: {names} evidence reached the answer packet, but "
        "no generated claim citing those passages was retained. Review the matching source "
        "or refine the question before treating the comparison as complete.")
    unavailable = (f"This result is partial: no matching {names} evidence was retrieved in "
        "the selected passages. Review the supported result and search that source "
        "type directly before treating the comparison as complete.")
    assert "Review the generated result" in answer_presentation.modality_coverage_notice(unavailable)
    for custom in ('The source quotes "' + historical + '".', historical + " Reviewer annotation.", " " + historical):
        assert answer_presentation.modality_coverage_notice(custom) == custom


@pytest.mark.parametrize("mode,historical,expected", CASES)
def test_saved_answer_views_exports_and_bundle_preserve_raw_payload(workspace, mode, historical, expected):
    client, bench, matter = workspace
    conversation = bench.workspace.get_conversation(matter.matter_id)
    payload = {"kind": "generated", "introduction": "Generated answer for source review:",
        "claims": [{"text": "The synthetic review needs source checking.", "citations": []}],
        "modality_coverage": coverage(mode, historical)}
    message = bench.workspace.append_message(matter.matter_id, conversation.conversation_id,
        "assistant", "Synthetic saved answer.", payload)
    raw = bench.workspace.connection.execute("SELECT payload_json FROM workbench_message WHERE message_id=?",
        (message.message_id,)).fetchone()[0]
    for suffix in ("", "/assistant"):
        page = client.get(f"/matters/{matter.slug}{suffix}", params={"conversation": conversation.conversation_id})
        assert page.status_code == 200
        assert expected in page.text
        if historical != expected:
            assert historical not in page.text
    for suffix in ("/export", f"/messages/{message.message_id}/export"):
        for format_name in ("markdown", "docx"):
            artifact = client.get(f"/matters/{matter.slug}/conversations/{conversation.conversation_id}{suffix}",
                params={"format": format_name})
            assert artifact.status_code == 200
            text = readable(artifact.content, format_name)
            assert expected in text
            if historical != expected:
                assert historical not in text
    bundle = client.get(f"/matters/{matter.slug}/export")
    assert bundle.status_code == 200
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        portable = json.loads(archive.read("conversations.json"))
        text = json.dumps(portable)
        assert expected in text
        if historical != expected:
            assert historical not in text
    assert bench.workspace.connection.execute("SELECT payload_json FROM workbench_message WHERE message_id=?",
        (message.message_id,)).fetchone()[0] == raw
    assert bench.workspace.messages(matter.matter_id, conversation.conversation_id)[-1] == message


@pytest.mark.parametrize("mode,historical,expected", CASES)
def test_saved_research_notice_views_exports_bundle_preserve_ledger(workspace, mode, historical, expected):
    client, bench, matter = workspace
    job, document = saved_research(bench, matter)
    original_units = document.parsed_units()
    original_bytes = bench.source_store(matter).source_path(document.document_id).read_bytes()
    result = copy.deepcopy(job.result)
    result["answer"]["modality_coverage"] = coverage(mode, historical)
    encoded = json.dumps(result)
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute("UPDATE workbench_research_job SET result_json=? WHERE job_id=?",
            (encoded, job.job_id))
    page = client.get(f"/matters/{matter.slug}/research", params={"job": job.job_id})
    assert page.status_code == 200
    assert expected in page.text
    if historical != expected:
        assert historical not in page.text
    for format_name in ("json", "markdown", "docx"):
        artifact = client.get(f"/matters/{matter.slug}/research/{job.job_id}/export", params={"format": format_name})
        assert artifact.status_code == 200
        text = readable(artifact.content, format_name)
        assert expected in text
        if historical != expected:
            assert historical not in text
        if format_name == "json":
            assert artifact.json()["investigation"]["answer"]["requested_source_coverage"]["notice"] == expected
            assert artifact.json()["investigation"]["supporting_sources"][0]["excerpt"] == result["evidence"][0]["excerpt"]
    bundle = client.get(f"/matters/{matter.slug}/export")
    assert bundle.status_code == 200
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        for name in (name for name in archive.namelist() if name.startswith("investigations/")):
            text = readable(archive.read(name), "docx" if name.endswith(".docx") else "text")
            assert expected in text
            if historical != expected:
                assert historical not in text
    assert bench.workspace.connection.execute("SELECT result_json FROM workbench_research_job WHERE job_id=?",
        (job.job_id,)).fetchone()[0] == encoded
    current = bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id)
    assert current.result == result
    assert current.result["evidence"] == job.result["evidence"]
    assert document.parsed_units() == original_units
    assert bench.source_store(matter).source_path(document.document_id).read_bytes() == original_bytes


def test_custom_authored_body_is_not_a_generated_notice(workspace):
    client, bench, matter = workspace
    conversation = bench.workspace.get_conversation(matter.matter_id)
    custom = CASES[0][1]
    saved = bench.workspace.append_message(matter.matter_id, conversation.conversation_id,
        "assistant", custom, {"kind": "manual"})
    assert custom in client.get(f"/matters/{matter.slug}").text
    exported = client.get(f"/matters/{matter.slug}/conversations/{conversation.conversation_id}/export?format=markdown")
    assert exported.status_code == 200 and custom in exported.text
    assert bench.workspace.messages(matter.matter_id, conversation.conversation_id)[-1] == saved


@pytest.mark.parametrize("surface", ["answer", "research"])
def test_export_does_not_reclassify_custom_notice_after_whitespace_cleanup(workspace, surface):
    client, bench, matter = workspace
    historical = CASES[0][1]
    custom = " " + historical
    if surface == "research":
        job, _ = saved_research(bench, matter)
        result = copy.deepcopy(job.result)
        result["answer"]["modality_coverage"] = coverage("complete", custom)
        with fixture_write(bench.workspace):
            bench.workspace.connection.execute("UPDATE workbench_research_job SET result_json=? WHERE job_id=?",
                (json.dumps(result), job.job_id))
        response = client.get(f"/matters/{matter.slug}/research/{job.job_id}/export?format=json")
        assert response.status_code == 200
        assert response.json()["investigation"]["answer"]["requested_source_coverage"]["notice"] == historical
        export_path = f"/matters/{matter.slug}/research/{job.job_id}/export"
    else:
        conversation = bench.workspace.get_conversation(matter.matter_id)
        bench.workspace.append_message(matter.matter_id, conversation.conversation_id, "assistant",
            "Synthetic custom notice.", {"kind": "generated", "claims": [],
                "modality_coverage": coverage("complete", custom)})
        bundle = client.get(f"/matters/{matter.slug}/export")
        assert bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            portable = json.loads(archive.read("conversations.json"))
            assert portable["conversations"][0]["messages"][-1]["answer"]["requested_source_coverage"]["notice"] == historical
        export_path = f"/matters/{matter.slug}/conversations/{conversation.conversation_id}/export"
    for format_name in ("markdown", "docx"):
        response = client.get(export_path, params={"format": format_name})
        assert response.status_code == 200
        text = readable(response.content, format_name)
        assert historical in text and COMPLETE not in text
