"""Fail before blocking if a source-cache caller reverses the lock order."""
import ast
import io
from pathlib import Path
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from case_intelligence.source_cache import SourceCacheLock, source_cache_guard
from case_intelligence.workbench import create_workbench_app
from tests.test_matter_notebook import WEB_ACTOR, _create_matter


def cache_owner():
    workspace = SimpleNamespace(_lock=threading.RLock())
    return SimpleNamespace(workspace=workspace, _store_lock=SourceCacheLock(lambda: workspace._lock))


@pytest.mark.parametrize("method", ["acquire", "__enter__"])
def test_inversion_raises_before_waiting_for_contended_cache(method):
    bench = cache_owner()
    assert bench._store_lock._check_order, "CI must enable cache-order assertions"
    owned, release = threading.Event(), threading.Event()

    def hold_cache():
        with source_cache_guard(bench):
            owned.set()
            assert release.wait(5)

    holder = threading.Thread(target=hold_cache, daemon=True)
    holder.start()
    try:
        assert owned.wait(5)
        # Another thread's workspace ownership cannot authorize this thread.
        with pytest.raises(RuntimeError, match="source_cache_guard"):
            getattr(bench._store_lock, method)()
    finally:
        release.set()
        holder.join(5)
    assert not holder.is_alive()


def test_nested_guard_and_exception_release_both_locks():
    bench = cache_owner()
    with pytest.raises(ValueError, match="synthetic"):
        with source_cache_guard(bench), source_cache_guard(bench):
            assert bench.workspace._lock._is_owned()
            assert bench._store_lock._is_owned()
            raise ValueError("synthetic")
    assert not bench.workspace._lock._is_owned()
    assert not bench._store_lock._is_owned()
    with bench.workspace._lock, source_cache_guard(bench):
        assert bench._store_lock._is_owned()


def test_helper_rejects_already_inverted_ownership_before_workspace_wait():
    bench = cache_owner()
    # Model a legacy/raw acquisition that predates the checked boundary.
    with bench._store_lock._lock:
        with pytest.raises(RuntimeError, match="source_cache_guard"):
            with source_cache_guard(bench):
                pytest.fail("entered an inverted guard")


def test_production_callers_acquire_cache_only_through_helper():
    root = Path(__file__).resolve().parents[1] / "src" / "case_intelligence"
    acquisitions = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            expressions = ([item.context_expr for item in node.items] if isinstance(node, ast.With)
                           else [node.func.value] if isinstance(node, ast.Call)
                           and isinstance(node.func, ast.Attribute) and node.func.attr == "acquire" else [])
            if any(isinstance(item, ast.Attribute) and item.attr == "_store_lock" for item in expressions):
                acquisitions.append(path.name)
    assert acquisitions == ["source_cache.py"]


def test_matter_creation_cache_order(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    monkeypatch.setenv("CASE_INTELLIGENCE_AUTOMATIC_DISCOVERY", "0")
    with TestClient(create_workbench_app(tmp_path, auth_mode="test")) as client:
        bench = client.app.state.workbench
        # Explicit instrumentation also runs against the historical #216 parent.
        original = bench._store_lock
        bench._store_lock = SourceCacheLock(lambda: bench.workspace._lock)
        try:
            slug = _create_matter(client, "Synthetic lock-order creation")
            matter = bench.matter(slug, WEB_ACTOR)
            store = bench.source_store(matter)
            assert bench.source_store(matter) is store
            assert bench.workspace.matter_readiness(matter.matter_id).total_count == 0
        finally:
            bench._store_lock = original


def test_home_briefing_cache_order(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")
    monkeypatch.setenv("CASE_INTELLIGENCE_AUTOMATIC_DISCOVERY", "0")
    monkeypatch.setenv("CASE_INTELLIGENCE_BRIEFING", "1")
    with TestClient(create_workbench_app(tmp_path, auth_mode="test", background_ingestion=False)) as client:
        slug = _create_matter(client, "Synthetic lock-order Home")
        bench = client.app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        bench.source_store(matter).store_stream("Synthetic memo.txt", "text/plain",
            io.BytesIO(b"Alex Example arrived on 2024-05-06."))
        bench.run_automatic_discovery_once()
        # Arm after setup so the historical #256 parent fails on the Home path.
        original = bench._store_lock
        bench._store_lock = SourceCacheLock(lambda: bench.workspace._lock)
        try:
            response = client.get(f"/matters/{slug}/home")
            assert response.status_code == 200
            assert "data-discovery-briefing" in response.text
            assert "Alex Example" in response.text
        finally:
            bench._store_lock = original
