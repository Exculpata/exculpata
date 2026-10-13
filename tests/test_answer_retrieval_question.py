"""Synthetic answer composition preserves the current question through retrieval."""
import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.workbench import (
    MAX_SEARCH_CHARS, CaseIntelligenceWorkbench, create_workbench_app,
)


ACTOR = "development-taylor-morgan"
STATEMENT = "Aquamarine rests in the cedar drawer."
QUESTION = "Where is aquamarine?"


class EchoGenerator:
    available = True

    def __init__(self):
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "answerable": bool(kwargs["evidence"]),
            "claims": [
                {"text": item.excerpt, "evidence_ids": [item.evidence_id]}
                for item in kwargs["evidence"]
            ],
            "limitation": None,
            "missing_information": "",
        }


@pytest.mark.parametrize("durable,notebook,long_question", [
    (False, False, False),
    (True, False, False),
    (True, True, False),
    (False, False, "contextual"),
    (True, True, "contextual"),
    (True, True, "standalone"),
    (True, True, "summary"),
])
def test_current_question_reaches_real_search_and_saved_cited_answer(
    tmp_path, monkeypatch, durable, notebook, long_question,
):
    generator = EchoGenerator()
    app = create_workbench_app(tmp_path / "runtime", auth_mode="test", generator=generator,
        storage_policy=StoragePolicy(reserve_bytes=0))
    with TestClient(app):
        bench = app.state.workbench
        # Own the durable job explicitly; do not race the answer coordinator.
        bench.answers.close()
        bench.answers = None
        bench.research.close()
        bench.full_review.close()
        matter = bench.create_matter("Synthetic retrieval continuity", "", ACTOR)
        document, _ = bench.source_store(matter).store_stream(
            "Synthetic drawer note.txt", "text/plain", io.BytesIO(STATEMENT.encode()))
        bench._sync_source_catalog(matter, [document])
        conversation = bench.workspace.get_conversation(matter.matter_id)
        previous = "zircon " * 100
        bench.workspace.append_message(matter.matter_id, conversation.conversation_id,
            "user", previous.strip())
        bench.workspace.append_message(matter.matter_id, conversation.conversation_id,
            "assistant", "Synthetic earlier answer.")
        if notebook:
            bench.workspace.create_notebook_item(matter.matter_id, ACTOR,
                item_type="note", status="confirmed", title="Synthetic notebook context",
                body="topaz " * 200, origin="manual")
        question = QUESTION
        if long_question:
            prefix = {"contextual": "Review this ", "standalone": "Locate ",
                      "summary": "Give a broad summary of "}[long_question]
            padding = ("zircon " * 300)[:2000 - len(prefix) - len(QUESTION) - 1]
            question = prefix + padding + " " + QUESTION
            assert len(question) == 2000
        calls = []
        search = bench.search

        def recorded_search(matter, query, **kwargs):
            calls.append((query, kwargs["limit"]))
            return search(matter, query, **kwargs)

        monkeypatch.setattr(bench, "search", recorded_search)
        if durable:
            job, _ = bench.queue_answer(matter, conversation, question,
                "answer-request-" + "a" * 32, ACTOR,
                notebook_mode="confirmed" if notebook else None)
            claimed = bench.workspace.claim_answer_job("synthetic-retrieval-worker")
            assert claimed.job_id == job.job_id
            result = bench._process_answer_job(claimed, lambda *_: None, lambda: False)
            bench._finish_answer_job(claimed, result)
            saved = bench.workspace.messages(matter.matter_id, conversation.conversation_id)[-1]
        else:
            saved = bench.ask(matter, conversation, question)
        assert calls and all(len(query) <= MAX_SEARCH_CHARS for query, _ in calls)
        assert any(QUESTION in query for query, _ in calls)
        assert sum(limit for _, limit in calls) <= 20
        if long_question:
            assert " ".join(query for query, _ in calls) == " ".join(question.split())
        assert saved.role == "assistant"
        assert saved.payload["answerable"] is True
        assert saved.payload["claims"][0]["text"] == STATEMENT
        reference = saved.payload["claims"][0]["citations"][0]
        assert reference["document_id"] == document.document_id
        assert bench.support(matter, reference["support_token"]).lines == (STATEMENT,)
        assert generator.calls[-1]["question"] == " ".join(question.split())
        assert generator.calls[-1]["history"][-2:] == (
            ("user", previous.strip()), ("assistant", "Synthetic earlier answer."))
        if notebook:
            assert "Synthetic notebook context" in generator.calls[-1]["working_context"]


@pytest.mark.parametrize("length", [511, 512, 513, 2000])
def test_current_question_boundary_uses_history_only_when_it_fits(length):
    question = "this " + "x" * (length - 5)
    messages = [SimpleNamespace(role="user", content="older " * 200),
                SimpleNamespace(role="assistant", content="ignored assistant answer")]
    query = CaseIntelligenceWorkbench._retrieval_question(question, messages)
    assert query == question
    assert CaseIntelligenceWorkbench._retrieval_question(QUESTION, messages).endswith(QUESTION)


def test_short_followup_keeps_previous_user_context_and_standalone_does_not():
    messages = [SimpleNamespace(role="user", content="Locate the synthetic cedar drawer."),
                SimpleNamespace(role="assistant", content="The earlier synthetic answer.")]
    assert CaseIntelligenceWorkbench._retrieval_question(QUESTION, messages) == (
        "Locate the synthetic cedar drawer. " + QUESTION)
    assert CaseIntelligenceWorkbench._retrieval_question(QUESTION, ()) == QUESTION
    standalone = "Which synthetic drawer contains the aquamarine sample?"
    assert CaseIntelligenceWorkbench._retrieval_question(standalone, messages) == standalone


@pytest.mark.parametrize("primary_limit", [1, 20])
def test_long_question_windows_keep_candidate_and_modality_scope_limits(primary_limit):
    class SearchHarness:
        def __init__(self):
            self.calls = []

        def search(self, matter, query, *, limit, document_ids, stage_callback=None):
            self.calls.append((query, limit, document_ids, stage_callback))
            kind = "transcript" if document_ids == frozenset({"recording"}) else "document"
            return tuple(SimpleNamespace(
                evidence_kind=kind, support_token=f"{kind}-{len(self.calls)}-{index}",
                document_id="recording" if kind == "transcript" else "report",
            ) for index in range(limit))

        def source_store(self, matter):
            return SimpleNamespace(ready_documents=lambda: (
                SimpleNamespace(document_id="recording", media_type="audio/wav"),
                SimpleNamespace(document_id="outside-scope", media_type="audio/wav"),
                SimpleNamespace(document_id="report", media_type="text/plain"),
            ))

    # Seven indivisible words are a worst case for the legal question limit.
    question = " ".join("x" * 257 for _ in range(7)) + (
        " Using both the written report and spoken recording, where is aquamarine?")
    assert len(question) <= 2000
    harness = SearchHarness()
    scope = frozenset({"report", "recording"})
    callback = lambda _: None
    if primary_limit == 1:
        with pytest.raises(ValueError, match="must cover all current-question windows"):
            CaseIntelligenceWorkbench._answer_search(harness, SimpleNamespace(),
                question, question, document_ids=scope, primary_limit=primary_limit)
        assert harness.calls == []
        return
    rows = CaseIntelligenceWorkbench._answer_search(harness, SimpleNamespace(),
        question, question, document_ids=scope, primary_limit=primary_limit,
        stage_callback=callback)
    primary = [call for call in harness.calls if call[2] == scope]
    supplemental = [call for call in harness.calls if call[2] == frozenset({"recording"})]
    assert len(primary) == len(supplemental) == 7
    assert all(len(query) <= 512 for query, *_ in harness.calls)
    assert " ".join(query for query, *_ in primary) == question
    assert " ".join(query for query, *_ in supplemental) == question
    assert sum(limit for _, limit, *_ in primary) == primary_limit
    assert sum(limit for _, limit, *_ in supplemental) == 12
    assert sum(row.evidence_kind == "document" for row in rows) == primary_limit
    assert sum(row.evidence_kind == "transcript" for row in rows) == 12
    assert primary[0][3] is callback
    assert all(call[3] is None for call in harness.calls[1:])
