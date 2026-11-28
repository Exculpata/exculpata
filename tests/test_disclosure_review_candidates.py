"""Synthetic fixture, wording-guard, HTTP review/export, and eval tests for #272 disclosure-review candidates."""
from __future__ import annotations

import json
from pathlib import Path
import re

from fastapi.testclient import TestClient

from case_intelligence.case_assistant import (
    AssistantFinding, AssistantInvestigationResult, AssistantModelCall, AssistantTurn, CaseAssistant,
)
from case_intelligence.case_assistant_proposals import (
    DISCLOSURE_NOTICE,
    DISCLOSURE_SUBTYPES,
    PROHIBITED_DISCLOSURE_PHRASES,
    build_verified_proposals,
    detect_disclosure_candidates,
    has_prohibited_disclosure_wording,
)
from case_intelligence.case_assistant_tools import MatterToolRuntime, SourceRef
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workbench_assistant import WorkbenchToolBackend
from tests.test_milestone_a_workbench import _answer_and_wait, _create_matter


ROOT = Path(__file__).resolve().parents[1]


def test_disclosure_candidate_fixtures_cover_all_four_categories_and_negatives():
    fixtures = [
        ("inconsistent_statement", SourceRef("doc-1", "interview-1.txt", "Page 1", "v1"),
         "Witness Jordan later admitted the prior statement was inconsistent and contradicted the earlier account given on Tuesday."),
        ("witness_benefit", SourceRef("doc-2", "proffer-memo.txt", "Page 1", "v1"),
         "The cooperating witness was offered a cooperation agreement and sentencing leniency recommendation in exchange for testimony."),
        ("alternate_suspect", SourceRef("doc-3", "tip-report.txt", "Page 1", "v1"),
         "An alternate suspect matching the witness description was seen near the storage gate and another person confessed to moving the crate."),
        ("credibility_issue", SourceRef("doc-4", "background-note.txt", "Page 1", "v1"),
         "Internal records note prior false report findings and bias against the defendant raising witness credibility concerns."),
    ]
    negatives = [
        (SourceRef("doc-5", "schedule.txt", "Page 1", "v1"),
         "The delivery truck arrived at 09:00 and unloaded twelve wooden pallets at Bay 3."),
        (SourceRef("doc-6", "receipt.txt", "Page 1", "v1"),
         "Invoice 4412 confirmed payment of 250.00 for routine lighting maintenance."),
    ]
    refs = tuple(ref for _, ref, _ in fixtures) + tuple(ref for ref, _ in negatives)
    texts = {
        **{(ref.document_id, ref.location, ref.source_version_id): body for _, ref, body in fixtures},
        **{(ref.document_id, ref.location, ref.source_version_id): body for ref, body in negatives},
    }
    candidates = detect_disclosure_candidates(refs, texts)
    by_subtype = {c.subtype: c for c in candidates}
    assert set(DISCLOSURE_SUBTYPES) == {"inconsistent_statement", "witness_benefit", "alternate_suspect", "credibility_issue"}
    for subtype, ref, _ in fixtures:
        assert subtype in by_subtype, f"Missing expected candidate category {subtype}"
        cand = by_subtype[subtype]
        assert cand.category == "disclosure_review"
        assert cand.citations == (ref,)
        assert "candidate for" in cand.label.casefold() or "candidate for" in cand.disclosure_note.casefold()
        assert not has_prohibited_disclosure_wording(f"{cand.label} {cand.title} {cand.detail} {cand.disclosure_note}")
    flagged_doc_ids = {ref.document_id for c in candidates for ref in c.citations}
    assert "doc-5" not in flagged_doc_ids and "doc-6" not in flagged_doc_ids


def test_no_disclosure_flag_without_citation_and_prohibited_wording_rejected():
    ref = SourceRef("doc-1", "memo.txt", "Page 1", "v1")
    result = AssistantInvestigationResult(
        answerable=True, summary="Witness deal noted.",
        findings=(AssistantFinding("Witness deal noted.", (ref,), ()),),
        omitted_findings=0, gaps=(), competing_accounts=(),
        citations=(ref,), receipts={}, trace=(), turns_used=1, elapsed_ms=5,
    )
    proposals = build_verified_proposals(result, raw_proposals=(
        {"category": "disclosure_review", "subtype": "witness_benefit",
         "title": "Uncited witness deal", "detail": "No citation attached.", "citations": []},
        {"category": "disclosure_review", "subtype": "witness_benefit",
         "title": "Conclusive legal claim",
         "detail": "This document is Brady material and proves a Brady violation where disclosure was required.",
         "citations": [{"document_id": "doc-1", "location": "Page 1"}]},
        {"category": "disclosure_review", "subtype": "witness_benefit",
         "title": "Neutral witness-benefit candidate",
         "detail": "Memo notes leniency discussion; candidate for attorney review.",
         "citations": [{"document_id": "doc-1", "location": "Page 1"}]},
    ))
    disc = [p for p in proposals if p.category == "disclosure_review"]
    assert len(disc) == 1
    assert disc[0].title == "Neutral witness-benefit candidate"
    assert disc[0].citations == (ref,)
    assert disc[0].subtype == "witness_benefit"


def test_ui_templates_exports_and_docs_use_candidate_language_and_avoid_prohibited_conclusions():
    files_to_scan = [
        *sorted((ROOT / "src/case_intelligence/templates").glob("*.html")),
        ROOT / "src/case_intelligence/work_product_exports.py",
        ROOT / "src/case_intelligence/case_assistant_proposals.py",
        ROOT / "docs/CASE_ASSISTANT.md",
    ]
    assert PROHIBITED_DISCLOSURE_PHRASES
    for path in files_to_scan:
        content = path.read_text(encoding="utf-8")
        if path.name == "case_assistant_proposals.py":
            # Exclude the prohibited-phrase constant definition block itself when checking prose.
            content = re.sub(r"PROHIBITED_DISCLOSURE_PHRASES[^=]*=\s*\([^)]*\)", "", content, flags=re.DOTALL)
        assert not has_prohibited_disclosure_wording(content), (
            f"Prohibited conclusive disclosure wording found in {path.relative_to(ROOT)}"
        )
    assert "candidate for attorney review" in DISCLOSURE_NOTICE.casefold()
    assert "candidates for review" in (ROOT / "docs/CASE_ASSISTANT.md").read_text(encoding="utf-8").casefold()


class _DisclosureAssistantClient:
    available = True

    def __init__(self) -> None:
        self.turn = 0

    def complete_turn(self, messages, *, tools):
        self.turn += 1
        if self.turn == 1:
            return AssistantTurn(tool_calls=(
                AssistantModelCall("s1", "search_discovery", {"query": "leniency cooperation agreement", "limit": 5}),
                AssistantModelCall("s2", "search_discovery", {"query": "alternate suspect confessed", "limit": 5}),
            ))
        tool_msgs = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
        hits = [h for m in tool_msgs for h in m.get("hits", ())]
        deal_hit = next(h for h in hits if "proffer" in h["source_name"])
        alt_hit = next(h for h in hits if "tip" in h["source_name"])
        deal_cite = {k: deal_hit[k] for k in ("document_id", "source_name", "location", "source_version_id")}
        alt_cite = {k: alt_hit[k] for k in ("document_id", "source_name", "location", "source_version_id")}
        return AssistantTurn(content=json.dumps({
            "answerable": True,
            "summary": "Two passages warrant attorney review as potential disclosure candidates.",
            "findings": [
                {"text": "Proffer memo notes a cooperation agreement and sentencing leniency recommendation.",
                 "citations": [deal_cite], "calculation_ids": []},
                {"text": "Tip report describes an alternate suspect seen near the storage gate.",
                 "citations": [alt_cite], "calculation_ids": []},
            ],
            "proposals": [
                {"category": "disclosure_review", "subtype": "witness_benefit",
                 "title": "Cooperating witness leniency agreement",
                 "detail": "Candidate for review: proffer memo records sentencing leniency in exchange for testimony.",
                 "citations": [deal_cite]},
                {"category": "disclosure_review", "subtype": "alternate_suspect",
                 "title": "Alternate suspect sighting near storage gate",
                 "detail": "Candidate for review: tip report identifies another person near the storage gate.",
                 "citations": [alt_cite]},
            ],
            "gaps": [], "competing_accounts": [],
        }))


def test_disclosure_candidates_end_to_end_review_dismiss_and_export_with_citations(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    monkeypatch.setenv("CASE_INTELLIGENCE_CASE_ASSISTANT", "1")
    app = create_workbench_app(tmp_path / "runtime", generator=_DisclosureAssistantClient(), auth_mode="test")
    with TestClient(app) as client:
        slug = _create_matter(client, "Synthetic Disclosure Candidate Matter")
        upload = client.post(f"/matters/{slug}/uploads", files=[
            ("files", ("proffer-memo.txt", b"Cooperating witness offered cooperation agreement and sentencing leniency recommendation.\n", "text/plain")),
            ("files", ("tip-report.txt", b"Alternate suspect seen near the storage gate and confessed to moving the crate.\n", "text/plain")),
            ("files", ("routine-log.txt", b"Standard shift log with no unusual events.\n", "text/plain")),
        ])
        assert upload.status_code == 200
        workspace = client.get(f"/matters/{slug}")
        conv_id = re.search(r'name="conversation" value="([^"]+)"', workspace.text).group(1)

        _, page = _answer_and_wait(client, slug, conv_id, "Surface any potential impeachment or exculpatory candidates for review.")
        assert "Candidate for attorney review" in page.text
        assert not has_prohibited_disclosure_wording(page.text)

        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        actions = list(dict.fromkeys(re.findall(r'action="(/matters/[^"]+/assistant-proposals/[^"]+)"', page.text)))
        assert len(actions) >= 2

        # 1. Mark first candidate as reviewed (confirmed)
        reviewed = client.post(
            actions[0], data={"csrf_token": csrf, "decision": "review"},
            headers={"Accept": "application/json"},
        )
        assert reviewed.status_code == 200 and reviewed.json()["status"] == "confirmed"

        # 2. Dismiss second candidate
        dismissed = client.post(
            actions[1], data={"csrf_token": csrf, "decision": "dismiss"},
            headers={"Accept": "application/json"},
        )
        assert dismissed.status_code == 200 and dismissed.json()["status"] == "dismissed"

        # 3. Export notebook with citations (Markdown + CSV) and verify neutral candidate language
        md_export = client.get(f"/matters/{slug}/notebook/export", params={"format": "markdown"})
        assert md_export.status_code == 200
        md_text = md_export.text
        assert "candidate for attorney review" in md_text.casefold()
        assert "proffer-memo.txt" in md_text or "tip-report.txt" in md_text
        assert not has_prohibited_disclosure_wording(md_text)

        csv_export = client.get(f"/matters/{slug}/notebook/export", params={"format": "csv"})
        assert csv_export.status_code == 200
        assert not has_prohibited_disclosure_wording(csv_export.text)

        # 4. Eval-style direct CaseAssistant run proves raw_proposals flow through verification cleanly.
        bench = app.state.workbench
        actor = "development-taylor-morgan"
        matter = bench.workspace.get_matter(slug, actor)
        runtime = MatterToolRuntime(matter.matter_id, actor, WorkbenchToolBackend(bench, actor))
        inv = CaseAssistant(_DisclosureAssistantClient()).investigate(
            "Identify any candidates for attorney disclosure review.", runtime
        )
        verified_props = build_verified_proposals(inv, raw_proposals=inv.raw_proposals)
        disc_props = [p for p in verified_props if p.category == "disclosure_review"]
        assert {p.subtype for p in disc_props} == {"witness_benefit", "alternate_suspect"}
        assert all(p.citations for p in disc_props)
