"""Synthetic regressions for the final BYOM review's destination and provenance gaps."""
import ipaddress
import json

import pytest

from case_intelligence import external_generator as endpoints
from case_intelligence.embedding_index import record_embedding_index, load_embedding_index


@pytest.mark.parametrize("mapped", [False, True])
@pytest.mark.parametrize("confirmed", [False, True])
def test_provider_metadata_is_blocked_for_literal_and_dns(monkeypatch, mapped, confirmed):
    address = ipaddress.IPv4Address((100 << 24) | (100 << 16) | (100 << 8) | 200)
    if mapped:
        address = ipaddress.IPv6Address((0xFFFF << 32) | int(address))
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_REMOTE_CONFIRMED", "1" if confirmed else "0")
    monkeypatch.setattr(endpoints, "addresses_for", lambda host: (address,))
    for host in ("synthetic-generator.example.test", f"[{address}]" if mapped else str(address)):
        endpoint = endpoints.parse_generator_endpoint("ollama", f"http://{host}", "synthetic", remote_confirmed=confirmed)
        with pytest.raises(endpoints.EndpointRejected, match="metadata"):
            endpoints.require_local_or_confirmed(endpoint)
        with pytest.raises(endpoints.DestinationRejected, match="metadata"):
            endpoints.require_resolved_destination(f"http://{host}/api/chat")


@pytest.mark.parametrize("stored", [None, "not-json", b"\xff", {}, {"populated": False},
    {"populated": True, "model": "", "revision": ""}])
def test_populated_unknown_index_is_never_blessed(tmp_path, stored):
    path = tmp_path / "embedding-index.json"
    if isinstance(stored, bytes):
        path.write_bytes(stored)
    elif stored is not None:
        path.write_text(stored if isinstance(stored, str) else json.dumps(stored))
    before = path.read_bytes() if path.exists() else None
    warning = record_embedding_index(path, model="synthetic/new", revision="synthetic-new-revision", populated=True)
    assert "unknown" in warning
    assert (path.read_bytes() if path.exists() else None) == before
    assert load_embedding_index(path) != {"model": "synthetic/new", "revision": "synthetic-new-revision", "populated": True}


def test_unreadable_populated_index_record_remains_unknown(tmp_path):
    path = tmp_path / "embedding-index.json"
    path.mkdir()
    assert "unknown" in record_embedding_index(path, model="synthetic", revision="synthetic-revision", populated=True)
    assert path.is_dir()


def test_empty_document_does_not_clear_global_index_provenance(monkeypatch, tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from case_intelligence.review_bench_v2 import upsert_postgres_document
    path = tmp_path / "embedding-index.json"
    path.write_text(json.dumps({"model": "synthetic/old", "revision": "synthetic-old", "populated": True}))
    before = path.read_bytes()
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_INDEX_RECORD", str(path))
    connection = SimpleNamespace(autocommit=False, commit=lambda: None,
        cursor=lambda: nullcontext(SimpleNamespace(execute=lambda *args: None)))
    upsert_postgres_document(connection, matter_id="synthetic", matter_name="Synthetic",
        document_id="synthetic-empty", display_name="Synthetic empty", media_type="text/plain",
        units=(), ensure_schema=False)
    assert path.read_bytes() == before


def test_ollama_environment_key_reaches_every_request(monkeypatch, tmp_path):
    from case_intelligence import generation, generator_completion, generator_schema
    from case_intelligence.generation import EvidenceItem
    key_file = tmp_path / "synthetic-key"
    key_file.write_text("synthetic-ollama-credential")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_BACKEND", "ollama")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_SERVER", "ollama")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_MODEL", "synthetic")
    monkeypatch.delenv("CASE_INTELLIGENCE_GENERATOR_API_KEY", raising=False)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_SCHEMA_PROBE", "1")
    expected = {"Authorization": "Bearer synthetic-ollama-credential"}
    observed = []
    def probe(*args, **kwargs):
        observed.append(("probe", kwargs.get("headers")))
        return generator_schema.SchemaProbe("verified", 1, "Synthetic probe", "")
    monkeypatch.setattr(generator_completion, "probe_schema_enforcement", probe)
    def get(*args, **kwargs):
        observed.append(("health", kwargs.get("headers")))
        return {"models": [{"name": "synthetic"}]}
    monkeypatch.setattr(generation, "_bounded_json_get", get)
    def post(url, payload, **kwargs):
        observed.append(("chat", kwargs.get("headers")))
        if "claims" in payload["format"].get("properties", {}):
            result = {"answerable": False, "claims": [], "limitation": None, "missing_information": "Synthetic missing support"}
        else:
            result = {"decision": "not_identified", "rationale": "Synthetic missing support", "evidence_ids": []}
        return {"message": {"content": json.dumps(result)}}
    monkeypatch.setattr(generation, "_bounded_json_request", post)
    client = generation.generator_from_environment()
    assert client.available
    evidence = (EvidenceItem("S1", "Synthetic source", "Line 1", "Synthetic evidence."),)
    client.generate(question="Synthetic question?", evidence=evidence)
    client.classify_source(criterion="Synthetic", include_guidance="", exclude_guidance="", evidence=evidence)
    class StopDraft(Exception):
        pass
    def draft_post(*args, **kwargs):
        observed.append(("questions", kwargs.get("headers")))
        raise StopDraft
    monkeypatch.setattr(generation, "_bounded_json_request", draft_post)
    with pytest.raises(StopDraft):
        client.draft_questions(purpose="discovery", topic="Synthetic topic", evidence=evidence)
    assert [kind for kind, _ in observed] == ["probe", "health", "chat", "chat", "questions"]
    assert all(headers == expected for _, headers in observed)
