"""Bring-your-own-model checks: hardware tiers, schema probes, and installer wiring."""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from case_intelligence.external_generator import (
    HOST_GATEWAY_NAME,
    EndpointRejected,
    environment_values,
    parse_generator_endpoint,
    require_local_or_confirmed,
)
from case_intelligence.generation import (
    GenerationUnavailable,
    OpenAICompatibleGenerator,
    OllamaGenerator,
    VerifiedAnswer,
    generator_from_environment,
    refresh_generator_schema,
)
from case_intelligence.generator_schema import (
    PROBE_MARKER,
    PROBE_SCHEMA,
    SchemaProbe,
    SchemaTransportError,
    openai_response_format,
    probe_schema_enforcement,
    schema_chat_request,
    schema_error,
)
from case_intelligence.hardware_profile import (
    HardwareReading,
    detect_hardware,
    suggest_tier,
    validate_profiles,
)
from case_intelligence.model_qualification import (
    UNVALIDATED_MODEL_LABEL,
    apply_harness_result,
    qualification_label,
)
from case_intelligence.store_records import MatterRecord, MessageRecord, ResearchJobRecord
from case_intelligence.work_product_exports import export_answer, export_research
from case_intelligence.workbench import CaseIntelligenceWorkbench
from case_intelligence.workspace_store import ConversationRecord


ROOT = Path(__file__).parents[1]
MIB = 1024 * 1024
STAMP = "2026-08-27T15:00:00Z"
TOKEN = "synthetic-generator-token"


def _reading(**overrides) -> HardwareReading:
    values = dict(
        cpu_cores=8,
        ram_bytes=8 * 1024 ** 3,
        gpu_vendor="none",
        gpu_vram_bytes=None,
        apple_silicon=False,
        notes=(),
    )
    values.update(overrides)
    return HardwareReading(**values)


def test_profile_catalog_matches_its_schema():
    validate_profiles()
    suggestion = suggest_tier(_reading())
    assert suggestion.profile_id == "cpu"
    assert suggestion.fallback is False
    assert "model download :: not started" in suggestion.lines()
    assert "measured quality" in suggestion.tradeoff


@pytest.mark.parametrize(
    ("reading", "profile_id"),
    [
        (_reading(ram_bytes=14335 * MIB), "cpu"),
        (_reading(ram_bytes=14336 * MIB), "cpu-16"),
        (_reading(gpu_vendor="nvidia", gpu_vram_bytes=int(7.9 * 1024 ** 3)), "cpu"),
        (_reading(gpu_vendor="nvidia", gpu_vram_bytes=8192 * MIB), "gpu-8"),
        (_reading(gpu_vendor="amd", gpu_vram_bytes=12287 * MIB), "gpu-8"),
        (_reading(gpu_vendor="nvidia", gpu_vram_bytes=12288 * MIB), "gpu-12"),
        (_reading(gpu_vendor="nvidia", gpu_vram_bytes=16383 * MIB), "gpu-12"),
        (_reading(gpu_vendor="nvidia", gpu_vram_bytes=16384 * MIB), "gpu-16"),
        (_reading(gpu_vendor="nvidia", gpu_vram_bytes=24575 * MIB), "gpu-16"),
        (_reading(gpu_vendor="amd", gpu_vram_bytes=24576 * MIB), "gpu-24"),
        (_reading(apple_silicon=True, ram_bytes=16 * 1024 ** 3), "apple-16"),
        (_reading(apple_silicon=True, ram_bytes=13 * 1024 ** 3), "cpu"),
    ],
)
def test_tier_boundaries(reading, profile_id):
    suggestion = suggest_tier(reading)
    assert suggestion.profile_id == profile_id
    assert suggestion.fallback is False


def test_detection_failure_uses_the_cpu_profile_without_crashing():
    blank = HardwareReading(None, None, "none", None, False, ("CPU core count was not available.",))
    suggestion = suggest_tier(blank)
    assert suggestion.profile_id == "cpu" and suggestion.fallback is True
    assert "hardware detection :: using the CPU suggestion" in suggestion.lines()

    def missing(command):
        raise FileNotFoundError(command[0])

    reading = detect_hardware(
        run=missing,
        system="Linux",
        machine="x86_64",
        cpu_count=None,
        meminfo=None,
    )
    assert reading.gpu_vram_bytes is None
    assert any("GPU" in note or "nvidia-smi" in note or "RAM" in note for note in reading.notes)
    failed = suggest_tier(reading)
    assert failed.profile_id == "cpu" and failed.fallback is True


def test_gpu_memory_parsers_accept_nvidia_and_amd_output():
    def run(command):
        if command[0] == "nvidia-smi":
            return subprocess.CompletedProcess(command, 0, "8192\n4096\n", "")
        if command[0] == "rocm-smi":
            return subprocess.CompletedProcess(command, 0, "GPU[0] : VRAM Total Memory (MiB): 24576\n", "")
        raise AssertionError(command)

    nvidia = detect_hardware(
        run=run, system="Linux", machine="x86_64", cpu_count=4, meminfo="MemTotal: 16777216 kB",
    )
    assert nvidia.gpu_vendor == "nvidia"
    assert nvidia.gpu_vram_bytes == 8192 * MIB
    assert nvidia.cpu_cores == 4
    assert suggest_tier(nvidia).profile_id == "gpu-8"

    def amd_only(command):
        if command[0] == "nvidia-smi":
            raise FileNotFoundError("nvidia-smi")
        return run(command)

    amd = detect_hardware(
        run=amd_only, system="Linux", machine="x86_64", cpu_count=4, meminfo="MemTotal: 16777216 kB",
    )
    assert amd.gpu_vendor == "amd" and amd.gpu_vram_bytes == 24576 * MIB
    apple = detect_hardware(
        run=missing_tools, system="Darwin", machine="arm64", cpu_count=8, meminfo="17179869184",
    )
    assert apple.apple_silicon is True and apple.gpu_vendor == "apple"
    assert suggest_tier(apple).profile_id == "apple-16"


def missing_tools(command):
    raise FileNotFoundError(command[0])


def test_live_hardware_detection_returns_a_catalog_tier():
    suggestion = suggest_tier(detect_hardware())
    assert suggestion.profile_id
    assert "model download :: not started" in suggestion.lines()


def test_each_server_uses_its_schema_form():
    ollama_path, ollama_body = schema_chat_request(
        "ollama", model="synthetic-model", schema=PROBE_SCHEMA, prompt="probe", name="exculpata_schema_probe",
    )
    assert ollama_path == "/api/chat"
    assert ollama_body["format"] == PROBE_SCHEMA
    assert "response_format" not in ollama_body
    for server in ("llamacpp", "lmstudio", "vllm"):
        path, body = schema_chat_request(
            server, model="synthetic-model", schema=PROBE_SCHEMA, prompt="probe", name="exculpata_schema_probe",
        )
        assert path == "/v1/chat/completions"
        response_format = body["response_format"]
        assert "schema" not in response_format
        assert response_format["json_schema"]["schema"]["properties"]["marker"]["const"] == PROBE_MARKER
        assert response_format["json_schema"]["strict"] is True
    nested = openai_response_format("case_answer", {"type": "object"})
    assert set(nested) == {"type", "json_schema"}


def _probe(server, replies):
    seen = []

    def transport(url, body, headers):
        seen.append((url, json.loads(json.dumps(body)), dict(headers)))
        reply = replies[len(seen) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply

    result = probe_schema_enforcement(
        "http://127.0.0.1:9", server, "synthetic-model", transport=transport, attempts=3,
    )
    return result, seen


def _valid(server):
    content = json.dumps({"marker": PROBE_MARKER})
    if server == "ollama":
        return {"message": {"content": content}}
    return {"choices": [{"message": {"content": content}}]}


def _invalid(server):
    if server == "ollama":
        return {"message": {"content": "not the requested object"}}
    return {"choices": [{"message": {"content": "not the requested object"}}]}


@pytest.mark.parametrize("server", ["ollama", "llamacpp", "lmstudio"])
def test_schema_probe_reports_verified_retry_refused_and_unreachable(server):
    verified, seen = _probe(server, [_valid(server)])
    assert verified.status == "verified" and verified.label == "verified" and verified.attempts == 1
    body = seen[0][1]
    if server == "ollama":
        assert seen[0][0].endswith("/api/chat") and "format" in body and "response_format" not in body
    else:
        assert seen[0][0].endswith("/v1/chat/completions")
        assert "schema" not in body["response_format"]
        assert body["response_format"]["json_schema"]["schema"]["required"] == ["marker"]

    retried, calls = _probe(server, [_invalid(server), _valid(server)])
    assert retried.status == "validate_and_retry" and retried.label == "not enforced"
    assert len(calls) == 2

    refused, calls = _probe(server, [_invalid(server), _invalid(server), _invalid(server)])
    assert refused.status == "refused" and refused.label == "refused" and len(calls) == 3

    unreachable, calls = _probe(server, [SchemaTransportError("unreachable")])
    assert unreachable.status == "unreachable" and unreachable.label == "not checked" and len(calls) == 1
    assert schema_error({"marker": PROBE_MARKER, "extra": "synthetic-case-text"}, PROBE_SCHEMA) == (
        "The reply did not match the JSON schema."
    )


def _answer(extra=False):
    payload = {
        "answerable": False,
        "claims": [],
        "limitation": None,
        "missing_information": "",
    }
    if extra:
        payload["extra"] = "synthetic-case-text"
    return json.dumps(payload)


def test_validate_and_retry_checks_locally_and_unprobed_does_not(monkeypatch):
    import case_intelligence.generation as generation

    calls = []

    def request(url, payload, **kwargs):
        calls.append((url, payload))
        content = _answer(extra=len(calls) == 1)
        if url.endswith("/api/chat"):
            return {"message": {"content": content}}
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(generation, "_bounded_json_request", request)
    evidence = (generation.EvidenceItem("S1", "Synthetic report", "Page 1", "A synthetic sentence."),)
    ollama = OllamaGenerator("http://127.0.0.1:11434", "synthetic-model", schema_mode="validate_and_retry")
    assert ollama.generate(question="What does the synthetic report say?", evidence=evidence)["answerable"] is False
    assert len(calls) == 2
    assert calls[0][1]["format"]["additionalProperties"] is False
    assert "response_format" not in calls[0][1]
    assert "synthetic-case-text" not in json.dumps(calls[1][1])
    assert "did not match the JSON schema" in calls[1][1]["messages"][-1]["content"]

    calls.clear()
    llamacpp = OpenAICompatibleGenerator(
        "http://127.0.0.1:8080", "synthetic-model", schema_server="llamacpp", schema_mode="validate_and_retry",
    )
    llamacpp.generate(question="What does the synthetic report say?", evidence=evidence)
    assert calls[0][0].endswith("/v1/chat/completions")
    assert "schema" not in calls[0][1]["response_format"]
    assert calls[0][1]["response_format"]["json_schema"]["schema"]["required"] == [
        "answerable", "claims", "limitation", "missing_information",
    ]
    lmstudio = OpenAICompatibleGenerator(
        "http://127.0.0.1:1234", "synthetic-model", schema_server="lmstudio", schema_mode="unprobed",
    )
    calls.clear()
    lmstudio.generate(question="What does the synthetic report say?", evidence=evidence)
    assert len(calls) == 1

    calls.clear()

    def fail(url, payload, **kwargs):
        calls.append(url)
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr(generation, "_bounded_json_request", fail)
    with pytest.raises(TimeoutError):
        ollama.generate(question="What does the synthetic report say?", evidence=evidence)
    assert len(calls) == 1

    calls.clear()

    def always_invalid(url, payload, **kwargs):
        calls.append(url)
        return {"message": {"content": _answer(extra=True)}}

    monkeypatch.setattr(generation, "_bounded_json_request", always_invalid)
    with pytest.raises(GenerationUnavailable, match="unavailable for this request"):
        ollama.generate(question="What does the synthetic report say?", evidence=evidence)
    assert len(calls) == 3

    refused = OllamaGenerator("http://127.0.0.1:11434", "synthetic-model", schema_mode="refused")
    assert refused.available is False
    calls.clear()
    with pytest.raises(GenerationUnavailable, match="Answering stays off"):
        refused.generate(question="What does the synthetic report say?", evidence=evidence)
    assert calls == []


def test_environment_labels_unvalidated_models_and_can_skip_the_probe(monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_BACKEND", "openai")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_SERVER", "llamacpp")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_MODEL", "recordbench-qwen35-9b")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_SCHEMA_PROBE", "0")
    client = generator_from_environment()
    assert client.schema_server == "llamacpp"
    assert client.schema_mode == "unprobed"
    assert client.model_notice == UNVALIDATED_MODEL_LABEL
    assert qualification_label("recordbench-qwen35-4b") == UNVALIDATED_MODEL_LABEL


def test_schema_refresh_is_bounded(monkeypatch):
    import case_intelligence.generator_completion as completion

    calls = []
    monkeypatch.setattr(completion, "probe_generator", lambda client: calls.append(client.schema_mode))
    current = OllamaGenerator("http://127.0.0.1:11434", "synthetic-model", schema_mode="verified")
    current._schema_checked_at = 1_000
    refresh_generator_schema(current, now=1_030)
    refresh_generator_schema(current, now=1_061)
    untouched = OllamaGenerator("http://127.0.0.1:11434", "synthetic-model")
    refresh_generator_schema(untouched, now=10_000)
    assert calls == ["verified"]


def test_qualification_registry_and_harness_seam(monkeypatch):
    assert qualification_label("synthetic-model") == UNVALIDATED_MODEL_LABEL
    assert qualification_label("") == ""
    passed = apply_harness_result(
        {"schema_version": 1, "qualifications": []},
        model_id="synthetic-model",
        harness="defense-workflow",
        passed=True,
        evidence="synthetic-scorecard",
    )
    assert qualification_label("synthetic-model", registry=passed) == ""
    failed = apply_harness_result(
        passed, model_id="synthetic-model", harness="defense-workflow", passed=False,
    )
    assert failed["qualifications"] == []
    assert qualification_label("synthetic-model", registry=failed) == UNVALIDATED_MODEL_LABEL

    module = type(sys)("case_intelligence.defense_workflow_eval")

    def qualification_for(model_id):
        if model_id == "synthetic-pass":
            return ""
        if model_id == "synthetic-defer":
            return None
        return UNVALIDATED_MODEL_LABEL

    module.qualification_for = qualification_for
    monkeypatch.setitem(sys.modules, "case_intelligence.defense_workflow_eval", module)
    deferred = apply_harness_result(
        {"schema_version": 1, "qualifications": []},
        model_id="synthetic-defer",
        harness="defense-workflow",
        passed=True,
    )
    assert qualification_label("synthetic-pass", registry=failed) == ""
    assert qualification_label("synthetic-model", registry=passed) == UNVALIDATED_MODEL_LABEL
    assert qualification_label("synthetic-defer", registry=deferred) == ""
    monkeypatch.delitem(sys.modules, "case_intelligence.defense_workflow_eval")
    assert qualification_label("synthetic-model") == UNVALIDATED_MODEL_LABEL


def test_endpoint_acceptance_and_remote_confirmation():
    with pytest.raises(EndpointRejected, match="http or https"):
        parse_generator_endpoint("ollama", "not-a-url", "synthetic-model")
    with pytest.raises(EndpointRejected, match="/v1/chat/completions"):
        parse_generator_endpoint("llamacpp", "http://127.0.0.1:8080/v1/chat/completions", "synthetic-model")
    loopback = parse_generator_endpoint("ollama", "http://127.0.0.1:11434", "synthetic-model")
    assert loopback.container_loopback is True and loopback.local_network is True
    lan = require_local_or_confirmed(parse_generator_endpoint("ollama", "http://synthetic-answers:11434", "synthetic-model"))
    assert lan.local_network is True and lan.container_loopback is False
    gateway = parse_generator_endpoint(
        "llamacpp", "http://" + HOST_GATEWAY_NAME + ":8080", "synthetic-model",
    )
    assert gateway.local_network is True and gateway.container_loopback is False
    assert gateway.host == HOST_GATEWAY_NAME
    remote = parse_generator_endpoint("lmstudio", "http://192.0.2.10:1234", "synthetic-model")
    assert remote.local_network is False
    with pytest.raises(EndpointRejected, match="outside the local network"):
        require_local_or_confirmed(remote)
    confirmed = require_local_or_confirmed(
        parse_generator_endpoint("lmstudio", "http://api.example.test:1234", "synthetic-model", remote_confirmed=True)
    )
    assert confirmed.remote_confirmed is True
    values = environment_values(lan, api_key_file="/run/recordbench-secrets/generator-api-key")
    assert values["CASE_INTELLIGENCE_GENERATOR_BACKEND"] == "ollama"
    assert values["CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS"] == "synthetic-answers"
    alias_args = _installer()._parser().parse_args([
        "install", "--models", "none", "--non-interactive",
        "--external-generator", "llamacpp",
        "--external-generator-url", "http://" + HOST_GATEWAY_NAME + ":8080",
        "--external-generator-model", "synthetic-model",
    ])
    recorded: dict[str, object] = {}
    recorded_endpoint = _installer()._apply_external_generator(
        alias_args, {}, recorded, {"secrets": Path("/tmp/exculpata-missing-secrets")},
    )
    assert recorded_endpoint is not None
    assert recorded["EXCULPATA_HOST_GATEWAY_ALIAS"] == HOST_GATEWAY_NAME
    assert values["CASE_INTELLIGENCE_GENERATOR_DISABLE_THINKING"] == "0"
    assert values["CASE_INTELLIGENCE_GENERATOR_TIMEOUT"] == "180"
    assert values["CASE_INTELLIGENCE_GENERATOR_API_KEY_FILE"].endswith("generator-api-key")
    assert TOKEN not in json.dumps(values)


def _installer():
    from tests.test_oss_installer import installer
    return installer


def _configure_node(tmp_path, extra, monkeypatch=None):
    installer = _installer()
    root = tmp_path / "Synthetic Node"
    console = installer.Console(color=False, quiet=True)
    args = installer._parser().parse_args([
        "install", "--names", "recordbench", "--auth", "local", "--enable-account-management",
        "--models", "none", "--non-interactive", "--prepare-only",
        "--admin-username", "alice.admin", "--admin-display-name", "Alice Administrator",
        *extra,
    ])
    installer._collect_identity_choices(args)
    paths = installer._prepare_directories(console, root, storage_root=None, resume=False, dry_run=False)
    cert, key = paths["tls"] / "synthetic.crt", paths["tls"] / "synthetic.key"
    cert.write_text("synthetic certificate")
    key.write_text("synthetic key")
    args.tls_cert, args.tls_key = cert, key
    installer._configure(console, args, root, paths, "synthetic-release", ROOT, ())
    return installer, root, args, paths


def test_installer_records_a_local_server_without_the_api_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(TOKEN + "\n"))
    installer, root, _args, paths = _configure_node(tmp_path, [
        "--external-generator", "ollama",
        "--external-generator-url", "http://synthetic-answers:11434",
        "--external-generator-model", "synthetic-model",
        "--external-generator-api-key-stdin",
    ])
    application = installer._dotenv(root / "config" / "recordbench.env")
    compose = installer._dotenv(root / "compose.env")
    record = json.loads((root / "installation.json").read_text())
    assert application["CASE_INTELLIGENCE_GENERATOR_BACKEND"] == "ollama"
    assert application["CASE_INTELLIGENCE_GENERATOR_SERVER"] == "ollama"
    assert application["CASE_INTELLIGENCE_GENERATOR_URL"] == "http://synthetic-answers:11434"
    assert application["CASE_INTELLIGENCE_GENERATOR_MODEL"] == "synthetic-model"
    assert application["CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS"] == "synthetic-answers"
    assert application["CASE_INTELLIGENCE_GENERATOR_API_KEY_FILE"] == "/run/recordbench-secrets/generator-api-key"
    assert compose["RECORDBENCH_GENERATOR_ALLOWED_HOSTS"] == "synthetic-answers"
    assert compose["RECORDBENCH_RETRIEVAL_DEVICE"] == "cpu"
    assert record["external_generator"] == {
        "server": "ollama",
        "url": "http://synthetic-answers:11434",
        "model": "synthetic-model",
        "host": "synthetic-answers",
        "remote_confirmed": False,
    }
    secret = (paths["secrets"] / "generator-api-key").read_text()
    assert TOKEN in secret
    published = (root / "installation.json").read_text() + (root / "compose.env").read_text()
    assert TOKEN not in published

    dry = installer._parser().parse_args(["doctor", "--root", str(root), "--dry-run"])
    installer._doctor(installer.Console(color=False), dry, root)
    dry_output = capsys.readouterr().out
    assert "hardware ::" in dry_output
    assert "suggested tier ::" in dry_output
    assert "model download :: not started" in dry_output
    assert "answer server :: ollama synthetic-model at http://synthetic-answers:11434" in dry_output
    assert "Dry run: the schema check was not sent" in dry_output
    assert TOKEN not in dry_output

    probes = []

    def probe(url, server, model, **kwargs):
        probes.append((url, server, model, kwargs.get("headers")))
        return SchemaProbe("verified", 1, "The server returned the required JSON object on the first check.", "")

    monkeypatch.setattr(installer, "probe_schema_enforcement", probe)
    monkeypatch.setattr(installer, "_wait_health", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        installer, "_run",
        lambda console, command, **kwargs: subprocess.CompletedProcess(list(command), 0, "", ""),
    )
    live = installer._parser().parse_args(["doctor", "--root", str(root)])
    installer._doctor(installer.Console(color=False), live, root)
    live_output = capsys.readouterr().out
    assert "schema enforcement :: verified" in live_output
    assert TOKEN not in live_output
    assert probes[0][0] == "http://synthetic-answers:11434"
    assert probes[0][1] == "ollama"
    assert probes[0][3]["Authorization"] == f"Bearer {TOKEN}"

    installer._replace_env(
        root / "compose.env", "RECORDBENCH_EMBEDDING_MODEL", "synthetic/other-embedding", add_missing_aliases=True,
    )
    installer._doctor_generator(installer.Console(color=False), root, dry_run=True)
    assert "Existing indexes must be rebuilt" in capsys.readouterr().out

    resumed = installer._parser().parse_args([
        "install", "--resume", "--non-interactive", "--prepare-only", "--root", str(root),
    ])
    installer._saved_node_arguments(resumed, root)
    assert resumed.external_generator == "ollama"
    assert resumed.external_generator_url == "http://synthetic-answers:11434"
    assert resumed.external_generator_model == "synthetic-model"
    resumed.admin_username = record["initial_administrator"]
    resumed.admin_display_name = record["initial_administrator_display_name"]
    installer._configure(installer.Console(color=False, quiet=True), resumed, root, paths, "synthetic-release", ROOT, ())
    kept = installer._dotenv(root / "config" / "recordbench.env")
    assert kept["CASE_INTELLIGENCE_GENERATOR_URL"] == "http://synthetic-answers:11434"
    assert TOKEN not in (root / "installation.json").read_text()


def test_remote_and_partial_generator_settings_are_not_saved(tmp_path):
    installer = _installer()
    with pytest.raises(RuntimeError, match="outside the local network"):
        _configure_node(tmp_path, [
            "--external-generator", "lmstudio",
            "--external-generator-url", "http://192.0.2.10:1234",
            "--external-generator-model", "synthetic-model",
        ])
    root = tmp_path / "Synthetic Node"
    assert not (root / "installation.json").exists()
    leaked = [
        path for path in root.rglob("*")
        if path.is_file() and "192.0.2.10" in path.read_text(errors="ignore")
    ]
    assert leaked == []
    args = installer._parser().parse_args([
        "install", "--models", "none", "--non-interactive",
        "--external-generator", "ollama", "--external-generator-model", "synthetic-model",
    ])
    with pytest.raises(RuntimeError, match="together"):
        installer._external_from_args(args)
    bundled = installer._parser().parse_args([
        "install", "--models", "review", "--non-interactive",
        "--external-generator", "ollama",
        "--external-generator-url", "http://synthetic-answers:11434",
        "--external-generator-model", "synthetic-model",
    ])
    with pytest.raises(RuntimeError, match="bundled generator"):
        installer._collect_external_generator(installer.Console(color=False, quiet=True), bundled)


def test_interactive_generator_prompts_guide_instead_of_quitting(monkeypatch, capsys):
    installer = _installer()
    answers = iter([
        "yes",
        "ollama",
        "not-a-url",
        "synthetic-model",
        "ollama",
        "http://127.0.0.1:11434",
        "synthetic-model",
        "http://" + HOST_GATEWAY_NAME + ":11434",
    ])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    args = installer._parser().parse_args(["install", "--models", "none"])
    endpoint = installer._collect_external_generator(installer.Console(color=False, quiet=True), args)
    output = capsys.readouterr().out
    assert endpoint is not None
    assert endpoint.url == "http://" + HOST_GATEWAY_NAME + ":11434"
    assert "http or https" in output
    assert "container" in output.casefold()
    assert args.confirm_remote_generator is False

    remote_answers = iter([
        "yes",
        "lmstudio",
        "http://192.0.2.10:1234",
        "synthetic-model",
        "no",
        "llamacpp",
        "http://synthetic-lan:8080",
        "synthetic-model",
    ])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(remote_answers))
    declined = installer._parser().parse_args(["install", "--models", "transcription"])
    kept = installer._collect_external_generator(installer.Console(color=False, quiet=True), declined)
    assert kept.host == "synthetic-lan"
    assert "was not confirmed" in capsys.readouterr().out

    confirmed_answers = iter([
        "yes",
        "llamacpp",
        "https://api.example.test",
        "synthetic-model",
        "yes",
    ])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(confirmed_answers))
    confirmed_args = installer._parser().parse_args(["install", "--models", "none"])
    confirmed = installer._collect_external_generator(installer.Console(color=False, quiet=True), confirmed_args)
    assert confirmed.remote_confirmed is True
    assert confirmed.host == "api.example.test"


def test_cpu_embeddings_and_rerank_stay_in_process(monkeypatch):
    import case_intelligence.retrieval_worker as worker
    import case_intelligence.review_bench_v2 as adapters

    calls = []

    class SentenceTransformer:
        def __init__(self, name, **kwargs):
            calls.append(("embed", name, kwargs))

        def encode(self, texts, normalize_embeddings=True):
            return [[0.25] for _text in texts]

    class CrossEncoder:
        def __init__(self, name, **kwargs):
            calls.append(("rerank", name, kwargs))

        def predict(self, pairs):
            return [0.5 for _pair in pairs]

    module = type(sys)("sentence_transformers")
    module.SentenceTransformer = SentenceTransformer
    module.CrossEncoder = CrossEncoder
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setenv("CASE_REVIEW_MODEL_DEVICE", "cpu")
    worker._embedding = None
    worker._reranker = None
    try:
        worker._embedding_model()
        worker._reranker_model()
    finally:
        worker._embedding = None
        worker._reranker = None
    assert {item[0] for item in calls} == {"embed", "rerank"}
    for _kind, _name, kwargs in calls:
        assert kwargs["device"] == "cpu"
        assert kwargs["local_files_only"] is True
        assert kwargs["trust_remote_code"] is False
        assert "url" not in kwargs

    calls.clear()
    embedding = adapters.LazySentenceTransformerEmbedding("synthetic-embedding", True, True)
    vectors = embedding.embed(["synthetic passage"])
    assert vectors == ((0.25,),)
    assert calls[-1][2]["device"] == "cpu"
    monkeypatch.delenv("CASE_REVIEW_MODEL_DEVICE")
    calls.clear()
    plain = adapters.LazySentenceTransformerEmbedding("synthetic-embedding", True, True)
    plain.embed(["synthetic passage"])
    assert "device" not in calls[-1][2]


def test_answers_and_exports_label_unvalidated_models():
    holder = CaseIntelligenceWorkbench.__new__(CaseIntelligenceWorkbench)
    holder.generator = SimpleNamespace(client=SimpleNamespace(
        model_notice=UNVALIDATED_MODEL_LABEL, schema_mode="validate_and_retry",
    ))
    answer = VerifiedAnswer(False, "Review the synthetic source.", (), None, "The passages do not answer it.", (), True, 1)
    payload = CaseIntelligenceWorkbench._noted_answer_payload(holder, answer, {})
    assert payload["model_notice"] == UNVALIDATED_MODEL_LABEL
    assert payload["schema_enforcement"] == "not enforced"
    silent = VerifiedAnswer(False, "Review the synthetic source.", (), None, "The passages do not answer it.", (), False, 1)
    unlabeled = CaseIntelligenceWorkbench._noted_answer_payload(holder, silent, {})
    assert "model_notice" not in unlabeled

    matter = MatterRecord("ci-matter-" + "a" * 32, "m-" + "a" * 12, "Synthetic Matter", "fixture", "owner", STAMP, STAMP)
    conversation = ConversationRecord("conversation-" + "b" * 32, matter.matter_id, "Synthetic timeline", STAMP, STAMP)
    message = MessageRecord(
        "message-" + "d" * 32, conversation.conversation_id, 2, "assistant", "A synthetic answer.",
        {
            "kind": "generated",
            "answerable": False,
            "introduction": "The synthetic record supports one sentence.",
            "missing_information": "A synthetic synthesis.",
            "claims": [{"text": "A synthetic vehicle arrived.", "citations": []}],
            "model_notice": UNVALIDATED_MODEL_LABEL,
            "schema_enforcement": "not enforced",
        },
        STAMP,
    )
    markdown = export_answer(matter, conversation, message, None, "markdown", exported_at=STAMP).body.decode()
    assert UNVALIDATED_MODEL_LABEL in markdown
    assert "Schema enforcement: not enforced." in markdown
    job = ResearchJobRecord(
        "job-" + "e" * 32, matter.matter_id, "owner", "synthetic-key", "What happened?",
        "Synthetic investigation", None, None, None, "completed", "done", 1, None, 0, {},
        {"evidence": [], "summary": "A synthetic synthesis.", "answer": message.payload},
        1, 1, 0, 0, "", STAMP, STAMP, STAMP, STAMP, STAMP,
    )
    exported = json.loads(export_research(matter, job, "json", exported_at=STAMP, presentation_notice="").body)
    assert exported["investigation"]["answer"]["model_notice"] == UNVALIDATED_MODEL_LABEL
    assert exported["investigation"]["answer"]["schema_enforcement"] == "not enforced"
    research_markdown = export_research(matter, job, "markdown", exported_at=STAMP, presentation_notice="").body.decode()
    assert UNVALIDATED_MODEL_LABEL in research_markdown
    assert "Schema enforcement: not enforced." in research_markdown


def test_templates_show_schema_and_qualification_notices():
    templates = ROOT / "src" / "case_intelligence" / "templates"
    notices = (templates / "workbench_model_notices.html").read_text()
    assert "Schema enforcement: refused." in notices
    assert "Schema enforcement: not enforced." in notices
    assert "Schema enforcement: not checked." in notices
    assert "Unvalidated model." in notices
    workspace = (templates / "workbench_workspace.html").read_text()
    home = (templates / "workbench_matter_home.html").read_text()
    assert 'include "workbench_model_notices.html"' in workspace
    assert 'include "workbench_model_notices.html"' in home
    assert "Schema enforcement: verified." in workspace
    for name in ("workbench_assistant.html", "workbench_research.html"):
        text = (templates / name).read_text()
        assert "model_notice" in text
        assert "schema_enforcement" in text
    admin = (templates / "workbench_admin.html").read_text()
    assert "model_qualification" in admin
    compose = (ROOT / "compose.yaml").read_text()
    assert "EXCULPATA_GENERATOR_ALLOWED_HOSTS" in compose
    assert "${EXCULPATA_HOST_GATEWAY_ALIAS:-host-gateway.example.test}:host-gateway" in compose
    assert HOST_GATEWAY_NAME not in compose
