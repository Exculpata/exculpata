"""Only the reviewed standalone evaluation inputs may reach the configured external generator."""
import io
import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

from case_intelligence import defense_eval, generation, generator_transport, question_draft_evaluation
from case_intelligence.generation_errors import GenerationApprovalRequired
from case_intelligence.generation_matter import generation_matter_scope


@pytest.fixture
def requests(monkeypatch):
    endpoint = "https://answers.example.test/tenant-a"
    model = "synthetic-answer"
    for key, value in {"BACKEND": "openai", "URL": endpoint, "MODEL": model,
                       "ALLOWED_HOSTS": "answers.example.test", "SCHEMA_PROBE": "0"}.items():
        monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_" + key, value)
    records = []

    def receive(request, **_):
        if request.get_method() == "GET":
            response = {"data": [{"id": model}]}
        else:
            assert request.full_url == endpoint + "/v1/chat/completions"
            packet = json.loads(request.data)
            assert packet["model"] == model
            if not records:
                # Another caller in this thread cannot inherit the standalone client's authority.
                with pytest.raises(GenerationApprovalRequired):
                    generation._bounded_json_request(request.full_url, {"model": model, "messages": []}, timeout=1)
            records.append(packet)
            answer = {"answerable": False, "claims": [], "limitation": None, "missing_information": ""}
            if "questions" in packet["response_format"]["json_schema"]["schema"]["properties"]:
                answer = {"questions": []}
            response = {"choices": [{"message": {"content": json.dumps(answer)}}]}
        return io.BytesIO(json.dumps(response).encode())

    monkeypatch.setattr(generator_transport, "open_generator_request", receive)
    return records


def run_question_cli(path):
    root = Path(__file__).resolve().parents[1]
    command = runpy.run_path(str(root / "scripts/evaluate-question-drafts.py"))["main"]
    return command(["--cases", str(path)])


@pytest.mark.parametrize("corpus", ["questions", "defense"])
def test_only_pinned_standalone_corpora_can_send_without_a_matter(corpus, tmp_path, requests):
    if corpus == "questions":
        path = question_draft_evaluation.DEFAULT_CASES
        run = run_question_cli
    else:
        path = defense_eval.DEFAULT_SUITE
        def run(path):
            return defense_eval.main(["--mode", "configured", "--suite", str(path), "--judge",
                                      "--output-dir", str(tmp_path / "out"), "--force"])
    run(path)
    assert requests
    if corpus == "defense":
        assert any("Judge the candidate" in str(packet["messages"]) for packet in requests)
    requests.clear()

    # A copied real pin or a recomputed self-consistent manifest cannot attest to new content.
    data = json.loads(path.read_bytes())
    if corpus == "questions":
        data["cases"][0]["topic"] = "Synthetic private topic"
    else:
        data["questions"][0]["question"] = "Synthetic private question"
        data["fingerprint"] = defense_eval.suite_fingerprint(data)
    modified = tmp_path / "modified.json"
    # fixture-write: Build a synthetic evaluation manifest with changed input.
    modified.write_text(json.dumps(data), encoding="utf-8")
    if corpus == "defense":
        assert defense_eval.load_suite(modified).questions[0].text == "Synthetic private question"
    run(modified)
    assert requests == []

    workspace = SimpleNamespace(model_grant_allows=lambda matter, destination: False)
    with generation_matter_scope(workspace, "denied"):
        run(path)
    assert requests == []

    with pytest.raises(GenerationApprovalRequired):
        generation._bounded_json_request("https://answers.example.test/tenant-a/v1/chat/completions",
                                         {"model": "synthetic-answer", "messages": []}, timeout=1)
