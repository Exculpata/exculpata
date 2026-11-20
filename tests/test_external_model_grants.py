"""Synthetic workspace boundaries for operator-supplied model endpoints."""
from pathlib import Path
import sqlite3

import pytest

from tests.fixture_write import fixture_write

from case_intelligence.external_model_grants import configured_model_destination, model_destination
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore


def test_grant_persists_exact_identity_and_revocation_across_connections(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_URL", "https://models.example.test/")
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_MODEL", "Synthetic-A")
    dest = configured_model_destination("embedding")
    assert dest == model_destination("embedding", "https://MODELS.example.test:443/", "Synthetic-A")
    path = tmp_path / "workspace.sqlite"
    store = WorkspaceStore(path)
    for actor in ("principal-owner", "principal-member"):
        store.upsert_principal("test", actor, actor, actor, preferred_principal_id=actor)
    owner = "principal-owner"
    matter = store.create_matter("Synthetic A", "", owner).matter_id
    other = store.create_matter("Synthetic B", "", owner).matter_id
    store.add_member(matter, "principal-member", owner)
    with pytest.raises(WorkspaceProblem):
        store.set_model_grant(matter, dest, "principal-member", approved=True)
    with pytest.raises(WorkspaceProblem):
        store.set_model_grant(matter, dest, "principal-member", approved=True, administrator_override=True)
    with pytest.raises(ValueError):
        store.set_model_grant(matter, dest, owner, approved=True, request_id="")
    assert not store.model_grant_allows(matter, dest)
    store.set_model_grant(matter, dest, owner, approved=True)
    store.close()
    store = WorkspaceStore(path)
    second = WorkspaceStore(path)
    try:
        assert store.model_grants(matter) == (dest,)
        assert second.model_grant_allows(matter, dest)
        assert not second.model_grant_allows(other, dest)
        assert not second.model_grant_allows("unknown-matter", dest)
        for different in (
            model_destination("embedding", dest.endpoint, "synthetic-a"),
            model_destination("generation", dest.endpoint, dest.model),
            model_destination("embedding", dest.endpoint + ":8443", dest.model),
        ):
            assert not second.model_grant_allows(matter, different)
        store.set_model_grant(matter, dest, owner, approved=False)
        assert not second.model_grant_allows(matter, dest)
        assert second.model_grants(matter) == ()
        events = [e for e in store.audit_events(matter) if e.action.startswith("model_grant.")]
        assert [e.action for e in events] == ["model_grant.approve", "model_grant.revoke"]
        assert {e.actor_principal_id for e in events} == {owner}
        assert len({e.object_id for e in events}) == 1
    finally:
        second.close()
        store.close()


def test_legacy_workspace_upgrades_without_standing_approval(tmp_path):
    path = tmp_path / "legacy.sqlite"
    store = WorkspaceStore(path)
    store.upsert_principal("test", "owner", "Owner", "owner", preferred_principal_id="principal-owner")
    matter = store.create_matter("Synthetic legacy matter", "", "principal-owner").matter_id
    store.close()
    with sqlite3.connect(path) as legacy:
        # fixture-write: Represent a pre-grant schema in this isolated legacy database.
        legacy.execute("DROP TABLE workbench_matter_model_grant")
    migrated = WorkspaceStore(path)
    try:
        assert migrated.model_grants(matter) == ()
        assert not migrated.model_grant_allows(matter, model_destination(
            "generation", "https://models.example.test", "synthetic"))
    finally:
        migrated.close()
    migration = "0051_external_model_grants.sql"
    root = Path(__file__).resolve().parents[1]
    assert (root / "migrations/sqlite" / migration).read_bytes() == (
        root / "src/case_intelligence/migrations/sqlite" / migration).read_bytes()


def test_external_destination_config_and_origin(monkeypatch):
    monkeypatch.delenv("CASE_INTELLIGENCE_GENERATOR_BACKEND", raising=False)
    monkeypatch.delenv("CASE_INTELLIGENCE_RERANK_URL", raising=False)
    assert configured_model_destination("generation") is None
    assert configured_model_destination("reranking") is None
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_BACKEND", "openai")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_URL", "http://generator:8000")
    assert configured_model_destination("generation") is None
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_URL", "https://MODEL.example.test:8443/api")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_MODEL", "Synthetic")
    generator = configured_model_destination("generation")
    assert generator.endpoint == "https://model.example.test:8443/api"
    assert generator.capability == "generation"
    assert generator != model_destination("generation", "https://model.example.test:8443/other", "Synthetic")
    assert generator == model_destination("generation", "https://MODEL.example.test:8443/api/", "Synthetic")
    with pytest.raises(ValueError):
        model_destination("embedding", "https://models.example.test/tenant", "Synthetic")
    monkeypatch.setenv("CASE_INTELLIGENCE_RERANK_URL", "https://MODEL.example.test:443/")
    monkeypatch.setenv("CASE_INTELLIGENCE_RERANK_MODEL", "Synthetic")
    assert configured_model_destination("reranking") == model_destination(
        "reranking", "https://model.example.test", "Synthetic")
    for url in ("https://user:synthetic@models.example.test", "https://models.example.test/?key=synthetic"):
        with pytest.raises(ValueError):
            model_destination("embedding", url, "Synthetic")


def test_purging_matter_removes_its_model_grants(tmp_path, monkeypatch):
    from tests.case_lifecycle_helpers import claim_synthetic_exit

    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_URL", "https://models.example.test")
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_MODEL", "Synthetic")
    dest = configured_model_destination("embedding")
    store = WorkspaceStore(tmp_path / "control.sqlite")
    try:
        owner = "principal-owner"
        store.upsert_principal("test", owner, "Owner", "owner", preferred_principal_id=owner)
        alpha = store.create_matter("Synthetic Alpha", "", owner)
        bravo = store.create_matter("Synthetic Bravo", "", owner)
        for matter in (alpha, bravo):
            store.set_model_grant(matter.matter_id, dest, owner, approved=True)
        _, lifecycle = claim_synthetic_exit(store, alpha, owner)
        assert store.complete_matter_purge(alpha.matter_id, lifecycle.purge_id).state == "deleted"
        assert store.model_grants(alpha.matter_id) == ()
        assert store.model_grant_allows(bravo.matter_id, dest)
    finally:
        store.close()


@pytest.mark.parametrize("observation", ["startup", "review", "dispatch", "rejected-change"])
def test_observing_new_config_expires_all_matters_before_switching_back(tmp_path, monkeypatch, observation):
    key = "CASE_INTELLIGENCE_EMBEDDING_URL"
    monkeypatch.setenv(key, "https://first.example.test")
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_MODEL", "Synthetic")
    original = configured_model_destination("embedding")
    store = WorkspaceStore(tmp_path / "control.sqlite")
    try:
        actor = "principal-owner"
        store.upsert_principal("test", actor, "Owner", "owner", preferred_principal_id=actor)
        alpha, bravo = [store.create_matter(name, "", actor).matter_id for name in ("Synthetic A", "Synthetic B")]
        for matter in (alpha, bravo):
            store.set_model_grant(matter, original, actor, approved=True)
        reviewed_config = next(revision for dest, _, revision in store.model_grant_review(alpha) if dest == original)
        monkeypatch.setenv(key, "" if observation == "startup" else "https://second.example.test")
        if observation == "startup":
            store.observe_model_destinations()
        elif observation == "review":
            assert not store.model_grants(alpha)
        elif observation == "dispatch":
            assert not store.model_grant_allows(alpha, original)
        else:
            with pytest.raises(WorkspaceProblem):
                store.set_model_grant(alpha, original, actor, approved=True)
        monkeypatch.setenv(key, original.endpoint)
        for matter in (alpha, bravo):
            assert not store.model_grant_allows(matter, original)
            assert [event.action for event in store.audit_events(matter)
                    if event.action.startswith("model_grant.")] == ["model_grant.approve", "model_grant.expire"]
        with pytest.raises(WorkspaceProblem):
            store.set_model_grant(alpha, original, actor, approved=True, expected_configuration_id=reviewed_config)
        assert not store.model_grant_allows(alpha, original)
        fresh_config = next(revision for dest, _, revision in store.model_grant_review(alpha) if dest == original)
        assert fresh_config != reviewed_config
        store.set_model_grant(alpha, original, actor, approved=True, expected_configuration_id=fresh_config)
        assert store.model_grant_allows(alpha, original)
        assert not store.model_grant_allows(bravo, original)
    finally:
        store.close()


def test_speech_upgrade_preserves_existing_grants_and_configuration_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_URL", "https://index.example.test")
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_MODEL", "synthetic-embedding")
    monkeypatch.setenv("CASE_INTELLIGENCE_ASR_URL", "https://speech.example.test")
    monkeypatch.setenv("CASE_INTELLIGENCE_ASR_MODEL", "synthetic-asr")
    path = tmp_path / "matter.sqlite"
    store = WorkspaceStore(path)
    owner = "principal-owner"
    store.upsert_principal("test", owner, "Owner", "owner", preferred_principal_id=owner)
    matter = store.create_matter("Synthetic migration", "", owner).matter_id
    embedding = configured_model_destination("embedding")
    schema = Path(__file__).resolve().parents[1] / "migrations/sqlite/0051_external_model_grants.sql"
    with fixture_write(store) as legacy:
        legacy.executescript("DROP TABLE workbench_matter_model_grant; DROP TABLE workbench_model_destination;")
        legacy.executescript(schema.read_text(encoding="utf-8"))
        legacy.execute("INSERT INTO workbench_model_destination VALUES (?,?,?,?)",
                       ("embedding", embedding.endpoint, embedding.model, "synthetic-configuration"))
        legacy.execute("INSERT INTO workbench_matter_model_grant VALUES (?,?,?,?,?,?,?)", (
            "synthetic-grant", matter, "embedding", embedding.endpoint, embedding.model, 1, "synthetic-configuration",
        ))
    store.close()
    speech = configured_model_destination("transcription")
    restored = WorkspaceStore(path)
    try:
        assert (embedding, True, "synthetic-configuration") in restored.model_grant_review(matter)
        assert not restored.model_grant_allows(matter, speech)
        restored.set_model_grant(matter, speech, owner, approved=True)
    finally:
        restored.close()
    reopened = WorkspaceStore(path)
    try:
        assert set(reopened.model_grants(matter)) == {embedding, speech}
    finally:
        reopened.close()
