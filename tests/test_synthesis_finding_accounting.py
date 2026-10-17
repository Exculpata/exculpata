"""Synthetic finding survival, independent of source-unit citation coverage."""

from tests.fixture_write import fixture_write
from copy import deepcopy
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import io
import sqlite3
import zipfile

import pytest

from case_intelligence.generation import GroundedGenerationService
from case_intelligence.hierarchical_synthesis import (
    completion_receipt, findings_from_passes, groups_for, matter_inputs,
    node_spec, run_synthesis, synthesis_answer, validate_state,
)
from tests.test_full_text_synthesis import ACTOR, workspace

ACCOUNT = "Synthetic dispatch record states the crate was delivered."
CORRECTION = "Synthetic corrected dispatch record states the crate was not delivered."


class AccountOnly:
    available = True

    def generate(self, **kwargs):
        return {"answerable": True, "claims": [{"text": ACCOUNT, "evidence_ids": ["S1"]}],
                "limitation": None, "missing_information": ""}


def shared_unit():
    token = hashlib.sha1(b"synthetic-shared-unit").hexdigest()
    evidence = [{"support_token": token, "source_name": "synthetic-dispatch.txt",
                 "location": "page 1", "excerpt": ACCOUNT + " " + CORRECTION,
                 "evidence_kind": "document", "document_id": "synthetic-source",
                 "source_version_id": "synthetic-v1"}]
    passes = [{"answer": {"claims": [{"text": text, "citations": [{"support_token": token}]}
                                     for text in (ACCOUNT, CORRECTION)]}}]
    return passes, evidence


def synthesize(corpus, client=None, saved=None):
    return run_synthesis("What do the synthetic dispatch records say?", *corpus,
        GroundedGenerationService(client or AccountOnly()), lambda state: None,
        lambda: None, saved, now=lambda: 1000)


def test_dropped_correction_sharing_a_cited_unit_is_an_explicit_finding_omission():
    corpus = shared_unit()
    state = synthesize(corpus)
    assert state["uncited_evidence"] == []
    assert state["partial"]
    assert state["retained_findings"] == ["P1C1"]
    assert state["omitted_findings"] == ["P1C2"]
    assert state["issue"][0]["claims"][0]["parents"] == ["P1C1"]
    assert state["issue"][0]["omitted_parents"] == ["P1C2"]
    assert "saved findings omitted or not confirmed retained: 1 (P1C2)" in synthesis_answer(state, corpus[1]).text
    validate_state(state, *corpus, final=True)


@pytest.mark.parametrize("stage", ["issue", "matter"])
def test_each_stage_accounts_for_a_dropped_correction(stage):
    class Selective(AccountOnly):
        def generate(self, **kwargs):
            answer = super().generate(**kwargs)
            if stage == "matter" and "issue-level" in kwargs["working_context"]:
                answer["claims"].append({"text": CORRECTION, "evidence_ids": ["S1"]})
            return answer

    state = synthesize(shared_unit(), Selective())
    assert state["retained_findings"] == ["P1C1"]
    assert state["omitted_findings"] == ["P1C2"]
    assert state[stage][0]["omitted_parents"] == ["P1C2" if stage == "issue" else "issue1C2"]
    assert state["partial"] and state["uncited_evidence"] == []


def test_combined_output_retains_both_findings_with_their_own_parent_ids():
    class Combined(AccountOnly):
        def generate(self, **kwargs):
            answer = super().generate(**kwargs)
            answer["claims"][0]["text"] = ACCOUNT + " " + CORRECTION
            return answer

    corpus = shared_unit()
    state = synthesize(corpus, Combined())
    assert not state["partial"]
    assert state["retained_findings"] == ["P1C1", "P1C2"]
    assert state["omitted_findings"] == []
    assert state["issue"][0]["claims"][0]["parents"] == ["P1C1", "P1C2"]
    validate_state(state, *corpus, final=True)


def test_final_retention_is_not_inherited_from_a_combined_intermediate():
    class CombineThenDrop(AccountOnly):
        def generate(self, **kwargs):
            answer = super().generate(**kwargs)
            if "issue-level" in kwargs["working_context"]:
                answer["claims"][0]["text"] = ACCOUNT + " " + CORRECTION
            return answer

    corpus = shared_unit()
    state = synthesize(corpus, CombineThenDrop())
    assert state["issue"][0]["claims"][0]["parents"] == ["P1C1", "P1C2"]
    assert state["matter"][0]["omitted_parents"] == ["issue1C1"]
    assert state["retained_findings"] == ["P1C1"]
    assert state["omitted_findings"] == ["P1C2"]
    assert state["partial"]
    validate_state(state, *corpus, final=True)


def test_valid_paraphrase_remains_grounded_but_its_finding_retention_is_unconfirmed():
    class Paraphrase(AccountOnly):
        def generate(self, **kwargs):
            answer = super().generate(**kwargs)
            answer["claims"][0]["text"] = "The synthetic dispatch record says the crate was delivered."
            return answer

    corpus = shared_unit()
    state = synthesize(corpus, Paraphrase())
    assert synthesis_answer(state, corpus[1]).claims
    assert state["retained_findings"] == []
    assert state["omitted_findings"] == ["P1C1", "P1C2"]
    assert state["issue"][0]["claims"][0]["parents"] == []
    validate_state(state, *corpus, final=True)


@pytest.mark.parametrize("mutation", ["parents", "node_omissions", "retained", "omitted", "partial"])
def test_forged_retention_and_omission_metadata_is_rejected(mutation):
    corpus = shared_unit()
    state = synthesize(corpus)
    if mutation == "parents":
        state["issue"][0]["claims"][0]["parents"].append("P1C2")
    elif mutation == "node_omissions":
        state["issue"][0]["omitted_parents"] = []
    elif mutation == "retained":
        state["retained_findings"].append("P1C2")
    elif mutation == "omitted":
        state["omitted_findings"] = []
    else:
        state["partial"] = False
    with pytest.raises(ValueError):
        validate_state(state, *corpus, final=True)


def legacy_checkpoint(corpus, current=None):
    """Reconstruct the documented v1 group-parent JSON, without new fields."""
    from case_intelligence.hierarchical_synthesis import LEGACY_POLICY
    state = deepcopy(current) if current is not None else synthesize(corpus)
    state.update(version=1, policy=dict(LEGACY_POLICY))
    state.pop("retained_findings")
    state.pop("omitted_findings")
    ledger = {item["support_token"]: item for item in corpus[1]}
    findings, _ = findings_from_passes(corpus[0], ledger)
    for level in ("issue", "matter"):
        inputs = findings if level == "issue" else matter_inputs(state["issue"])
        for ordinal, (node, group) in enumerate(zip(state[level], groups_for(inputs))):
            node.update(node_spec(level, ordinal, group))
            node.pop("omitted_parents")
            for claim in node["claims"]:
                claim["parents"] = list(node["parents"])
    state.update(completion_receipt(state, findings, ledger))
    return state


@pytest.mark.parametrize("interrupted", [False, True])
def test_v1_reading_and_resume_upgrade_preserve_claims_spend_and_deadline(interrupted):
    corpus = shared_unit()
    saved = legacy_checkpoint(corpus)
    validate_state(saved, *corpus, final=True)
    assert "complete within its saved finding scope" in synthesis_answer(saved, corpus[1]).text
    if interrupted:
        saved["matter"] = []
        saved["stop_reason"] = "running"
    before = deepcopy(saved)

    class Counted(AccountOnly):
        calls = 0
        def generate(self, **kwargs):
            self.calls += 1
            return super().generate(**kwargs)

    client = Counted()
    resumed = synthesize(corpus, client, saved)
    assert saved == before
    assert resumed["version"] == 2 and resumed["partial"]
    assert client.calls == int(interrupted)
    assert resumed["requests_spent"] == saved["requests_spent"] + int(interrupted) == 2 + int(interrupted)
    assert resumed["started_at"] == saved["started_at"]
    assert resumed["omitted_findings"] == ["P1C2"]
    assert [claim.text for claim in synthesis_answer(resumed, corpus[1]).claims] == [ACCOUNT]
    validate_state(resumed, *corpus, final=True)


@pytest.mark.parametrize("retained", [2, 24])
def test_hierarchy_evaluator_requires_all_24_retained_records(tmp_path, monkeypatch, retained):
    """Controlled output tests the harness, never actual-model quality."""
    path = Path(__file__).resolve().parents[1] / "scripts/evaluate-hierarchical-synthesis.py"
    spec = importlib.util.spec_from_file_location("synthetic_hierarchy_evaluator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class EndpointsOnly:
        available = True
        endpoint = "http://127.0.0.1:11435"

        def generate(self, **kwargs):
            claims = [{"text": item.excerpt, "evidence_ids": [item.evidence_id]}
                      for item in kwargs["evidence"] if any(
                          f"record {number} states" in item.excerpt
                          for number in ((1, 24) if retained == 2 else range(1, 25))) ]
            return {"answerable": bool(claims), "claims": claims, "limitation": None,
                    "missing_information": "" if claims else "Synthetic controlled omission."}

    output = tmp_path / "synthetic-incomplete-evaluation.json"
    monkeypatch.setattr(sys, "argv", [str(path), "--model", "synthetic-model",
        "--expected-digest", "synthetic-digest", "--output", str(output)])
    monkeypatch.setattr(module, "OllamaGenerator", lambda *args, **kwargs: EndpointsOnly())
    monkeypatch.setattr(module, "_bounded_json_get", lambda url, **kwargs:
        {"models": [{"name": "synthetic-model", "digest": "synthetic-digest"}]}
        if url.endswith("/api/tags") else {"version": "synthetic-runtime"})
    assert module.main() == (1 if retained == 2 else 0)
    receipt = json.loads(output.read_text())
    assert receipt["decisive_evidence_recall"] == {"support": True, "contradiction": True}
    assert receipt["original_statement_recall"] == {"recalled": retained, "total": 24}
    assert receipt["passed"] is (retained == 24)


@pytest.mark.parametrize("legacy", [False, True])
def test_saved_investigation_discloses_shared_unit_omission_in_page_and_exports(workspace, monkeypatch, tmp_path, legacy):
    client, bench, matter, _ = workspace

    class SplitThenDrop(AccountOnly):
        def generate(self, **kwargs):
            answer = super().generate(**kwargs)
            if not kwargs.get("working_context"):
                answer["claims"].append({"text": CORRECTION, "evidence_ids": ["S1"]})
            return answer

    bench.generator = GroundedGenerationService(SplitThenDrop())
    assert client.post(f"/matters/{matter.slug}/uploads", files=[("files",
        ("synthetic-dispatch.txt", (ACCOUNT + " " + CORRECTION).encode(), "text/plain"))]).status_code == 200
    citation = bench.search(matter, "dispatch crate", limit=1)[0]

    def search(*args, **kwargs):
        kwargs["retrieval_boundary"]["source_fingerprint"] = bench.workspace.source_availability_fingerprint(matter.matter_id)
        return (citation,)

    monkeypatch.setattr(bench, "_answer_search", search)
    job, _ = bench.workspace.queue_research_job(matter.matter_id, ACTOR,
        "What do the synthetic dispatch records say?", "Synthetic shared-unit findings",
        "research-request-" + "d" * 32)
    claimed = bench.workspace.claim_research_job("synthetic-accounting-worker")
    claimed = bench.workspace.set_research_plan(job.job_id,
        {"queries": ["synthetic dispatch"], "synthesis_version": 1}, 5)
    completed = bench._finish_research_job(claimed, bench._process_research_job(claimed, lambda: False))
    assert completed.result["hierarchical_synthesis"]["partial"]
    notice = "saved findings omitted or not confirmed retained: 1 (P1C2)"
    assert notice in completed.result["summary"]
    if legacy:
        result = deepcopy(completed.result)
        result["hierarchical_synthesis"] = legacy_checkpoint((result["passes"], result["evidence"]),
                                                            result["hierarchical_synthesis"])
        answer = synthesis_answer(result["hierarchical_synthesis"], result["evidence"])
        result["summary"] = answer.text
        result["answer"] = bench._answer_payload(answer, {"S1": citation})
        completed = replace(completed, result=result)
        with fixture_write(bench.workspace):
            bench.workspace.connection.execute("UPDATE workbench_research_job SET result_json=? WHERE job_id=?",
                                               (json.dumps(result), job.job_id))
        notice = "Historical checkpoint: finding-level retention was not measured."
    from case_intelligence.workspace_store import WorkspaceStore
    destination = tmp_path / "synthetic-clean-restore.sqlite"
    with sqlite3.connect(destination) as output:
        bench.workspace.connection.backup(output)
    original_workspace = bench.workspace
    restored = WorkspaceStore(destination)
    try:
        bench.workspace = restored
        completed = restored.research_job(matter.matter_id, ACTOR, completed.job_id)
        assert restored.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not restored.connection.execute("PRAGMA foreign_key_check").fetchall()
        response = client.get(f"/matters/{matter.slug}/research?job={job.job_id}")
        assert response.status_code == 200 and notice in response.text
        for format_name in ("json", "markdown", "docx"):
            artifact = bench.export_research_work_product(matter, completed, format_name)
            if format_name == "docx":
                with zipfile.ZipFile(io.BytesIO(artifact.body)) as archive:
                    text = archive.read("word/document.xml").decode()
            else:
                text = artifact.body.decode()
            assert notice in text
            if format_name == "json":
                hierarchy = json.loads(text)["investigation"]["hierarchical_synthesis"]
                if legacy:
                    assert hierarchy["version"] == 1
                    assert hierarchy["finding_retention_notice"] == notice
                else:
                    assert hierarchy["retained_findings"] == ["P1C1"]
                    assert hierarchy["omitted_findings"] == ["P1C2"]
                    assert hierarchy["issue"][0]["omitted_parents"] == ["P1C2"]
                    assert hierarchy["issue"][0]["claims"][0]["parents"] == ["P1C1"]
                assert "support_token" not in text
    finally:
        bench.workspace = original_workspace
        restored.close()
