"""End-to-end synthetic proving and fail-closed tests for Slice 4 assistant proposals."""
from __future__ import annotations

import re
from fastapi.testclient import TestClient

from case_intelligence.case_assistant import AssistantFinding, AssistantInvestigationResult
from case_intelligence.case_assistant_proposals import build_verified_proposals
from case_intelligence.case_assistant_tools import SourceRef
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workbench_assistant import WorkbenchToolBackend
from tests.test_milestone_a_workbench import _answer_and_wait, _create_matter
from tests.test_workbench_assistant import _ScriptedWorkbenchAssistantClient


def test_build_verified_proposals_drops_uncited_and_labels_disclosure_candidates():
    ref = SourceRef("doc-1", "filing.txt", "Line 1", "v1")
    result = AssistantInvestigationResult(
        answerable=True, summary="Ownership exceeded 10% on 2024-09-01.",
        findings=(AssistantFinding("Holdings exceeded 10% after 2024-09-01 buyback.", (ref,), ()),),
        omitted_findings=0, gaps=(), competing_accounts=("Witness denied instructions despite emails.",),
        citations=(ref,), receipts={}, trace=(), turns_used=2, elapsed_ms=10,
    )
    proposals = build_verified_proposals(result, raw_proposals=(
        {"category": "disclosure_review", "title": "Prior inconsistent witness statement",
         "detail": "Email schedule contradicts interview denial.", "citations": [{"document_id": "doc-1", "location": "Line 1"}]},
        {"category": "case_theory", "title": "Uncited claim must be dropped", "detail": "No source.", "citations": []},
        {"category": "case_theory", "title": "Fabricated ref dropped", "detail": "Bad ref.",
         "citations": [{"document_id": "missing-99", "location": "Page 1"}]},
    ))
    categories = [p.category for p in proposals]
    assert "case_theory" in categories and "timeline_event" in categories
    assert "contradiction" in categories and "disclosure_review" in categories
    assert not any("Uncited" in p.title or "Fabricated" in p.title for p in proposals)
    disclosure = next(p for p in proposals if p.category == "disclosure_review")
    assert "candidate for attorney review" in disclosure.disclosure_note.casefold()


def test_reviewer_gated_proposal_save_idempotency_memory_readback_and_stale_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_CASE_ASSISTANT", "1")
    app = create_workbench_app(tmp_path / "runtime", generator=_ScriptedWorkbenchAssistantClient(), auth_mode="test")
    with TestClient(app) as client:
        slug = _create_matter(client, "Synthetic Proposal Matter")
        upload = client.post(f"/matters/{slug}/uploads", files=[
            ("files", ("buyback-filing.txt", b"Orbel Spindle Works buyback effective 2024-09-01 outstanding shares 800000.\n", "text/plain")),
            ("files", ("stmt-stk0.txt", b"Position date=2024-09-30 account=stk-0 shares=50000.\n", "text/plain")),
            ("files", ("stmt-stk1.txt", b"Position date=2024-09-30 account=stk-1 shares=40000.\n", "text/plain")),
            ("files", ("damaged-scan.pdf", b"%PDF-1.4\n%%corrupted-stream-body\n", "application/pdf")),
        ])
        assert upload.status_code == 200
        workspace = client.get(f"/matters/{slug}")
        conv_id = re.search(r'name="conversation" value="([^"]+)"', workspace.text).group(1)

        _, page = _answer_and_wait(client, slug, conv_id, "Did controlled accounts exceed 10% after the buyback?")
        assert "Save to case memory" in page.text
        bench = app.state.workbench
        actor = "development-taylor-morgan"
        matter = bench.workspace.get_matter(slug, actor)

        # Nothing is written automatically before attorney confirmation.
        assert bench.workspace.all_notebook_items(matter.matter_id, actor, include_dismissed=True) == ()

        actions = re.findall(r'action="(/matters/[^"]+/assistant-proposals/[^"]+)"', page.text)
        assert len(actions) >= 2
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)

        # Save first proposal via reviewer confirmation POST; repeat save is idempotent.
        saved = client.post(actions[0], data={"csrf_token": csrf}, headers={"Accept": "application/json"})
        assert saved.status_code == 200
        body = saved.json()
        assert body["created"] is True and body["status"] in {"suggested", "disputed", "needs_review"}
        repeat = client.post(actions[0], data={"csrf_token": csrf}, headers={"Accept": "application/json"})
        assert repeat.status_code == 200 and repeat.json()["created"] is False
        assert repeat.json()["item_id"] == body["item_id"]

        # Saved item flows back into future assistant get_case_knowledge calls.
        knowledge = WorkbenchToolBackend(bench, actor).get_case_knowledge(matter.matter_id, section="case_theory")
        assert knowledge["case_theory"]

        # Removing a cited original causes saving another proposal to fail closed with 409.
        store = bench.source_store(matter)
        first_doc_id = next(d.document_id for d in store.documents.values() if d.display_name == "buyback-filing.txt")
        store.remove(first_doc_id)
        stale = client.post(actions[1], data={"csrf_token": csrf}, headers={"Accept": "application/json"})
        assert stale.status_code == 409
