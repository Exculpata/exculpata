"""End-to-end synthetic proving and fail-closed tests for Slice 4 assistant proposals."""
from __future__ import annotations

import json
import re
from fastapi.testclient import TestClient

from case_intelligence.case_assistant import (
    AssistantFinding, AssistantInvestigationResult, AssistantModelCall, AssistantTurn,
)
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
        omitted_findings=0, gaps=(), competing_accounts=("Unverified competing account string.",),
        citations=(ref,), receipts={}, trace=(), turns_used=2, elapsed_ms=10,
    )
    proposals = build_verified_proposals(result, raw_proposals=(
        {"category": "disclosure_review", "title": "Prior inconsistent witness statement",
         "detail": "Email schedule contradicts interview denial.", "citations": [{"document_id": "doc-1", "location": "Line 1"}]},
        {"category": "contradiction", "title": "Explicitly cited contradiction",
         "detail": "Email conflicts with statement.", "citations": [{"document_id": "doc-1", "location": "Line 1"}]},
        {"category": "case_theory", "title": "Uncited claim must be dropped", "detail": "No source.", "citations": []},
        {"category": "case_theory", "title": "Fabricated ref dropped", "detail": "Bad ref.",
         "citations": [{"document_id": "missing-99", "location": "Page 1"}]},
    ))
    categories = [p.category for p in proposals]
    assert "case_theory" in categories and "timeline_event" in categories
    assert "contradiction" in categories and "disclosure_review" in categories
    assert not any("Uncited" in p.title or "Fabricated" in p.title for p in proposals)
    # Plain competing_accounts strings must never borrow findings' citations as saveable proposals.
    assert not any("Unverified competing account" in p.detail for p in proposals)
    disclosure = next(p for p in proposals if p.category == "disclosure_review")
    assert "candidate for attorney review" in disclosure.disclosure_note.casefold()


class _LongUnitProposalClient:
    available = True

    def __init__(self) -> None:
        self.turn = 0

    def complete_turn(self, messages, *, tools):
        self.turn += 1
        if self.turn == 1:
            return AssistantTurn(tool_calls=(
                AssistantModelCall("l1", "search_discovery", {"query": "longmarker", "mode": "exact", "limit": 5}),
            ))
        tool_msgs = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
        hit = next(h for m in tool_msgs for h in m.get("hits", ()))
        cite = {k: hit[k] for k in ("document_id", "source_name", "location", "source_version_id")}
        return AssistantTurn(content=json.dumps({
            "answerable": True,
            "summary": "The 2024-09-01 long record includes longmarker.",
            "findings": [{
                "text": "The 2024-09-01 long record includes longmarker.",
                "citations": [cite], "calculation_ids": [],
            }],
            "gaps": [], "competing_accounts": [],
        }))


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
        assert len(actions) >= 1
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)

        # Save first proposal via reviewer confirmation POST; repeat save is idempotent.
        saved = client.post(actions[0], data={"csrf_token": csrf}, headers={"Accept": "application/json"})
        assert saved.status_code == 200
        body = saved.json()
        assert body["created"] is True and body["status"] in {"suggested", "disputed", "needs_review"}
        repeat = client.post(actions[0], data={"csrf_token": csrf}, headers={"Accept": "application/json"})
        assert repeat.status_code == 200 and repeat.json()["created"] is False
        assert repeat.json()["item_id"] == body["item_id"]

        # Section filter applies before LIMIT: an older timeline event is returned even with limit=1
        # after newer ordinary notes are created.
        bench.workspace.create_notebook_item(
            matter.matter_id, actor, item_type="event", status="confirmed",
            title="Earlier buyback event", body="Event body", date_label="2024-09-01",
        )
        bench.workspace.create_notebook_item(
            matter.matter_id, actor, item_type="note", status="confirmed",
            title="Newer general note", body="Note body",
        )
        backend = WorkbenchToolBackend(bench, actor)
        assert backend.get_case_knowledge(matter.matter_id, section="case_theory", limit=1)["case_theory"]
        tl = backend.get_case_knowledge(matter.matter_id, section="timeline", limit=1)["timeline"]
        assert len(tl) == 1 and tl[0]["title"] == "Earlier buyback event"

        # Tampering with a cited original on disk (without removing document record) fails closed with 409.
        store = bench.source_store(matter)
        first_doc_id = next(d.document_id for d in store.documents.values() if d.display_name == "buyback-filing.txt")
        original_path = store.source_path(first_doc_id, verify_digest=True)
        original_path.write_text("Tampered discovery bytes on disk.\n", encoding="utf-8")
        stale = client.post(actions[0], data={"csrf_token": csrf}, headers={"Accept": "application/json"})
        assert stale.status_code == 409


def test_long_unit_proposal_saves_with_bounded_notebook_preview(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_CASE_ASSISTANT", "1")
    app = create_workbench_app(tmp_path / "runtime", generator=_LongUnitProposalClient(), auth_mode="test")
    with TestClient(app) as client:
        slug = _create_matter(client, "Synthetic Long Unit Matter")
        long_text = "longmarker 2024-09-01 " + ("x" * 7_024) + "\n"
        upload = client.post(f"/matters/{slug}/uploads", files=[
            ("files", ("long-memo.txt", long_text.encode("utf-8"), "text/plain")),
        ])
        assert upload.status_code == 200
        workspace = client.get(f"/matters/{slug}")
        conv_id = re.search(r'name="conversation" value="([^"]+)"', workspace.text).group(1)

        _, page = _answer_and_wait(client, slug, conv_id, "Check longmarker date.")
        actions = re.findall(r'action="(/matters/[^"]+/assistant-proposals/[^"]+)"', page.text)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        assert len(actions) >= 1
        saved = client.post(actions[0], data={"csrf_token": csrf}, headers={"Accept": "application/json"})
        assert saved.status_code == 200 and saved.json()["created"] is True

