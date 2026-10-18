"""Synthetic deployment and saved provenance regressions for BYOM."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from case_intelligence import external_generator as endpoints
from case_intelligence.generation import _bounded_json_get

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("port", ["abc", "99999", "-1", "0"])
def test_malformed_port_is_rejected_before_install_or_health(monkeypatch, port):
    monkeypatch.setattr(endpoints, "addresses_for", lambda host: pytest.fail("DNS must not run"))
    url = f"http://127.0.0.1:{port}"
    with pytest.raises(endpoints.EndpointRejected, match="port"):
        endpoints.parse_generator_endpoint("ollama", url, "synthetic")
    assert _bounded_json_get(url + "/api/tags", timeout=1) == {}


def test_compose_app_and_retrieval_share_embedding_identity(monkeypatch, tmp_path):
    from case_intelligence.embedding_index import note_populated_index
    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    for key in ("CASE_REVIEW_EMBEDDING_MODEL", "CASE_REVIEW_EMBEDDING_REVISION"):
        assert services["app"]["environment"][key] == services["retrieval"]["environment"][key]
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_INDEX_RECORD", str(tmp_path / "index.json"))
    monkeypatch.setenv("CASE_REVIEW_EMBEDDING_MODEL", "synthetic/first")
    monkeypatch.setenv("CASE_REVIEW_EMBEDDING_REVISION", "synthetic-revision-1")
    assert note_populated_index(populated=True) == ""
    monkeypatch.setenv("CASE_REVIEW_EMBEDDING_MODEL", "synthetic/second")
    assert "must be rebuilt" in note_populated_index(populated=True)


def test_compilation_persists_client_notice_after_model_changes(tmp_path):
    from case_intelligence.report_compilation import compile_report
    from tests.test_report_compilation import material, service
    generator = service()
    generator.client.model_notice = "unvalidated model"
    draft = compile_report("timeline", materials=(material(),), generator=generator)
    generator.client.model_notice = ""
    assert draft.coverage["model_notice"] == "unvalidated model"
    assert "unvalidated model" in draft.sections[-1]["body"]


@pytest.mark.parametrize("with_file", [False, True])
def test_doctor_uses_inline_key_before_file(monkeypatch, tmp_path, with_file):
    from tests.test_oss_installer import installer
    from case_intelligence.generator_schema import SchemaProbe
    token = "synthetic-inline-credential"
    app = {"CASE_INTELLIGENCE_GENERATOR_BACKEND": "openai",
           "CASE_INTELLIGENCE_GENERATOR_SERVER": "llamacpp",
           "CASE_INTELLIGENCE_GENERATOR_URL": "http://127.0.0.1:8000",
           "CASE_INTELLIGENCE_GENERATOR_MODEL": "synthetic",
           "CASE_INTELLIGENCE_GENERATOR_API_KEY": token}
    if with_file:
        app["CASE_INTELLIGENCE_GENERATOR_API_KEY_FILE"] = "/synthetic/unreadable"
    monkeypatch.setattr(installer, "_dotenv", lambda path: {} if path.name == "compose.env" else app)
    monkeypatch.setattr(installer, "_print_hardware_suggestion", lambda console: None)
    seen, output = [], []
    def probe(*args, **kwargs):
        seen.append(kwargs["headers"])
        return SchemaProbe("verified", 1, "Synthetic probe", "")
    monkeypatch.setattr(installer, "probe_schema_enforcement", probe)
    installer._doctor_generator(SimpleNamespace(note=output.append, warn=output.append), tmp_path, dry_run=False)
    assert seen == [{"Authorization": "Bearer " + token}]
    assert token not in "\n".join(output)


def test_doctor_bundled_probe_uses_existing_app_namespace(monkeypatch, tmp_path):
    from tests.test_oss_installer import installer
    app = {"CASE_INTELLIGENCE_GENERATOR_BACKEND": "vllm",
           "CASE_INTELLIGENCE_GENERATOR_URL": "http://generator:8000",
           "CASE_INTELLIGENCE_GENERATOR_MODEL": "synthetic"}
    monkeypatch.setattr(installer, "_dotenv", lambda path: {} if path.name == "compose.env" else app)
    monkeypatch.setattr(installer, "_print_hardware_suggestion", lambda console: None)
    monkeypatch.setattr(installer, "_compose", lambda *args: ["docker", "compose"])
    monkeypatch.setattr(installer, "probe_schema_enforcement", lambda *a, **k: pytest.fail("host cannot reach bundled DNS"))
    commands, output = [], []
    def probe(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "verified", "attempts": 1,
            "detail": "Synthetic schema check", "fix": ""}))
    monkeypatch.setattr(installer, "_probe", probe)
    installer._doctor_generator(SimpleNamespace(note=output.append, warn=output.append), tmp_path, dry_run=False)
    assert commands == [["docker", "compose", "exec", "-T", "app", "python", "-m", "case_intelligence.generator_doctor"]]
    assert "schema enforcement :: verified" in output


def test_packaged_registry_matches_editable_registry():
    from importlib.resources import files
    from case_intelligence.model_qualification import load_qualifications
    for name in ("model_qualifications.json", "model_qualifications.schema.json"):
        assert files("case_intelligence").joinpath("config", name).read_bytes() == (ROOT / "config" / name).read_bytes()
    assert load_qualifications()["schema_version"] == 1


def test_in_container_probe_uses_effective_config_and_runtime_credentials(monkeypatch):
    from case_intelligence import generator_doctor
    from case_intelligence.generator_schema import SchemaProbe
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_URL", "http://generator:8000")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS", "generator")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_MODEL", "synthetic")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_BACKEND", "vllm")
    monkeypatch.delenv("CASE_INTELLIGENCE_GENERATOR_SERVER", raising=False)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_API_KEY", "synthetic-inline-credential")
    seen = []
    def probe(*args, **kwargs):
        seen.append((args, kwargs))
        return SchemaProbe("verified", 1, "Synthetic schema check", "")
    monkeypatch.setattr(generator_doctor, "probe_schema_enforcement", probe)
    assert generator_doctor.probe_configured_generator().status == "verified"
    assert seen[0][0] == ("http://generator:8000", "vllm", "synthetic")
    assert seen[0][1]["headers"] == {"Authorization": "Bearer synthetic-inline-credential"}
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS", "other.example.test")
    assert generator_doctor.probe_configured_generator().status == "unreachable"
    assert len(seen) == 1


@pytest.mark.parametrize("view", ["assistant", "workspace"])
def test_unsupported_saved_answer_keeps_notices_after_model_change(tmp_path, view):
    from fastapi.testclient import TestClient
    from case_intelligence.generation import UnavailableGenerator
    from case_intelligence.workbench import create_workbench_app
    from tests.test_guided_usability import _matter
    with TestClient(create_workbench_app(tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="test")) as client:
        slug, matter_id, _ = _matter(client)
        store = client.app.state.workbench.workspace
        conversation = store.get_conversation(matter_id)
        store.append_message(matter_id, conversation.conversation_id, "assistant", "Synthetic missing support", {
            "kind": "not-supported", "missing_information": "Synthetic missing support",
            "model_notice": "unvalidated model", "schema_enforcement": "not enforced / validated locally"})
        path = f"/matters/{slug}/assistant" if view == "assistant" else f"/matters/{slug}"
        page = client.get(path, params={"conversation": conversation.conversation_id})
        assert page.status_code == 200
        assert "Synthetic missing support" in page.text
        assert "unvalidated model" in page.text
        assert "Schema enforcement: not enforced / validated locally." in page.text
