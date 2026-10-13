"""Synthetic retrieval-to-generation regressions; no model-quality claim."""
import io
from dataclasses import asdict, replace
import hashlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from case_intelligence.workbench import create_workbench_app
from case_intelligence.workspace_store import WorkspaceStore

ACTOR = "development-taylor-morgan"
QUESTION = "Where is aquamarine?"
FACT = "The aquamarine is in the synthetic cedar cabinet."


class WindowReader:
    available = True

    def __init__(self):
        self.packets = []

    def generate(self, *, evidence, **kwargs):
        self.packets.append(evidence)
        claims = [dict(text=FACT, evidence_ids=[item.evidence_id])
                  for item in evidence if FACT in item.excerpt]
        return dict(answerable=bool(claims), claims=claims, limitation=None,
                    missing_information="" if claims else "The location was not in the supplied passages.")


@pytest.mark.parametrize("queued", [False, True])
def test_retrieved_late_answer_survives_generation_and_saved_citations(tmp_path, monkeypatch, queued):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    generator = WindowReader()
    app = create_workbench_app(tmp_path / "runtime", generator=generator, auth_mode="test")
    with TestClient(app) as client:
        bench = app.state.workbench
        bench.answers.close()
        bench.research.close()
        bench.full_review.close()
        matter = bench.create_matter("Synthetic evidence window", "", ACTOR)
        text = "Synthetic filler. " * 480 + FACT + " More synthetic filler." * 160
        document, _ = bench.source_store(matter).store_stream(
            "synthetic-cabinet.txt", "text/plain", io.BytesIO(text.encode()))
        bench.index_document(matter, document.document_id)
        citations = bench.search(matter, QUESTION)
        assert len(citations) == 1 and FACT in citations[0].excerpt
        assert citations[0].excerpt.index(FACT) > 8_000
        conversation = bench.workspace.get_conversation(matter.matter_id)
        if queued:
            bench.queue_answer(matter, conversation, QUESTION, "answer-request-" + "a" * 32, ACTOR)
            job = bench.workspace.claim_answer_job("synthetic-window-worker")
            result = bench._process_answer_job(job, lambda *args: None, lambda: False)
            message = bench._finish_answer_job(job, result)
        else:
            message = bench.ask(matter, conversation, QUESTION)
        assert message.payload["answerable"], message.payload
        assert message.payload["claims"][0]["text"] == FACT
        original = citations[0]
        saved = message.payload["claims"][0]["citations"][0]
        for key in ("document_id", "source_version_id", "chunk_id", "location", "excerpt_digest", "support_token"):
            assert saved[key] == getattr(original, key)
        assert bench._current_workflow_citation(matter, original) == original
        window = message.payload["evidence_windows"]["passages"][0]
        assert window["start"] > 0
        assert generator.packets[0][0].excerpt == text[window["start"]:window["end"]]
        assert window["omitted_chars"] == len(text) - len(generator.packets[0][0].excerpt)
        assert len(generator.packets[0][0].excerpt) <= 6_000
        notice = message.payload["evidence_windows"]["notice"]
        assert "omitted" in notice
        for suffix in ("", "/assistant"):
            page = client.get(f"/matters/{matter.slug}{suffix}")
            assert notice in page.text
        export = client.get(f"/matters/{matter.slug}/conversations/{conversation.conversation_id}/export?format=markdown")
        assert export.status_code == 200 and notice in export.text
        reopened = WorkspaceStore(bench.workspace.path)
        try:
            assert reopened.messages(matter.matter_id, conversation.conversation_id)[-1] == message
        finally:
            reopened.close()


def packet_for(texts, question=QUESTION, context=""):
    from case_intelligence.answer_evidence import answer_evidence_packet
    citations = {f"S{i}": SimpleNamespace(source_name=f"Synthetic {i}.txt", location="Line 1",
        excerpt=text, evidence_kind="document", document_id=f"synthetic-{i}")
        for i, text in enumerate(texts, 1)}
    return answer_evidence_packet(question, citations, context=context)


@pytest.mark.parametrize("count", [1, 8, 12])
def test_packet_caps_preserve_late_matches_in_every_admitted_slot(count):
    texts = ["Synthetic filler. " * 480 + FACT + " Later filler." * 400] * count
    packet, metadata = packet_for(texts)
    assert len(packet) == count
    assert sum(len(item.excerpt) for item in packet) <= 48_000
    for item, row, original in zip(packet, metadata["passages"], texts):
        assert FACT in item.excerpt and len(item.excerpt) <= 6_000
        assert item.excerpt == original[row["start"]:row["end"]]
    assert metadata["omitted_chars"] == sum(len(t) for t in texts) - sum(len(i.excerpt) for i in packet)


def test_short_passages_keep_complete_text_and_reallocate_unused_room():
    long = "Synthetic filler. " * 480 + FACT + " Later filler." * 400
    packet, metadata = packet_for([FACT] * 4 + [long] * 8)
    assert all(item.excerpt == FACT for item in packet[:4])
    assert all(FACT in item.excerpt and len(item.excerpt) > 5_900 for item in packet[4:])
    short, unchanged = packet_for([FACT])
    assert short[0].excerpt == FACT
    assert unchanged["notice"] == "" and unchanged["omitted_chars"] == 0


def test_unicode_offsets_and_current_question_outrank_previous_context():
    text = "Straße background. " * 500 + FACT + " Synthetic filler. " * 450
    packet, metadata = packet_for([text], context="Straße background")
    row = metadata["passages"][0]
    assert FACT in packet[0].excerpt
    assert packet[0].excerpt == text[row["start"]:row["end"]]
    unicode_text = "Straße filler. " * 700 + "Der Schlüssel ist aquamarin." + " More filler." * 100
    packet, metadata = packet_for([unicode_text], question="Wo ist der SCHLÜSSEL?")
    row = metadata["passages"][0]
    assert "Der Schlüssel ist aquamarin." in packet[0].excerpt
    assert packet[0].excerpt == unicode_text[row["start"]:row["end"]]


def test_unmatched_passage_uses_disclosed_contiguous_prefix():
    text = "Synthetic filler. " * 500
    packet, metadata = packet_for([text])
    assert text.startswith(packet[0].excerpt)
    assert metadata["passages"][0]["start"] == 0
    assert "without literal matches" in metadata["notice"]


def test_omitted_text_cannot_ground_an_answer():
    from case_intelligence.generation import GroundedGenerationService, GenerationGroundingRejected
    secret = "The synthetic zircon is in the steel cabinet."
    text = secret + " Synthetic filler." * 600 + FACT + " More filler." * 400
    packet, metadata = packet_for([text])
    assert secret not in packet[0].excerpt and metadata["omitted_chars"] > 0
    client = SimpleNamespace(available=True, generate=lambda **kwargs: dict(answerable=True,
        claims=[dict(text='The record states "synthetic zircon is in the steel cabinet".',
                     evidence_ids=["S1"])], limitation=None, missing_information=""))
    with pytest.raises(GenerationGroundingRejected):
        GroundedGenerationService(client).answer(QUESTION, packet)


@pytest.mark.parametrize("queued", [False, True])
def test_edit_outside_supplied_window_blocks_answer_save(tmp_path, monkeypatch, queued):
    from case_intelligence.answer_jobs import AnswerJobFailure
    from case_intelligence.workspace_store import WorkspaceProblem
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    generator = WindowReader()
    app = create_workbench_app(tmp_path / "runtime", generator=generator, auth_mode="test")
    with TestClient(app):
        bench = app.state.workbench
        bench.answers.close()
        matter = bench.create_matter("Synthetic window revalidation", "", ACTOR)
        store = bench.source_store(matter)
        text = "Synthetic filler. " * 480 + FACT + " More synthetic filler." * 160
        document, _ = store.store_stream("synthetic-cabinet.txt", "text/plain", io.BytesIO(text.encode()))
        bench.index_document(matter, document.document_id)
        generate = generator.generate
        def mutate(**kwargs):
            result = generate(**kwargs)
            assert FACT in kwargs["evidence"][0].excerpt
            with store.mutation_guard():
                unit = document.parsed_units()[0]
                changed = "Changed prefix. " + text
                document.units = [asdict(replace(unit, text=changed,
                    excerpt_digest=hashlib.sha256(changed.encode()).hexdigest()))]
                store._save()
            return result
        generator.generate = mutate
        conversation = bench.workspace.get_conversation(matter.matter_id)
        with pytest.raises((AnswerJobFailure, WorkspaceProblem), match="source changed"):
            if queued:
                bench.queue_answer(matter, conversation, QUESTION, "answer-request-" + "b" * 32, ACTOR)
                job = bench.workspace.claim_answer_job("synthetic-window-worker")
                result = bench._process_answer_job(job, lambda *args: None, lambda: False)
                bench._finish_answer_job(job, result)
            else:
                bench.ask(matter, conversation, QUESTION)
        assert [m.role for m in bench.workspace.messages(matter.matter_id, conversation.conversation_id)] == ["user"]
