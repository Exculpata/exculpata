"""The synthetic demo uses normal intake and works without external services."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_ENV_PREFIX,
    LEGACY_STEM,
)

from collections import Counter
import importlib.util
import os
from pathlib import Path
import re
import socket
import sys
from unittest.mock import Mock

import pytest

from case_intelligence.briefing import build_briefing
from case_intelligence.intake_receipts import IntakeReceipts


ROOT = Path(__file__).resolve().parents[1]
PEOPLE = ("Vela Quenrix", "Nerin Talvex", "Sovi Pellune", "Ilex Rovanni")
PLACES = ("Brindlequay Storehouse", "Kestrelune Arcade")
DATES = ("2034-04-17", "2034-04-18", "2034-04-19")


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def demo_script(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return load_script(f"{LEGACY_STEM}_demo_test", "demo.py")


def test_seed_demo_accounts_for_every_source_and_discovers_recurring_names_offline(
    demo_script, tmp_path, monkeypatch,
):
    external_storage = tmp_path / "existing-storage"
    external_storage.mkdir()
    sentinel = external_storage / "sentinel.txt"
    sentinel.write_text("Synthetic existing deployment marker.", encoding="utf-8")
    inherited = {
        "CASE_INTELLIGENCE_MANAGED_STORAGE_ROOT": str(external_storage),
        "CASE_INTELLIGENCE_POSTGRES_DSN": "synthetic-inherited-value",
        "CASE_INTELLIGENCE_GENERATOR_URL": "https://generator.example.test",
        "CASE_INTELLIGENCE_TRANSCRIPTION_URL": "https://transcription.example.test",
        "CASE_INTELLIGENCE_CLAMAV_HOST": "scanner.example.test",
        "CASE_REVIEW_ENABLE_MODELS": "1",
        f"{LEGACY_ENV_PREFIX}LOCAL_ACCOUNT_ROOT": str(external_storage),
        "EXCULPATA_ALLOW_CONTAINER_BIND": "1",
        "EXCULPATA_BACKUP_STATUS_FILE": str(external_storage / "backup-status"),
    }
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)
    original_environment = os.environ.copy()
    network_attempts = []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def offline_connect(connection, address):
        if connection.family in (socket.AF_INET, socket.AF_INET6):
            network_attempts.append(address)
            raise AssertionError("The synthetic demo attempted a network connection")
        return original_connect(connection, address)

    def offline_connect_ex(connection, address):
        if connection.family in (socket.AF_INET, socket.AF_INET6):
            network_attempts.append(address)
            raise AssertionError("The synthetic demo attempted a network connection")
        return original_connect_ex(connection, address)

    monkeypatch.setattr(socket.socket, "connect", offline_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", offline_connect_ex)
    corpus = sorted(path for path in demo_script.CORPUS.rglob("*") if path.is_file())
    assert 30 <= len(corpus) <= 60
    expected_paths = Counter(path.relative_to(demo_script.CORPUS).as_posix() for path in corpus)
    runtime = tmp_path / "demo-runtime"
    with demo_script.seed_demo(runtime) as demo:
        bench = demo.app.state.workbench
        matter = bench.matter(demo.matter.slug, demo.actor_id)
        assert matter.display_name == "Harbor Street"
        assert not runtime.resolve().is_relative_to(ROOT)
        assert all(os.environ.get(flag) == "1" for flag in demo_script.FLAGS)
        assert "EXCULPATA_ALLOW_CONTAINER_BIND" not in os.environ
        assert "EXCULPATA_BACKUP_STATUS_FILE" not in os.environ
        assert f"{LEGACY_ENV_PREFIX}LOCAL_ACCOUNT_ROOT" not in os.environ
        assert bench.generator.available is False
        sources = bench.sources(matter)
        assert Counter(source.relative_path for source in sources) == expected_paths
        unreadable = [source for source in sources if source.tone == "attention"]
        assert len(unreadable) == 1
        assert all(source.tone in {"ready", "attention"} for source in sources)
        assert unreadable[0].state and unreadable[0].retryable

        status = demo.client.get(f"/matters/{matter.slug}/processing-status")
        assert status.status_code == 200
        readiness = status.json()
        assert readiness["total_count"] == len(corpus)
        assert readiness["saved_count"] == len(corpus)
        assert readiness["searchable_count"] == len(corpus) - 1
        assert readiness["attention_count"] == 1
        assert readiness["processing_count"] == 0
        assert not readiness["active_work"]
        assert readiness["can_query"] and readiness["partial_query"]
        assert not readiness["discovery"]["working"]
        progress = bench.automatic_discovery_progress(matter)
        assert progress["sources_pending"] == progress["sources_attention"] == progress["unsealed_sources"] == 0
        assert progress["sources_complete"] == len(corpus) - 1

        service = bench.entity_service(matter)
        for name in (*PEOPLE, *PLACES, *DATES):
            matching, _ = service.list(matter.matter_id, demo.actor_id, query=name)
            entities = [row for row in matching if row["display_name"] == name]
            assert entities
            source_ids = set()
            for entity in entities:
                assert entity["status"] == "suggested"
                if name in PEOPLE:
                    assert entity["entity_type"] == "person"
                _, mentions, _, _ = service.detail(matter.matter_id, demo.actor_id, entity["entity_id"])
                source_ids.update(mention["document_id"] for mention in mentions)
                assert all(mention["available"] for mention in mentions)
            assert len(source_ids) >= 2

        home = demo.client.get(f"/matters/{matter.slug}/home")
        assert home.status_code == 200 and "Harbor Street" in home.text
        assert "Discovery briefing" in home.text
        assert "Who and what appears most" in home.text
        assert "Busiest dates" in home.text
        assert "What could not be read and why" in home.text
        assert "Enter a question, exact search, or request to find matching records." in home.text
        built = build_briefing(
            matter, demo.actor_id, workspace=bench.workspace,
            entity_service=service, intake_receipts=IntakeReceipts(bench.workspace),
            source_metadata=bench.source_store(matter).get,
        )
        assert [section.key for section in built.sections] == [
            "arrived", "people_places", "dates", "unread",
        ]
        for section in built.sections:
            assert section.lines
            assert all(line.kind != "empty" for line in section.lines)
        people_text = " ".join(line.text for line in built.section("people_places").lines)
        date_text = " ".join(line.text for line in built.section("dates").lines)
        unread_text = " ".join(line.text for line in built.section("unread").lines)
        for name in PEOPLE:
            assert name in people_text and name in home.text
        for day in DATES:
            assert day in date_text and day in home.text
        assert unreadable[0].name in unread_text and unreadable[0].name in home.text
        receipt = demo.client.get(demo.receipt_url)
        assert receipt.status_code == 200 and unreadable[0].name in receipt.text
        library = demo.client.get(f"/matters/{matter.slug}/setup", params={"view": "list"})
        assert library.status_code == 200 and unreadable[0].name in library.text
        search = demo.client.get(
            f"/matters/{matter.slug}/exact-search", params={"words": "Vela Quenrix", "search": "1"},
        )
        assert search.status_code == 200
        found = re.search(r"([\d,]+) sources? found", search.text)
        assert found and int(found[1].replace(",", "")) >= 2
        assert "1 still preparing or need attention" in search.text
        conversation = demo.client.get(f"/matters/{matter.slug}")
        assert conversation.status_code == 200
        assert "Answering is temporarily unavailable. Search and source review still work." in conversation.text

    assert network_attempts == []
    assert os.environ == original_environment
    assert list(external_storage.iterdir()) == [sentinel]
    assert sentinel.read_text(encoding="utf-8") == "Synthetic existing deployment marker."


def test_fresh_temporal_answer_packet_retains_dispatch_observation(demo_script, tmp_path):
    """Measure the real answer packet, independently of generation or history."""
    with demo_script.seed_demo(tmp_path / "temporal-demo") as demo:
        bench, matter = demo.app.state.workbench, demo.matter
        assert bench.generator.available is False
        question = "Where was the teal case before 9:26?"
        retrieval_query = bench._retrieval_question(question, ())
        assert retrieval_query == question
        paths = {source.document_id: source.relative_path for source in bench.sources(matter)}
        candidates = bench._answer_search(matter, question, retrieval_query)
        packet = bench._answer_evidence_citations(matter, candidates)
        assert len(candidates) <= 20
        assert len(packet) == 8
        dispatch = next((row for row in packet
                         if paths[row.document_id] == "reports/04-dispatch-observation.txt"), None)
        assert dispatch is not None, [paths[row.document_id] for row in packet]
        text = " ".join(dispatch.excerpt.split())
        assert "Nerin Talvex recorded that Vela Quenrix left Brindlequay storehouse at 09:14" in text
        assert "carrying a teal case with a paper band" in text
        assert "Nerin Talvex did not read the band number" in text
        assert "The wall clock above the loading table had not been checked that morning" in text
        assert "This observation identifies a case, not its contents" in text
        assert "reports/05-arrival-observation.txt" in {paths[row.document_id] for row in packet}
        # A ranking rescue must not pull a source outside the selected population.
        allowed = frozenset(paths) - {dispatch.document_id}
        scoped = bench._answer_search(matter, question, retrieval_query, document_ids=allowed)
        assert all(row.document_id in allowed for row in scoped)
        assert dispatch.document_id not in {row.document_id for row in scoped}


@pytest.mark.parametrize("destination", ["checkout", "symlink", "nonempty"])
def test_seed_demo_refuses_unsafe_destinations(demo_script, tmp_path, destination):
    if destination == "checkout":
        path = ROOT / "synthetic-demo-must-not-be-created"
    elif destination == "symlink":
        link = tmp_path / "checkout-link"
        link.symlink_to(ROOT, target_is_directory=True)
        path = link / "synthetic-demo-must-not-be-created"
    else:
        path = tmp_path / "existing"
        path.mkdir()
        (path / "sentinel.txt").write_text("Keep existing data.", encoding="utf-8")
    original_environment = os.environ.copy()
    with pytest.raises(ValueError):
        with demo_script.seed_demo(path):
            pytest.fail("Unsafe destination accepted")
    assert os.environ == original_environment
    if destination == "nonempty":
        assert (path / "sentinel.txt").read_text(encoding="utf-8") == "Keep existing data."
    else:
        assert not path.exists()


def test_seed_timeout_closes_workers_and_restores_environment(demo_script, tmp_path, monkeypatch):
    original_environment = os.environ.copy()
    create_app = demo_script.create_workbench_app
    closed = []

    def capture_app(*args, **kwargs):
        app = create_app(*args, **kwargs)
        bench = app.state.workbench
        close = Mock(wraps=bench.close)
        monkeypatch.setattr(bench, "close", close)
        closed.append(close)
        return app

    monkeypatch.setattr(demo_script, "create_workbench_app", capture_app)
    with pytest.raises(TimeoutError):
        with demo_script.seed_demo(tmp_path / "timed-out-demo", timeout_seconds=0):
            pytest.fail("An expired processing deadline yielded a ready demo")
    assert len(closed) == 1
    closed[0].assert_called_once_with()
    assert os.environ == original_environment


def test_demo_corpus_passes_publication_sanitizer():
    publication = load_script(f"{LEGACY_STEM}_demo_publication_check", "publication-check.py")
    assert publication.scan_tree(ROOT / "demo_data", ()) == []
