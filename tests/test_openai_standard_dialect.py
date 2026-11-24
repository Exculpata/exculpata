"""Synthetic regressions for the standard OpenAI Chat Completions dialect."""
from __future__ import annotations

from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from case_intelligence import claim_evaluation, external_generator, generator_config, generator_schema
from case_intelligence.case_assistant import OpenAIAssistantClient
from case_intelligence.external_model_grants import ModelDestination, configured_model_destination
from case_intelligence.generation import (
    EvidenceItem,
    GenerationSchemaRejected,
    GroundedGenerationService,
    OpenAICompatibleGenerator,
    generator_from_environment,
)
from case_intelligence.generator_schema import PROBE_MARKER, PROBE_SCHEMA, probe_schema_enforcement, schema_chat_request
from case_intelligence.workbench_assistant import assistant_client_from_generator
from case_intelligence.workspace_store import WorkspaceProblem


_FORBIDDEN_STRICT_SCHEMA_KEYS = frozenset({
    "minLength", "maxLength", "minItems", "maxItems", "pattern", "const",
})
_SYNTHETIC_EVIDENCE = (
    EvidenceItem(
        "S1",
        "Synthetic dispatch.txt",
        "Line 1",
        "Rowan Example carried a green box to the depot at 09:10 and returned at 09:12.",
    ),
)


def _find_forbidden_schema_keys(node: object, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key in _FORBIDDEN_STRICT_SCHEMA_KEYS:
                found.append(f"{path}.{key}")
            elif key == "properties" and isinstance(value, Mapping):
                for prop_name, prop_schema in value.items():
                    found.extend(_find_forbidden_schema_keys(prop_schema, f"{path}.properties.{prop_name}"))
            else:
                found.extend(_find_forbidden_schema_keys(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            found.extend(_find_forbidden_schema_keys(item, f"{path}[{idx}]"))
    return found


class _StrictOpenAIServer:
    """Loopback mock enforcing the hosted OpenAI Chat Completions wire contract."""

    def __init__(self, *, model: str = "synthetic-frontier", bad_answer_override: dict[str, object] | None = None) -> None:
        self.model = model
        self.bad_answer_override = bad_answer_override
        self.received: list[dict[str, object]] = []
        self.rejected: list[tuple[str, dict[str, object]]] = []
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                return

            def _send_json(self, status: int, body: dict[str, object]) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                if self.path == "/v1/models":
                    self._send_json(200, {"data": [{"id": server_ref.model}]})
                    return
                self._send_json(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:
                if self.path != "/v1/chat/completions":
                    self._send_json(404, {"error": {"message": "not found"}})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                server_ref.received.append(payload)

                if "chat_template_kwargs" in payload:
                    server_ref.rejected.append(("chat_template_kwargs", payload))
                    self._send_json(400, {"error": {"message": "Unknown parameter: 'chat_template_kwargs'."}})
                    return
                if "max_tokens" in payload:
                    server_ref.rejected.append(("max_tokens", payload))
                    self._send_json(400, {"error": {"message": "Unsupported parameter: 'max_tokens'."}})
                    return
                if "temperature" in payload:
                    server_ref.rejected.append(("temperature", payload))
                    self._send_json(400, {"error": {"message": "Unsupported parameter: 'temperature'."}})
                    return

                response_format = payload.get("response_format")
                if isinstance(response_format, Mapping):
                    limit = payload.get("max_completion_tokens")
                    if not isinstance(limit, int) or limit < 4096:
                        server_ref.rejected.append(("max_completion_tokens_too_small", payload))
                        self._send_json(400, {"error": {"message": "Reasoning headroom required."}})
                        return
                    schema = response_format.get("json_schema", {}).get("schema")
                    forbidden = _find_forbidden_schema_keys(schema)
                    if forbidden:
                        server_ref.rejected.append((f"schema:{','.join(forbidden)}", payload))
                        self._send_json(400, {"error": {"message": f"Unsupported schema keywords: {forbidden}"}})
                        return
                    name = response_format.get("json_schema", {}).get("name", "")
                    if name == "exculpata_schema_probe":
                        content: object = {"marker": PROBE_MARKER}
                    elif name == "case_answer":
                        content = server_ref.bad_answer_override or {
                            "answerable": True,
                            "claims": [
                                {
                                    "text": "Rowan Example carried a green box to the depot at 09:10.",
                                    "evidence_ids": ["S1"],
                                }
                            ],
                            "limitation": None,
                            "missing_information": "",
                        }
                    elif name == "source_review_decision":
                        content = {
                            "decision": "include",
                            "rationale": "Rowan Example carried a green box to the depot at 09:10.",
                            "evidence_ids": ["S1"],
                        }
                    else:
                        content = {
                            "questions": [
                                {"text": "What did Rowan Example carry to the depot at 09:10?", "evidence_ids": ["S1"]},
                                {"text": "Where did Rowan Example carry the green box at 09:10?", "evidence_ids": ["S1"]},
                                {"text": "When did Rowan Example return from the depot?", "evidence_ids": ["S1"]},
                            ]
                        }
                    self._send_json(
                        200,
                        {
                            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(content)}}],
                            "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130},
                        },
                    )
                    return

                # Case Assistant tool loop turn
                self._send_json(
                    200,
                    {
                        "choices": [
                            {
                                "finish_reason": "tool_calls",
                                "message": {
                                    "role": "assistant",
                                    "content": "",
                                    "tool_calls": [
                                        {
                                            "id": "call_1",
                                            "type": "function",
                                            "function": {"name": "list_sources", "arguments": "{}"},
                                        }
                                    ],
                                },
                            }
                        ]
                    },
                )

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> _StrictOpenAIServer:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@pytest.mark.parametrize("thinking", [False, True])
def test_strict_openai_server_passes_schema_probe(monkeypatch, thinking: bool) -> None:
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS", "127.0.0.1")
    with _StrictOpenAIServer() as server:
        probe = probe_schema_enforcement(server.url, "openai", server.model, thinking=thinking)
        assert probe.status == "verified", probe.detail
        assert server.rejected == []
        assert len(server.received) == 1
        body = server.received[0]
        assert "chat_template_kwargs" not in body
        assert "max_tokens" not in body
        assert "temperature" not in body
        assert body["max_completion_tokens"] >= 4160


def test_strict_openai_server_runs_answers_classification_and_question_drafts(monkeypatch) -> None:
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS", "127.0.0.1")
    with _StrictOpenAIServer() as server:
        client = OpenAICompatibleGenerator(
            server.url,
            server.model,
            schema_server="openai",
            schema_mode="verified",
        )
        assert client.available is True

        service = GroundedGenerationService(client)
        answer = service.answer("What did Rowan Example carry?", _SYNTHETIC_EVIDENCE)
        assert answer.answerable is True
        assert len(answer.claims) == 1
        assert "green box" in answer.claims[0].text

        classification = client.classify_source(
            criterion="Find mentions of a green box",
            include_guidance="Include depot transport.",
            exclude_guidance="Exclude unrelated notes.",
            evidence=_SYNTHETIC_EVIDENCE,
        )
        assert classification["decision"] == "include"

        draft = client.draft_questions(purpose="witness", topic="Depot transport", evidence=_SYNTHETIC_EVIDENCE)
        assert len(draft["questions"]) == 3

        assert server.rejected == []
        seen_names = {
            body["response_format"]["json_schema"]["name"]
            for body in server.received
            if isinstance(body.get("response_format"), Mapping)
        }
        assert {"case_answer", "source_review_decision", "drafted_questions"}.issubset(seen_names)
        for body in server.received:
            assert "chat_template_kwargs" not in body
            assert "max_tokens" not in body
            assert "temperature" not in body
            assert isinstance(body["max_completion_tokens"], int)


def test_openai_dialect_still_enforces_full_local_schema_bounds(monkeypatch) -> None:
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS", "127.0.0.1")
    oversized = {
        "answerable": True,
        "claims": [{"text": "A" * 850, "evidence_ids": ["BAD99"]}],
        "limitation": None,
        "missing_information": "",
    }
    with _StrictOpenAIServer(bad_answer_override=oversized) as server:
        client = OpenAICompatibleGenerator(
            server.url,
            server.model,
            schema_server="openai",
            schema_mode="verified",
        )
        with pytest.raises(GenerationSchemaRejected):
            client.generate(question="What did Rowan Example carry?", evidence=_SYNTHETIC_EVIDENCE)
        assert server.rejected == []


def test_strict_openai_server_case_assistant_omits_temperature(monkeypatch) -> None:
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS", "127.0.0.1")
    with _StrictOpenAIServer() as server:
        generator = OpenAICompatibleGenerator(
            server.url,
            server.model,
            schema_server="openai",
            schema_mode="verified",
        )
        assistant_client, style = assistant_client_from_generator(generator)
        assert style == "openai"
        assert isinstance(assistant_client, OpenAIAssistantClient)
        turn = assistant_client.complete_turn(
            [{"role": "user", "content": "List the synthetic sources."}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "list_sources",
                        "description": "List matter sources",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        )
        assert len(turn.tool_calls) == 1
        assert turn.tool_calls[0].name == "list_sources"
        assert server.rejected == []
        assert "temperature" not in server.received[0]
        assert "chat_template_kwargs" not in server.received[0]


def test_openai_server_configuration_and_grant_destination_preserve_remote_guards(monkeypatch) -> None:
    monkeypatch.setattr(external_generator, "addresses_for", lambda host: ())
    unconfirmed = external_generator.parse_generator_endpoint(
        "openai", "https://api.example.test", "synthetic-frontier", remote_confirmed=False,
    )
    assert unconfirmed.backend == "openai"
    assert unconfirmed.server == "openai"
    assert unconfirmed.remote_confirmed is False
    with pytest.raises(external_generator.EndpointRejected):
        external_generator.require_local_or_confirmed(unconfirmed)

    confirmed = external_generator.require_local_or_confirmed(
        external_generator.parse_generator_endpoint(
            "openai", "https://api.example.test", "synthetic-frontier", remote_confirmed=True,
        )
    )
    env = external_generator.environment_values(confirmed)
    assert env["CASE_INTELLIGENCE_GENERATOR_BACKEND"] == "openai"
    assert env["CASE_INTELLIGENCE_GENERATOR_SERVER"] == "openai"
    assert env["CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS"] == "api.example.test"

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_SCHEMA_PROBE", "0")
    generator_config.validate_generator_environment(env)
    assert generator_config.generator_server("openai") == "openai"
    client = generator_from_environment()
    assert isinstance(client, OpenAICompatibleGenerator)
    assert client.schema_server == "openai"
    assert configured_model_destination("generation") == ModelDestination(
        "generation", "https://api.example.test", "synthetic-frontier",
    )


def test_recorded_dispatch_fails_closed_for_openai_dialect(monkeypatch) -> None:
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS", "127.0.0.1")
    client = OpenAICompatibleGenerator(
        "http://127.0.0.1:8080", "synthetic-frontier", schema_server="openai", schema_mode="verified",
    )
    service = GroundedGenerationService(client)
    with pytest.raises(WorkspaceProblem, match="chat tokenizer"):
        service.answer("What did Rowan Example carry?", _SYNTHETIC_EVIDENCE, recorded_dispatch=object())


def test_claim_evaluation_request_settings_accepts_openai_dialect() -> None:
    settings = claim_evaluation.request_settings({"max_completion_tokens": 5296}, "OpenAICompatibleGenerator")
    assert settings == {"thinking": True, "output_tokens": 1200, "reasoning_tokens": 4096}


@pytest.mark.parametrize("server", ["vllm", "llamacpp", "lmstudio"])
def test_local_openai_servers_keep_existing_wire_contract(server: str) -> None:
    path, body = schema_chat_request(
        server,
        model="synthetic-local",
        schema=PROBE_SCHEMA,
        prompt="probe",
        name="exculpata_schema_probe",
        max_tokens=64,
        thinking=False,
    )
    assert path == "/v1/chat/completions"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["max_tokens"] == 64
    assert "max_completion_tokens" not in body
    assert body["temperature"] == 0
    assert body["response_format"]["json_schema"]["schema"] == PROBE_SCHEMA
    assert server in generator_schema.OPENAI_SERVERS


def test_installer_cli_accepts_openai_and_requires_remote_consent(monkeypatch) -> None:
    import importlib.util
    from pathlib import Path
    import sys

    installer_path = Path(__file__).parents[1] / "scripts" / "exculpata_install.py"
    monkeypatch.syspath_prepend(str(installer_path.parent))
    spec = importlib.util.spec_from_file_location("exculpata_install_openai_check", installer_path)
    assert spec is not None and spec.loader is not None
    installer = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, installer)
    spec.loader.exec_module(installer)
    monkeypatch.setattr(external_generator, "addresses_for", lambda host: ())

    denied = installer._parser().parse_args([
        "install", "--models", "none", "--non-interactive",
        "--external-generator", "openai",
        "--external-generator-url", "https://api.example.test",
        "--external-generator-model", "synthetic-frontier",
    ])
    with pytest.raises(RuntimeError, match="outside the local network"):
        installer._external_from_args(denied)

    allowed = installer._parser().parse_args([
        "install", "--models", "none", "--non-interactive",
        "--external-generator", "openai",
        "--external-generator-url", "https://api.example.test",
        "--external-generator-model", "synthetic-frontier",
        "--confirm-remote-generator",
    ])
    endpoint = installer._external_from_args(allowed)
    assert endpoint is not None
    assert (endpoint.server, endpoint.backend, endpoint.remote_confirmed) == ("openai", "openai", True)

