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
        actions: list[str] = []
        for match in re.findall(r'action="(/matters/[^"]+/assistant-proposals/[^"]+)"', page.text):
            if match not in actions:
                actions.append(match)
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


class _NoProposalAndFabricatedNumClient:
    """Model client that emits an ungrounded-number proposal and omits disclosure proposals."""

    available = True
    model = "synthetic-disclosure-round2"

    def __init__(self, *, emit_fabricated_proposal: bool) -> None:
        self._emit_fabricated = emit_fabricated_proposal
        self._step = 0

    def complete_turn(self, messages, *, tools):
        self._step += 1
        if self._step == 1:
            return AssistantTurn(tool_calls=(
                AssistantModelCall("s1", "search_discovery", {"query": "cooperation agreement sentencing leniency", "limit": 5}),
            ))
        hits = json.loads(messages[-1]["content"]).get("hits") or ()
        hit = next(h for h in hits if "proffer" in h["source_name"])
        cite = {k: hit[k] for k in ("document_id", "source_name", "location", "source_version_id")}
        payload: dict[str, object] = {
            "answerable": True,
            "summary": "Proffer memo documents a cooperation agreement with sentencing leniency.",
            "findings": [{
                "text": "Proffer memo documents a cooperation agreement with sentencing leniency.",
                "citations": [cite], "calculation_ids": [],
            }],
            "gaps": [], "competing_accounts": [],
        }
        if self._emit_fabricated:
            payload["proposals"] = [
                {
                    "category": "disclosure_review",
                    "subtype": "witness_benefit",
                    "title": "Unsupported payment of $250000 in proffer memo",
                    "detail": "Fabricated $250000 figure not present in cited source.",
                    "citations": [cite],
                },
                {
                    "category": "case_theory",
                    "title": "Cooperating witness received sentencing leniency",
                    "detail": "Proffer memo notes a cooperation agreement with sentencing leniency.",
                    "citations": [cite],
                },
            ]
        return AssistantTurn(content=json.dumps(payload))


def test_pr609_round2_proposal_grounding_deterministic_detection_and_revision_transitions(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    monkeypatch.setenv("CASE_INTELLIGENCE_CASE_ASSISTANT", "1")

    # 1. Production workbench surfaces deterministic disclosure candidates even when model omits proposals,
    #    and enforces expected_updated_at when transitioning a saved proposal to reviewed/dismissed.
    app = create_workbench_app(
        tmp_path / "rt1", generator=_NoProposalAndFabricatedNumClient(emit_fabricated_proposal=False), auth_mode="test",
    )
    with TestClient(app) as client:
        slug = _create_matter(client, "Synthetic Deterministic Disclosure Matter")
        up = client.post(f"/matters/{slug}/uploads", files=[
            ("files", ("proffer-memo.txt", b"Cooperating witness signed a cooperation agreement for sentencing leniency.\n", "text/plain")),
        ])
        assert up.status_code == 200
        workspace = client.get(f"/matters/{slug}")
        conv_id = re.search(r'name="conversation" value="([^"]+)"', workspace.text).group(1)

        _, page = _answer_and_wait(client, slug, conv_id, "Review the proffer memo.")
        assert "Witness benefit or agreement" in page.text
        assert "Candidate for attorney review" in page.text

        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        actions: list[str] = []
        for match in re.findall(r'action="(/matters/[^"]+/assistant-proposals/[^"]+)"', page.text):
            if match not in actions:
                actions.append(match)
        assert actions

        # Save first -> returns updated_at
        saved = client.post(actions[0], data={"csrf_token": csrf, "decision": "save"}, headers={"Accept": "application/json"})
        assert saved.status_code == 200
        saved_json = saved.json()
        assert saved_json["created"] is True and saved_json["updated_at"]

        # Reloaded page renders the saved revision in the hidden expected_updated_at field.
        reloaded = client.get(f"/matters/{slug}", params={"conversation": conv_id})
        assert saved_json["updated_at"] in reloaded.text

        # Missing or stale expected_updated_at fails closed with 409 Conflict (no TypeError / silent overwrite).
        stale = client.post(
            actions[0],
            data={"csrf_token": csrf, "decision": "review", "expected_updated_at": "2000-01-01T00:00:00Z"},
            headers={"Accept": "application/json"},
        )
        assert stale.status_code == 409

        # Valid expected_updated_at transitions saved proposal to confirmed and advances updated_at.
        reviewed = client.post(
            actions[0],
            data={"csrf_token": csrf, "decision": "review", "expected_updated_at": saved_json["updated_at"]},
            headers={"Accept": "application/json"},
        )
        assert reviewed.status_code == 200
        reviewed_json = reviewed.json()
        assert reviewed_json["status"] == "confirmed" and reviewed_json["updated_at"] != saved_json["updated_at"]

        # Subsequent transition to dismissed with the new revision also succeeds cleanly.
        dismissed = client.post(
            actions[0],
            data={"csrf_token": csrf, "decision": "dismiss", "expected_updated_at": reviewed_json["updated_at"]},
            headers={"Accept": "application/json"},
        )
        assert dismissed.status_code == 200 and dismissed.json()["status"] == "dismissed"

        # 2. Ungrounded numbers in model-emitted proposals ($250000) are dropped before exposure.
        bench = app.state.workbench
        actor = "development-taylor-morgan"
        matter = bench.workspace.get_matter(slug, actor)
        runtime = MatterToolRuntime(matter.matter_id, actor, WorkbenchToolBackend(bench, actor))
        inv = CaseAssistant(_NoProposalAndFabricatedNumClient(emit_fabricated_proposal=True)).investigate(
            "Check proffer memo.", runtime,
        )
        assert all("250000" not in str(p.get("title", "")) and "250000" not in str(p.get("detail", "")) for p in inv.raw_proposals)
        assert any(p.get("category") == "case_theory" for p in inv.raw_proposals)
        assert inv.inspected_texts

    # 3. Proposal form JS submits event.submitter so Mark reviewed / Dismiss send their decision value.
    js_path = Path(__file__).resolve().parent.parent / "src" / "case_intelligence" / "static" / "case-intelligence.js"
    js_src = js_path.read_text(encoding="utf-8")
    assert "new FormData(form, submitter)" in js_src
    assert "data-assistant-proposal-revision" in js_src


def test_pr609_round3_thirteenth_citation_truncation_admin_preview_and_long_conversation_state(tmp_path, monkeypatch):
    from case_intelligence.case_assistant import verify_assistant_answer
    from case_intelligence.workbench_assistant import saved_proposal_states

    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    monkeypatch.setenv("CASE_INTELLIGENCE_CASE_ASSISTANT", "1")

    from types import SimpleNamespace

    refs = [SourceRef(f"d-{i}", f"doc-{i}.txt", "Page 1", f"v-{i}") for i in range(1, 14)]
    rt = SimpleNamespace(inspected_refs=tuple(refs), receipts={}, trace=())
    inspected_texts = {
        (r.document_id, r.location, r.source_version_id): (
            "Synthetic memo mentioning $250000." if idx == 13 else f"Routine neutral note {idx}."
        )
        for idx, r in enumerate(refs, 1)
    }
    raw = {
        "answerable": True,
        "summary": "Routine note 1.",
        "findings": [{
            "text": "Routine note 1.",
            "citations": [{"document_id": "d-1", "location": "Page 1", "source_version_id": "v-1"}],
            "calculation_ids": [],
        }],
        "proposals": [{
            "category": "disclosure_review",
            "subtype": "witness_benefit",
            "title": "Payment of $250000 noted in thirteenth reference",
            "detail": "Candidate for review citing $250000.",
            "citations": [
                {"document_id": r.document_id, "location": r.location, "source_version_id": r.source_version_id}
                for r in refs
            ],
        }],
        "gaps": [], "competing_accounts": [],
    }
    verified = verify_assistant_answer(raw, rt, inspected_texts, (), turns_used=1, budget_exhausted=False, elapsed_ms=1)
    assert verified.raw_proposals == ()

    # 2 & 3. Nonmember admin preview omits proposal_states cleanly without KeyError, and workspace renders
    #        saved expected_updated_at even after >8 subsequent conversation messages push the card out of the dock window.
    app = create_workbench_app(
        tmp_path / "rt3", generator=_NoProposalAndFabricatedNumClient(emit_fabricated_proposal=False), auth_mode="test",
    )
    with TestClient(app) as client:
        slug = _create_matter(client, "Synthetic Long Conversation Proposal Matter")
        up = client.post(f"/matters/{slug}/uploads", files=[
            ("files", ("proffer-memo.txt", b"Cooperating witness signed a cooperation agreement for sentencing leniency.\n", "text/plain")),
        ])
        assert up.status_code == 200
        workspace = client.get(f"/matters/{slug}")
        conv_id = re.search(r'name="conversation" value="([^"]+)"', workspace.text).group(1)

        _, page = _answer_and_wait(client, slug, conv_id, "Review the proffer memo.")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        actions: list[str] = []
        for match in re.findall(r'action="(/matters/[^"]+/assistant-proposals/[^"]+)"', page.text):
            if match not in actions:
                actions.append(match)
        saved = client.post(actions[0], data={"csrf_token": csrf, "decision": "save"}, headers={"Accept": "application/json"})
        assert saved.status_code == 200
        rev = saved.json()["updated_at"]

        bench = app.state.workbench
        actor = "development-taylor-morgan"
        matter = bench.workspace.get_matter(slug, actor)

        # Nonmember administrator call must return {} rather than raising KeyError.
        msgs = bench.workspace.messages(matter.matter_id, conv_id)
        assert saved_proposal_states(bench, matter.matter_id, "nonmember-admin-id", msgs) == {}

        # Append 10 more messages so the saved proposal message falls outside assistant_context's [-8:] slice.
        for i in range(10):
            bench.workspace.append_message(matter.matter_id, conv_id, "user", f"Follow-up note {i}")

        reloaded = client.get(f"/matters/{slug}", params={"conversation": conv_id})
        assert reloaded.status_code == 200
        assert f'name="expected_updated_at" value="{rev}"' in reloaded.text

        # Editing the saved note in the Notebook prevents blind confirmation from the original proposal card (IDEAL E4).
        edited = bench.workspace.update_notebook_item(
            matter.matter_id, actor, saved.json()["item_id"],
            item_type=saved.json()["item_type"], status=saved.json()["status"],
            title="Attorney-edited note title", body="Substantively edited notebook body.",
            date_label="", pinned=False, expected_updated_at=rev,
        )
        after_edit = client.get(f"/matters/{slug}", params={"conversation": conv_id})
        assert after_edit.status_code == 200
        assert "Edited in case memory; review and update status in the Notebook." in after_edit.text
        assert f'name="expected_updated_at" value="{edited.updated_at}"' not in after_edit.text
        refused = client.post(
            actions[0],
            data={"csrf_token": csrf, "decision": "review", "expected_updated_at": edited.updated_at},
            headers={"Accept": "application/json"},
        )
        assert refused.status_code == 409



