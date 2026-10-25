"""Synthetic regressions for retrieval configuration at b4c5cff."""
import importlib.util
import io
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from case_intelligence import embedding_index, retrieval_endpoints as endpoints
from case_intelligence import review_bench_v2 as review
from case_intelligence.pilot_uploads import PilotDocument
from case_intelligence.workbench import CaseIntelligenceWorkbench


@pytest.fixture
def retrieval_environment(monkeypatch, tmp_path):
    for key in tuple(os.environ):
        if key.startswith(("CASE_INTELLIGENCE_EMBEDDING_", "CASE_INTELLIGENCE_RERANK_")):
            monkeypatch.delenv(key)
    monkeypatch.delenv("CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL", raising=False)
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_INDEX_RECORD", str(tmp_path / "index.json"))

    def unexpected_request(*args, **kwargs):
        pytest.fail("Unconfigured retrieval transport received text")

    monkeypatch.setattr(review, "_local_model_request", unexpected_request)
    monkeypatch.setattr(endpoints, "open_generator_request", unexpected_request)


def test_rerank_only_uses_deterministic_embeddings(retrieval_environment, monkeypatch, tmp_path):
    url = "https://rerank.example.test"
    pins = json.dumps({"issuer": url, "addresses": [str(ipaddress.IPv4Address((10 << 24) | 7))]})
    for suffix, value in {"URL": url, "MODEL": "synthetic-reranker", "EGRESS_PINS": pins}.items():
        monkeypatch.setenv("CASE_INTELLIGENCE_RERANK_" + suffix, value)
    endpoints.require_index_identity()
    requests = []

    def request(req, **kwargs):
        requests.append((req.full_url, json.loads(req.data), kwargs["environment_prefix"]))
        return io.BytesIO(b'{"results":[{"index":0,"relevance_score":0.95}]}')

    monkeypatch.setattr(endpoints, "open_generator_request", request)
    connection = MagicMock(autocommit=False)
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [(None,), None]
    bench = CaseIntelligenceWorkbench(tmp_path / "runtime", postgres_connection=connection)
    try:
        assert isinstance(bench.embedding, review.DeterministicEmbeddingAdapter)
        assert isinstance(bench.reranker, review.RemoteRerankerAdapter)
        assert bench.learned_retrieval
        endpoints.require_index_identity()
        assert endpoints.relay_environment(os.environ) == {
            "EMBEDDING_EGRESS_URL": "", "EMBEDDING_EGRESS_PINS": "",
            "RERANK_EGRESS_URL": url, "RERANK_EGRESS_PINS": pins,
        }
        assert endpoints.relay_environment({"CASE_INTELLIGENCE_EMBEDDING_EGRESS_PINS": "invalid-pins"})["EMBEDDING_EGRESS_PINS"] == ""
        text = "Synthetic blue bicycle evidence."
        matter = SimpleNamespace(matter_id="synthetic-matter", display_name="Synthetic matter")
        document = PilotDocument("synthetic-document", "Synthetic.txt", "synthetic.txt", "text/plain",
            len(text), "ready", "", [{"number": 1, "text": text}], version_id="synthetic-v1")
        candidate = bench._candidate(matter, document, document.parsed_units()[0], 1)
        retriever = bench._retriever(matter)
        monkeypatch.setattr(retriever.backend, "lexical", lambda *a, **kw: (candidate,))
        monkeypatch.setattr(retriever.backend, "dense", lambda *a, **kw: (candidate,))
        assert retriever.search(matter.matter_id, "blue bicycle")[0].score == 0.95
        cursor.execute.reset_mock()
        bench._index_document(matter, document)
        chunks = [call.args[1] for call in cursor.execute.call_args_list
                  if call.args[0].startswith("INSERT INTO review_chunk(")]
        assert json.loads(chunks[0][6]) == pytest.approx(review.DeterministicEmbeddingAdapter().embed([text])[0])
        assert requests == [(url + "/v1/rerank", {
            "model": "synthetic-reranker", "documents": [text], "query": "blue bicycle",
        }, "CASE_INTELLIGENCE_RERANK")]
    finally:
        bench.close()


def test_fresh_index_and_caller_rebuild_messages(retrieval_environment, monkeypatch):
    assert not embedding_index.embedding_index_path().exists()
    endpoints.require_index_identity()
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_URL", "https://embed.example.test")
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_MODEL", "synthetic-embedder")
    bench = object.__new__(CaseIntelligenceWorkbench)
    bench.learned_retrieval = bench.postgres_ready = True
    bench.postgres_connection = MagicMock()
    bench.embedding = review.RemoteEmbeddingAdapter("")
    for check in (endpoints.require_index_identity,
                  lambda: review.RemoteEmbeddingAdapter("").embed(["Synthetic evidence"]),
                  lambda: bench._index_document(None, None)):
        with pytest.raises(RuntimeError) as failure:
            check()
        assert str(failure.value) == endpoints.REBUILD
    assert endpoints.doctor("EMBEDDING") == endpoints.REBUILD
    bench.postgres_connection.cursor.assert_not_called()
    monkeypatch.setattr(embedding_index, "embedding_index_path", lambda: None)
    with pytest.raises(RuntimeError, match="must be rebuilt"):
        endpoints.require_index_identity()
    monkeypatch.delenv("CASE_INTELLIGENCE_EMBEDDING_URL")
    endpoints.require_index_identity()


@pytest.mark.parametrize("action", ["ps", "logs", "stop", "down"])
@pytest.mark.parametrize("setting,value", [("URL", "http://invalid.example.test"), ("EGRESS_PINS", "invalid-pins")])
def test_invalid_saved_retrieval_allows_compose_maintenance(tmp_path, monkeypatch, action, setting, value):
    root = Path(os.environ.get("RETRIEVAL_SOURCE_ROOT", Path(__file__).resolve().parents[1]))
    monkeypatch.syspath_prepend(str(root / "scripts"))
    spec = importlib.util.spec_from_file_location("retrieval_round2_installer", root / "scripts/exculpata_install.py")
    installer = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, installer)
    spec.loader.exec_module(installer)
    config = tmp_path / "config"
    config.mkdir()
    values = {"CASE_INTELLIGENCE_RERANK_URL": "https://rerank.example.test",
              "CASE_INTELLIGENCE_RERANK_MODEL": "synthetic-reranker",
              "CASE_INTELLIGENCE_RERANK_" + setting: value}
    # fixture-write: Synthetic saved retrieval settings select an invalid destination or pins.
    (config / "exculpata.env").write_text(installer._env_text(values, "Synthetic"))
    # fixture-write: Synthetic Compose coordinates select only this fixture's application file.
    (tmp_path / "compose.env").write_text(installer._env_text({
        "EXCULPATA_CONFIG_ROOT": str(config), "EXCULPATA_APP_ENV_FILE": "exculpata.env",
    }, "Synthetic"))
    for role in ("EMBEDDING", "RERANK"):
        monkeypatch.setenv(role + "_EGRESS_URL", "https://ambient.example.test")
        monkeypatch.setenv(role + "_EGRESS_PINS", "ambient-pins")
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, "synthetic output", ""))
    monkeypatch.setattr(installer.subprocess, "run", run)
    command = ["docker", "compose", "--env-file", str(tmp_path / "compose.env")]
    result = installer._run(installer.Console(color=False), [*command, action], capture=True)
    assert result.returncode == 0
    run.assert_called_once()
    assert run.call_args.args[0] == [*command, action]
    environment = run.call_args.kwargs["env"]
    assert all(environment[role + suffix] == "" for role in ("EMBEDDING", "RERANK")
               for suffix in ("_EGRESS_URL", "_EGRESS_PINS"))
    with pytest.raises(RuntimeError, match="Invalid saved retrieval egress configuration") as failure:
        installer._run(installer.Console(color=False), [*command, "up", "-d"], capture=True)
    assert "CASE_INTELLIGENCE_RERANK" in str(failure.value)
    assert value not in str(failure.value)
    run.assert_called_once()
