"""Synthetic request-boundary and static no-execution regressions (T1–T7)."""
import ast
import io
import json
from pathlib import Path

import pytest

from case_intelligence import generation, generator_schema, generator_transport
from case_intelligence.question_generation import QuestionGenerationProfile


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_KEYS = {"tools", "tool_choice", "functions", "function_call", "parallel_tool_calls"}
TOP_LEVEL_CASES = [("tools", []), ("tool_choice", "none"), ("functions", []),
                   ("function_call", "none"), ("parallel_tool_calls", False)]
MESSAGE_CASES = [{"role": "tool"}, {"role": "function"},
                 {"role": "assistant", "tool_calls": []},
                 {"role": "assistant", "function_call": None}]
CHAT_PATHS = ["/v1/chat/completions", "/api/chat"]
GENERATOR_MODULES = ["generation", "question_generation", "recorded_generation",
                     "generator_schema", "generator_transport"]


class RecordingOpener:
    def __init__(self, response=None):
        self.calls = []
        self.response = json.dumps(response or {}).encode()

    def open(self, request, **kwargs):
        self.calls.append(json.loads(request.data))
        return io.BytesIO(self.response)


def assert_refused(send, payload, opener):
    # Resolve the exact type after the call so baseline failures exercise transport.
    with pytest.raises(RuntimeError) as error:
        send(payload)
    assert type(error.value) is generator_schema.ToolRequestRefused
    assert not isinstance(error.value, generation.GenerationUnavailable)
    assert opener.calls == []


@pytest.mark.parametrize("path", CHAT_PATHS)
@pytest.mark.parametrize("key,value", TOP_LEVEL_CASES)
def test_t1_top_level_keys_are_refused(path, key, value):
    opener = RecordingOpener()
    assert_refused(lambda payload: generation._bounded_json_request(
        "http://127.0.0.1:8790" + path, payload, timeout=1, opener=opener),
        {"messages": [], key: value}, opener)


@pytest.mark.parametrize("path", CHAT_PATHS)
@pytest.mark.parametrize("message", MESSAGE_CASES)
def test_t2_tool_messages_are_refused(path, message):
    opener = RecordingOpener()
    assert_refused(lambda payload: generation._bounded_json_request(
        "http://127.0.0.1:8790" + path, payload, timeout=1, opener=opener),
        {"messages": [message]}, opener)


@pytest.mark.parametrize("path", CHAT_PATHS)
@pytest.mark.parametrize("payload", [
    {"tools": [{"type": "function", "function": {"name": "synthetic_tool"}}]},
    *[{key: value} for key, value in TOP_LEVEL_CASES],
    *[{"messages": [message]} for message in MESSAGE_CASES],
])
def test_t3_urllib_sender_refuses_tools(path, payload):
    opener = RecordingOpener()
    send = generator_schema._urllib_transport(1, open_request=opener.open)
    assert_refused(lambda body: send("http://127.0.0.1:8790" + path, body, {}),
                   payload, opener)


@pytest.mark.parametrize("sender", ["bounded", "schema"])
def test_t4_https_refused_before_generator_opener(monkeypatch, sender):
    def unexpected_open(*args, **kwargs):
        pytest.fail("HTTPS generator opener must not be called")

    monkeypatch.setattr(generator_transport, "open_generator_request", unexpected_open)
    url = "https://generator.example/v1/chat/completions"
    send = (lambda body: generation._bounded_json_request(url, body, timeout=1)
            if sender == "bounded" else generator_schema._urllib_transport(1)(url, body, {}))
    assert_refused(send, {"tools": []}, RecordingOpener())


@pytest.mark.parametrize("sender", ["bounded", "schema"])
def test_t4_refusal_precedes_serialization_and_omits_payload(caplog, sender):
    opener = RecordingOpener()
    payload = {"tools": [object()], "messages": [{"role": "user", "content": "synthetic-sensitive-marker"}]}
    url = "http://127.0.0.1:8790/api/chat"
    send = (lambda body: generation._bounded_json_request(url, body, timeout=1, opener=opener)
            if sender == "bounded" else generator_schema._urllib_transport(1, open_request=opener.open)(url, body, {}))
    with pytest.raises(RuntimeError) as error:
        send(payload)
    assert type(error.value) is generator_schema.ToolRequestRefused
    assert "synthetic-sensitive-marker" not in str(error.value) + caplog.text
    assert opener.calls == []


@pytest.mark.parametrize("role", ["Tool", "TOOL", " tool", "Function\n", " TOOL ", "FuNcTiOn"])
@pytest.mark.parametrize("sender", ["bounded", "schema"])
@pytest.mark.parametrize("path", CHAT_PATHS)
def test_mixed_case_tool_roles_refused_before_serialization(monkeypatch, role, sender, path):
    opener = RecordingOpener()
    module = generation if sender == "bounded" else generator_schema
    serialized = []

    def record_serialization(*args, **kwargs):
        serialized.append((args, kwargs))
        return json.dumps(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module, "_serialize_json", record_serialization)
        url = "http://127.0.0.1:8790" + path
        send = (lambda body: generation._bounded_json_request(url, body, timeout=1, opener=opener)
                if sender == "bounded" else generator_schema._urllib_transport(1, open_request=opener.open)(url, body, {}))
        assert_refused(send, {"messages": [{"role": role}]}, opener)
    assert serialized == []


@pytest.mark.parametrize("role", [None, 5, ["tool"]])
@pytest.mark.parametrize("sender", ["bounded", "schema"])
@pytest.mark.parametrize("path", CHAT_PATHS)
def test_non_string_roles_send_normally(monkeypatch, role, sender, path):
    response = {"synthetic": "response"}
    opener = RecordingOpener(response)
    module = generation if sender == "bounded" else generator_schema
    serialized = []
    payload = {"messages": [{"role": role}]}

    def record_serialization(*args, **kwargs):
        serialized.append((args, kwargs))
        return json.dumps(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module, "_serialize_json", record_serialization)
        url = "http://127.0.0.1:8790" + path
        if sender == "bounded":
            result = generation._bounded_json_request(url, payload, timeout=1, opener=opener)
        else:
            result = generator_schema._urllib_transport(1, open_request=opener.open)(url, payload, {})
    assert result == response
    assert len(serialized) == 1
    assert serialized[0][0] == (payload,)
    assert opener.calls == [payload]


def test_tool_guard_does_not_convert_non_string_role():
    class UnstringifiableRole:
        def __str__(self):
            raise AssertionError("Role must not be converted to a string")

    generator_schema.refuse_tool_request({"messages": [{"role": UnstringifiableRole()}]})


@pytest.mark.parametrize("builder", ["answer", "schema-vllm", "schema-ollama", "question"])
def test_t5_existing_builders_send_valid_requests(builder):
    answer = {"answerable": False, "claims": [], "limitation": None,
              "missing_information": "Synthetic missing information"}
    content = {"questions": []} if builder == "question" else answer
    response = {"choices": [{"message": {"content": json.dumps(content)}}]}
    opener = RecordingOpener(response)
    if builder.startswith("schema-"):
        path, payload = generator_schema.schema_chat_request(
            builder.removeprefix("schema-"), model="synthetic-model",
            schema=generator_schema.PROBE_SCHEMA, prompt="Synthetic prompt", name="synthetic_probe")
        result = generator_schema._urllib_transport(1, open_request=opener.open)(
            "http://127.0.0.1:8790" + path, payload, {})
        assert result == response
    else:
        client = generation.OpenAICompatibleGenerator(
            "http://127.0.0.1:8790", "synthetic-model", opener=opener,
            schema_mode="verified", question_profile=QuestionGenerationProfile(top_up_calls=0))
        if builder == "answer":
            assert client.generate(question="Synthetic question", evidence=()) == answer
        else:
            result = client.draft_questions(purpose="witness", topic="Synthetic topic", evidence=())
            assert result["questions"] == []
            assert result["calls"][0].get("error") is None
    assert len(opener.calls) == 1
    body = opener.calls[0]
    assert not FORBIDDEN_KEYS.intersection(body)
    for message in body["messages"]:
        assert message.get("role") not in {"tool", "function"}
        assert not {"tool_calls", "function_call"}.intersection(message)


def test_t6_chat_request_construction_has_no_tool_keys():
    checked, violations = [], []
    for directory in (ROOT / "src", ROOT / "scripts"):
        for path in sorted(directory.rglob("*.py")):
            source = path.read_text()
            if not any(endpoint in source for endpoint in CHAT_PATHS):
                continue
            checked.append(path.relative_to(ROOT).as_posix())
            for node in ast.walk(ast.parse(source)):
                keys = ([key.value for key in node.keys if isinstance(key, ast.Constant)]
                        if isinstance(node, ast.Dict) else [node.arg] if isinstance(node, ast.keyword) else [])
                if any(key in FORBIDDEN_KEYS for key in keys):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert "src/case_intelligence/generation.py" in checked
    assert "src/case_intelligence/generator_schema.py" in checked
    assert violations == []


def qualified_name(node, aliases):
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return qualified_name(node.value, aliases) + "." + node.attr
    return ""


def execution_violations(source):
    tree = ast.parse(source)
    aliases, violations = {}, []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for name in node.names:
                origin = f"{node.module}.{name.name}" if isinstance(node, ast.ImportFrom) else name.name
                binding = origin if isinstance(node, ast.ImportFrom) or name.asname else origin.split(".")[0]
                aliases[name.asname or name.name.split(".")[0]] = binding
                if origin.split(".")[0] in {"subprocess", "pty", "shlex"}:
                    violations.append(f"{node.lineno}: import {origin}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = qualified_name(node.func, aliases)
            builtin = name.removeprefix("builtins.")
            if (builtin in {"eval", "exec", "compile", "__import__"}
                    or name in {"os.system", "os.popen"}
                    or name.startswith(("os.spawn", "os.exec"))):
                violations.append(f"{node.lineno}: call {name}")
    return violations


@pytest.mark.parametrize("module", GENERATOR_MODULES)
def test_t7_generator_modules_do_not_execute_output(module):
    source = (ROOT / "src" / "case_intelligence" / f"{module}.py").read_text()
    assert execution_violations(source) == []


@pytest.mark.parametrize("source", [
    "import subprocess", "from pty import spawn", "import shlex as parser",
    "eval('synthetic')", "exec('synthetic')", "compile('synthetic')", "__import__('synthetic')",
    "import os.path\nos.system('synthetic')", "import os as operating\noperating.popen('synthetic')",
    "from os import spawnv as launch\nlaunch('synthetic')", "import os\nos.execve('synthetic')",
    "from builtins import eval as evaluate\nevaluate('synthetic')",
])
def test_t7_static_scan_rejects_prohibited_forms(source):
    assert execution_violations(source)
