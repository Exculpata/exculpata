"""Harness failures are not retrieval or model quality thresholds."""
from __future__ import annotations

import json
import io

import pytest

from case_intelligence import defense_eval as evaluation


def test_cli_retains_error_card_and_fails_on_answerer_errors(monkeypatch, tmp_path):
    def broken(_self, question, passages):
        raise RuntimeError("synthetic harness failure")

    monkeypatch.setattr(evaluation.ReferenceStub, "__call__", broken)
    assert evaluation.main(["--mode", "stub", "--output-dir", str(tmp_path)]) == 2
    card = json.loads((tmp_path / "scorecard.json").read_text())
    assert card["aggregates"]["errors"] == len(card["questions"])
    assert all(row["error"] == "RuntimeError" for row in card["questions"])


@pytest.mark.parametrize("quote", ["SYNTHETIC", "teal lantern cases"])
def test_unrelated_or_partial_quote_does_not_support_reference_fact(quote):
    passage = evaluation.Passage("p", "matter", "p", "0" * 64, "document", "1",
                                 "SYNTHETIC: counted twelve teal lantern cases")
    question = evaluation.Question(
        "q", "matter", "fact_lookup", "How many?", False,
        (evaluation.Fact("count", "twelve", (
            evaluation.Support("p", "counted twelve teal lantern cases"),)),), (),
    )
    answer = evaluation.ModelAnswer(False, "twelve", (evaluation.Citation("p", quote),))
    row = evaluation.score_question(question, answer, (passage,), {"p": passage}, 0)
    assert row["citation_support"] is False
    assert row["missed_facts"] == ["count"]


def test_scorecard_counts_unsupported_citations_and_missing_citations(tmp_path):
    suite = evaluation.load_suite()

    def unsupported(question, passages):
        return evaluation.ModelAnswer(False, "invented", (evaluation.Citation("absent", "invented"),))

    card = evaluation.run_eval(suite, unsupported)
    assert card["aggregates"]["unsupported_citation_count"] == len(suite.questions)
    assert all(row["unsupported_citation_count"] == 1 for row in card["questions"])
    evaluation.write_scorecard(tmp_path, card)
    assert "Unsupported citations: 42" in (tmp_path / "scorecard.md").read_text()
    question = next(item for item in suite.questions if not item.expect_abstention)
    row = evaluation.score_question(
        question, evaluation.ModelAnswer(False, question.required_facts[0].answer_text, ()),
        (), {p.passage_id: p for p in suite.passages}, 0,
    )
    assert row["citation_support"] is False
    assert row["missed_facts"]
    assert row["unsupported_citation_count"] == 0


def test_worker_health_uses_approved_service_transport(monkeypatch):
    import urllib.request

    calls = []

    def approved(url, **kwargs):
        calls.append((url, kwargs))
        return io.BytesIO(b'{"status":"ok","embedding_loaded":true,"reranker_loaded":true}')

    def direct(*args, **kwargs):
        raise AssertionError("worker health must use the approved service transport")

    monkeypatch.setenv("CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(evaluation, "open_service_request", approved, raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", direct)
    assert evaluation._healthy_retrieval_worker() == "http://127.0.0.1:9"
    assert calls == [("http://127.0.0.1:9/health", {
        "timeout": 2, "environment_name": "CASE_INTELLIGENCE_RETRIEVAL_ALLOWED_HOSTS",
    })]


def test_score_changes_are_successful_cli_measurements(monkeypatch, tmp_path):
    monkeypatch.setattr(evaluation, "rank_passages", lambda passages, query, top_k: ())
    assert evaluation.main(["--mode", "stub", "--output-dir", str(tmp_path)]) == 0
    card = json.loads((tmp_path / "scorecard.json").read_text())
    assert card["aggregates"]["retrieval_hit"] == 0
    assert card["aggregates"]["answer_correct"] < card["aggregates"]["questions"]
    assert card["aggregates"]["errors"] == 0


@pytest.mark.parametrize("backend", ["openai", "ollama"])
def test_configured_cli_uses_real_http_and_writes_scorecards(backend, monkeypatch, tmp_path):
    """A synthetic HTTP service proves transport, not learned-model quality."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def reply(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if backend == "openai":
                self.reply({"data": [{"id": "synthetic-eval"}]})
            else:
                self.reply({"models": [{"name": "synthetic-eval"}]})

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, payload))
            content = json.dumps({
                "answerable": False, "claims": [], "limitation": None,
                "missing_information": "The supplied sources do not answer the question.",
            })
            message = {"content": content}
            self.reply({"choices": [{"message": message}]} if backend == "openai"
                       else {"message": message})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_BACKEND", backend)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_URL", endpoint)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_MODEL", "synthetic-eval")
    monkeypatch.delenv("CASE_INTELLIGENCE_GENERATOR_API_KEY", raising=False)
    try:
        assert evaluation.main(["--mode", "configured", "--output-dir", str(tmp_path)]) == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    card = json.loads((tmp_path / "scorecard.json").read_text())
    assert len(requests) == len(card["questions"]) == len(evaluation.load_suite().questions)
    assert all(payload["model"] == "synthetic-eval" for _, payload in requests)
    expected_path = "/v1/chat/completions" if backend == "openai" else "/api/chat"
    assert all(path == expected_path for path, _ in requests)
    assert card["model"]["mode"] == "configured"
    assert card["model"]["backend"] == backend
    assert card["aggregates"]["errors"] == 0
    assert all(row["abstained"] for row in card["questions"])
    assert endpoint not in (tmp_path / "scorecard.json").read_text()
    assert (tmp_path / "scorecard.md").is_file()


def comparison_card():
    return {"fingerprint": "0" * 64, "suite_version": "2", "questions": [{
        "question_id": "q", "type": "fact_lookup", "answer_correct": True,
        "citation_support": True, "abstention_correct": True, "retrieval_hit": True,
    }]}


@pytest.mark.parametrize("metric", ["answer_correct", "citation_support", "abstention_correct", "retrieval_hit"])
def test_comparison_rejects_missing_scores(metric, tmp_path):
    card = comparison_card()
    del card["questions"][0][metric]
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(card))
    assert evaluation.main(["--compare", str(path), str(path)]) == 2


@pytest.mark.parametrize("value", [None, 1, "true"])
def test_comparison_rejects_non_boolean_scores(value, tmp_path):
    card = comparison_card()
    card["questions"][0]["answer_correct"] = value
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(card))
    assert evaluation.main(["--compare", str(path), str(path)]) == 2


@pytest.mark.parametrize("metadata", ["fingerprint", "suite_version"])
def test_comparison_rejects_missing_metadata(metadata, tmp_path):
    card = comparison_card()
    del card[metadata]
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(card))
    assert evaluation.main(["--compare", str(path), str(path)]) == 2


@pytest.mark.parametrize("duplicate", [False, True], ids=["empty", "duplicate-id"])
def test_comparison_rejects_empty_or_duplicate_questions(duplicate, tmp_path):
    card = comparison_card()
    card["questions"] *= 2 if duplicate else 0
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(card))
    assert evaluation.main(["--compare", str(path), str(path)]) == 2
