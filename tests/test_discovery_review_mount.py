"""Synthetic acceptance for the first registered review module."""
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from case_intelligence import managed_storage
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.modules import Module, registry
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workspace_store import WorkspaceProblem
from tests.case_lifecycle_web_helpers import claim_bench_exit
from tests.test_compact_matter_navigation import DESTINATIONS, navigation
from tests.test_matter_notebook import WEB_ACTOR, _create_matter


@pytest.fixture
def review(tmp_path, monkeypatch):
    captured = {}
    def capture(app, **kwargs):
        captured.update(kwargs)
    probe = Module(name="probe", version="0", layer="modules.discovery_review", route_hook=capture)
    monkeypatch.setattr(registry, "REGISTERED", (*registry.REGISTERED, probe))
    app = create_workbench_app(tmp_path / "runtime", auth_mode="test", generator=UnavailableGenerator())
    with TestClient(app) as client:
        bench = app.state.workbench
        slug = _create_matter(client, "Synthetic review matter")
        matter = bench.matter(slug, WEB_ACTOR)
        yield client, bench, matter, captured


def test_mount_count_and_navigation(review, monkeypatch):
    client, bench, matter, captured = review
    url = f"/matters/{matter.slug}/production"
    empty = client.get(url)
    assert empty.status_code == 200
    assert "0 documents" in empty.text
    other_slug = _create_matter(client, "Synthetic neighboring matter")
    for slug, names in ((matter.slug, ("one.txt", "two.txt")), (other_slug, ("neighbor.txt",))):
        for name in names:
            response = client.post(f"/matters/{slug}/uploads", files={"files": (name, b"Synthetic record", "text/plain")})
            assert response.status_code == 200
    context = captured["context"]
    page = client.get(url)
    assert page.status_code == 200 and "2 documents" in page.text
    assert "neighbor.txt" not in page.text
    assert f'href="/matters/{matter.slug}/setup?view=list&amp;page_size=100#source-library">Back to Review' in page.text
    assert "1 document" in client.get(f"/matters/{other_slug}/production").text
    links = navigation(page.text, "Matter workspace sections")
    assert [link["text"] for link in links] == DESTINATIONS
    assert [link["text"] for link in links if link["current"] == "page"] == ["Review"]
    setup = client.get(f"/matters/{matter.slug}/setup?view=list&page_size=100")
    assert any(link["text"] == "Production documents" and link["href"] == url
               for link in navigation(setup.text, "Search and screening"))
    assert "discovery-review.css" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert bench.matter_active_work_counts(matter.matter_id)["exports"] == 0
    assert not (context.matters_root / matter.matter_id / "review-render-cache").exists()
    with pytest.raises(FrozenInstanceError):
        context.scan_mode = "changed"
    assert context.db.connection is bench.workspace.connection
    assert context.db.lock is bench.workspace._lock


def test_membership_wrong_matter_and_revocation(review, monkeypatch):
    client, bench, matter, captured = review
    assert client.get(f"/matters/{matter.slug}/production").status_code == 200
    principal = bench.workspace.upsert_principal("test", "other", "Synthetic other", "other")
    other = bench.workspace.create_matter("Synthetic private matter", "Synthetic", principal.principal_id)
    for slug in (other.slug, "missing-matter", matter.matter_id):
        assert client.get(f"/matters/{slug}/production").status_code == 404
    context = captured["context"]
    membership = context.membership
    calls = []
    def revoked(matter_id, actor):
        calls.append((matter_id, actor))
        if len(calls) == 2:
            raise KeyError(actor)
        return membership(matter_id, actor)
    monkeypatch.setattr(type(context), "membership", staticmethod(revoked))
    assert client.get(f"/matters/{matter.slug}/production").status_code == 404
    assert calls == [(matter.matter_id, WEB_ACTOR)] * 2


def test_reservation_aware_capacity(review, monkeypatch):
    _, bench, _, captured = review
    context = captured["context"]
    held = []
    @contextmanager
    def reservation_lock():
        held.append(True)
        try:
            yield
        finally:
            held.pop()
    def pending():
        assert held
        return 30
    def playback():
        assert held
        return 20
    def disk_usage(path):
        assert held
        return SimpleNamespace(total=1000, used=900, free=100)
    monkeypatch.setattr(bench, "_storage_reservation_lock", reservation_lock())
    monkeypatch.setattr(bench.workspace, "pending_upload_bytes", pending)
    monkeypatch.setattr(bench, "_playback_reserved_bytes", playback)
    monkeypatch.setattr(managed_storage.shutil, "disk_usage", disk_usage)
    context.ensure_capacity(50)
    monkeypatch.setattr(bench, "_storage_reservation_lock", reservation_lock())
    with pytest.raises(RuntimeError, match="capacity"):
        context.ensure_capacity(51)
    assert not held
    for invalid in (-1, True, 1.5, "1"):
        with pytest.raises(ValueError, match="byte count"):
            context.ensure_capacity(invalid)
    assert not held


def test_capacity_subtracts_the_configured_reserve_under_the_real_lock(review, monkeypatch):
    _, bench, _, captured = review
    monkeypatch.setattr(bench.storage, "policy", replace(bench.storage.policy, reserve_bytes=10))
    monkeypatch.setattr(bench.workspace, "pending_upload_bytes", lambda: 30)
    monkeypatch.setattr(bench, "_playback_reserved_bytes", lambda: 20)
    monkeypatch.setattr(managed_storage.shutil, "disk_usage",
                        lambda path: SimpleNamespace(total=1000, used=900, free=100))
    captured["context"].ensure_capacity(40)
    with pytest.raises(RuntimeError, match="capacity"):
        captured["context"].ensure_capacity(41)


@pytest.mark.parametrize("fails", [False, True])
def test_execute_purge_hook_and_failure(review, monkeypatch, fails):
    _, bench, matter, _ = review
    module = registry.lookup("discovery_review")
    assert module is not None and module.purge_hook is not None
    root = bench.storage.matters / matter.matter_id
    cache = root / "review-render-cache"
    cache.mkdir(parents=True)
    (cache / "page.png").write_bytes(b"synthetic cache")  # fixture-write: synthetic render cache
    neighbor = bench.storage.matters / "synthetic-neighbor" / "review-render-cache"
    neighbor.mkdir(parents=True)
    calls = []
    def hook(matter_root, matter_id):
        calls.append((matter_root, matter_id))
        if fails:
            raise OSError("synthetic cleanup failure")
        module.purge_hook(matter_root, matter_id)
        assert (root / "sources").is_dir()
    monkeypatch.setattr(registry, "REGISTERED", (replace(module, purge_hook=hook),))
    prepared, lifecycle = claim_bench_exit(bench, matter)
    if fails:
        with pytest.raises(WorkspaceProblem, match="Matter deletion stopped"):
            bench.execute_matter_purge(prepared, lifecycle)
        result = bench.workspace.matter_lifecycle(matter.matter_id)
        assert result.state == "purge_failed" and result.error_code == "storage"
        assert cache.exists() and (root / "sources").exists()
    else:
        assert bench.execute_matter_purge(prepared, lifecycle).state == "deleted"
        assert not cache.exists()
    assert calls == [(root, matter.matter_id)]
    assert neighbor.is_dir()


def test_purge_is_idempotent_and_refuses_symlinks(tmp_path):
    from case_intelligence.modules.discovery_review.purge import purge_hook
    root = tmp_path / ("ci-matter-" + "0" * 32)
    root.mkdir()
    sources = root / "sources"
    sources.mkdir()
    purge_hook(root, root.name)
    for malformed in ("ci-matter-short", "../ci-matter-" + "0" * 32, ""):
        with pytest.raises(RuntimeError, match="unsafe"):
            purge_hook(root, malformed)
    assert sources.is_dir()
    cache = root / "review-render-cache"
    cache.symlink_to(sources, target_is_directory=True)  # fixture-write: synthetic unsafe cache
    with pytest.raises(RuntimeError):
        purge_hook(root, root.name)
    assert sources.is_dir()
