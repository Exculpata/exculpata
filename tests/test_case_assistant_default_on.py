"""Synthetic regressions for default-on Case Assistant activation and opt-out."""
from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient

from case_intelligence.case_assistant import AssistantModelCall, AssistantTurn
from case_intelligence.generation import OllamaGenerator, OpenAICompatibleGenerator, UnavailableGenerator
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workbench_assistant import case_assistant_enabled, supports_case_assistant
from tests.test_milestone_a_workbench import _answer_and_wait, _create_matter


class _OneStepAssistantClient:
    available = True

    def __init__(self) -> None:
        self.calls = 0

    def complete_turn(self, messages, *, tools):
        self.calls += 1
        if self.calls == 1:
            return AssistantTurn(tool_calls=(AssistantModelCall("c1", "search_discovery", {"query": "harbor", "limit": 5}),))
        tool_payload = json.loads(messages[-1]["content"])
        hit = tool_payload["hits"][0]
        cite = {k: hit[k] for k in ("document_id", "source_name", "location", "source_version_id")}
        return AssistantTurn(content=json.dumps({
            "answerable": True,
            "summary": "The synthetic report mentions harbor delivery.",
            "findings": [{"text": "The report records a harbor delivery.", "citations": [cite], "calculation_ids": []}],
            "gaps": [],
            "competing_accounts": [],
        }))


def test_case_assistant_default_on_opt_out_and_zero_endpoint_safety(monkeypatch):
    openai_client = OpenAICompatibleGenerator("http://127.0.0.1:8000", "synthetic-model")
    ollama_client = OllamaGenerator("http://127.0.0.1:11434", "synthetic-model")
    unavail_client = UnavailableGenerator()
    scripted = _OneStepAssistantClient()

    assert supports_case_assistant(openai_client) is True
    assert supports_case_assistant(ollama_client) is True
    assert supports_case_assistant(scripted) is True
    assert supports_case_assistant(unavail_client) is False
    assert supports_case_assistant(None) is False

    # Outside test mode with env unset, OpenAI/Ollama endpoints enable Case Assistant by default.
    monkeypatch.delenv("CASE_INTELLIGENCE_CASE_ASSISTANT", raising=False)
    monkeypatch.delenv("CASE_INTELLIGENCE_TEST_MODE", raising=False)
    assert case_assistant_enabled() is True
    assert case_assistant_enabled(openai_client) is True
    assert case_assistant_enabled(ollama_client) is True
    assert case_assistant_enabled(unavail_client) is False

    # Explicit opt-out disables Case Assistant even outside test mode or on complete_turn clients.
    for falsy in ("0", "false", "off", "no", "unexpected"):
        monkeypatch.setenv("CASE_INTELLIGENCE_CASE_ASSISTANT", falsy)
        assert case_assistant_enabled(openai_client) is False
        assert case_assistant_enabled(scripted) is False

    # Explicit opt-in still protects zero-endpoint UnavailableGenerator installations.
    monkeypatch.setenv("CASE_INTELLIGENCE_CASE_ASSISTANT", "1")
    assert case_assistant_enabled(openai_client) is True
    assert case_assistant_enabled(unavail_client) is False

    # Under test mode with env unset, legacy single-turn stubs stay off while explicit complete_turn runs.
    monkeypatch.delenv("CASE_INTELLIGENCE_CASE_ASSISTANT", raising=False)
    monkeypatch.setenv("CASE_INTELLIGENCE_TEST_MODE", "1")
    assert case_assistant_enabled(openai_client) is False
    assert case_assistant_enabled(scripted) is True


def test_http_case_assistant_runs_without_env_flag_and_zero_endpoint_stays_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    monkeypatch.delenv("CASE_INTELLIGENCE_CASE_ASSISTANT", raising=False)

    # Tool-capable AssistantModelClient works over HTTP without setting CASE_INTELLIGENCE_CASE_ASSISTANT=1.
    client_stub = _OneStepAssistantClient()
    app = create_workbench_app(tmp_path / "runtime-assistant", generator=client_stub, auth_mode="test")
    with TestClient(app) as client:
        slug = _create_matter(client, "Default On Matter")
        up = client.post(f"/matters/{slug}/uploads", files=[("files", ("note.txt", b"Harbor delivery logged on Tuesday.\n", "text/plain"))])
        assert up.status_code == 200
        workspace = client.get(f"/matters/{slug}")
        conv_id = re.search(r'name="conversation" value="([^"]+)"', workspace.text).group(1)
        _, page = _answer_and_wait(client, slug, conv_id, "What happened at the harbor?")
        assert page.status_code == 200
        assert "Work trace" in page.text and "harbor delivery" in page.text
        assert client_stub.calls == 2

    # Zero-endpoint UnavailableGenerator stays on the normal fallback path even with CASE_INTELLIGENCE_CASE_ASSISTANT=1.
    monkeypatch.setenv("CASE_INTELLIGENCE_CASE_ASSISTANT", "1")
    zero_app = create_workbench_app(tmp_path / "runtime-zero", generator=UnavailableGenerator(), auth_mode="test")
    with TestClient(zero_app) as client:
        slug = _create_matter(client, "Zero Endpoint Matter")
        up = client.post(f"/matters/{slug}/uploads", files=[("files", ("note.txt", b"Harbor delivery logged on Tuesday.\n", "text/plain"))])
        assert up.status_code == 200
        workspace = client.get(f"/matters/{slug}")
        conv_id = re.search(r'name="conversation" value="([^"]+)"', workspace.text).group(1)
        job, page = _answer_and_wait(
            client, slug, conv_id, "What happened at the harbor?", terminal_states=("failed",),
        )
        assert page.status_code == 200
        assert "does not support the Case Assistant tool loop" not in (job.get("error") or "") + page.text


def test_bundled_vllm_tool_calling_flags_and_shared_generation_admission():
    from pathlib import Path
    import threading
    from types import SimpleNamespace
    from case_intelligence.case_assistant import OpenAIAssistantClient
    from case_intelligence.workbench_assistant import _AdmittedAssistantClient

    # P1: Bundled vLLM generator service enables auto tool choice and a Qwen3.5 tool-call parser.
    compose = (Path(__file__).resolve().parents[1] / "compose.yaml").read_text(encoding="utf-8")
    generator_block = compose.split("\n  generator:\n", 1)[1].split("\n  transcription-api:\n", 1)[0]
    assert "--enable-auto-tool-choice" in generator_block
    assert "--tool-call-parser" in generator_block
    assert "qwen3_coder" in generator_block

    # Verify OpenAIAssistantClient sends tool_choice="auto" and parses structured tool_calls.
    import io
    captured: list[dict] = []
    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): return False
    def _opener(req, timeout=None):
        captured.append(json.loads(req.data.decode("utf-8")))
        body = json.dumps({"choices": [{"message": {"tool_calls": [{"id": "tc1", "function": {"name": "list_sources", "arguments": "{}"}}]}}]}).encode()
        return _Resp(body)
    wire = OpenAIAssistantClient("http://127.0.0.1:8000", "exculpata-qwen35-4b", opener=SimpleNamespace(open=_opener))
    turn = wire.complete_turn([{"role": "user", "content": "Check sources"}], tools=[{"type": "function", "function": {"name": "list_sources"}}])
    assert captured[0]["tool_choice"] == "auto" and turn.tool_calls[0].name == "list_sources"

    # P2: _AdmittedAssistantClient enforces GroundedGenerationService._admission concurrency limit.
    active, peak = 0, 0
    guard = threading.Lock()
    entered = threading.Event()
    release = threading.Event()

    class _BlockingClient:
        def complete_turn(self, messages, *, tools):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            if not entered.is_set():
                entered.set()
                release.wait(timeout=2)
            with guard:
                active -= 1
            return AssistantTurn(content="{}")

    service = SimpleNamespace(_admission=threading.BoundedSemaphore(1), _counter_lock=threading.Lock(),
                              requests_started=0, requests_completed=0)
    admitted = _AdmittedAssistantClient(_BlockingClient(), service)
    t1 = threading.Thread(target=lambda: admitted.complete_turn([], tools=[]))
    t2 = threading.Thread(target=lambda: admitted.complete_turn([], tools=[]))
    t1.start()
    assert entered.wait(timeout=2)
    t2.start()
    release.set()
    t1.join(timeout=2)
    t2.join(timeout=2)
    assert peak == 1 and service.requests_started == 2 and service.requests_completed == 2

