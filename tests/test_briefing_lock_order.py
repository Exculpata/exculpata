"""Synthetic Home briefing overlap with another matter's source lookup."""
import faulthandler
import io
import os
from pathlib import Path
import subprocess
import sys
import threading
import traceback
from contextlib import contextmanager

from fastapi.testclient import TestClient
import pytest

from case_intelligence.workbench import create_workbench_app
from case_intelligence import case_lifecycle_support
from tests.test_matter_notebook import WEB_ACTOR, _create_matter


def _overlap(runtime, automatic, cached):
    faulthandler.enable()
    faulthandler.dump_traceback_later(18, exit=True)
    client = TestClient(create_workbench_app(runtime, auth_mode="test", background_ingestion=False))
    client.__enter__()
    slug = _create_matter(client, "Synthetic Home overlap")
    other_slug = _create_matter(client, "Synthetic concurrent source lookup")
    bench = client.app.state.workbench
    matter = bench.matter(slug, WEB_ACTOR)
    other = bench.matter(other_slug, WEB_ACTOR)
    bench.source_store(matter).store_stream("Synthetic memo.txt", "text/plain",
        io.BytesIO(b"Alex Example arrived on 2024-05-06."))
    if bench.automatic_discovery is not None:
        bench.automatic_discovery.close()
    bench.run_automatic_discovery_once()
    expected = client.get(f"/matters/{slug}/home")
    assert expected.status_code == 200 and "data-discovery-briefing" in expected.text
    if not cached:
        with case_lifecycle_support.source_lock(bench, other.matter_id), case_lifecycle_support.source_cache_guard(bench):
            bench._stores.pop(other.matter_id).close()

    in_snapshot, cache_requested = threading.Event(), threading.Event()
    membership = bench.workspace.membership
    cache_guard = case_lifecycle_support.source_cache_guard
    results, errors = {}, []

    def paused_membership(*args, **kwargs):
        # Coordinate only the existing membership recheck inside Home's real
        # source guard + workspace snapshot, not unrelated authorization reads.
        if (sys._getframe(1).f_code.co_name == "home_briefing"
                and bench.workspace._lock._is_owned()):
            in_snapshot.set()
            assert cache_requested.wait(5), "Other matter did not request the source cache"
        return membership(*args, **kwargs)

    @contextmanager
    def observed_cache_guard(current):
        if threading.current_thread().name == "synthetic-other-store":
            assert not current._store_lock._is_owned()
            cache_requested.set()
        with cache_guard(current):
            assert current.workspace._lock._is_owned()
            yield

    bench.workspace.membership = paused_membership
    case_lifecycle_support.source_cache_guard = observed_cache_guard

    def run(name, operation):
        try:
            results[name] = operation()
        except BaseException:
            errors.append(traceback.format_exc())

    home = threading.Thread(target=run, name="synthetic-home-request", daemon=True,
        args=("home", lambda: client.get(f"/matters/{slug}/home")))
    lookup = threading.Thread(target=run, name="synthetic-other-store", daemon=True,
        args=("lookup", lambda: bench.automatic_entity_discovery(other)))
    home.start()
    assert in_snapshot.wait(5), "Home did not enter the real briefing snapshot"
    lookup.start()
    home.join(3)
    lookup.join(1)
    assert cache_requested.is_set(), "The operations did not overlap"
    if home.is_alive() or lookup.is_alive():
        faulthandler.dump_traceback()
        raise AssertionError("Home and other-matter source lookup deadlocked on cache/workspace locks")
    assert not errors, errors
    assert results["home"].status_code == 200
    assert "data-discovery-briefing" in results["home"].text
    assert "Alex Example" in results["home"].text
    assert client.get("/health").status_code == 200
    # The store and readiness must remain live rather than a cached snapshot.
    bench.workspace.membership = membership
    case_lifecycle_support.source_cache_guard = cache_guard
    store = bench.source_store(matter)
    document = next(iter(store.documents.values()))
    store.remove(document.document_id)
    refreshed = client.get(f"/matters/{slug}/home")
    assert refreshed.status_code == 200
    assert bench.workspace.matter_readiness(matter.matter_id).total_count == 0
    assert not store.documents
    with bench.workspace._lock:
        assert bench.workspace.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not bench.workspace.connection.execute("PRAGMA foreign_key_check").fetchall()
    client.__exit__(None, None, None)
    faulthandler.cancel_dump_traceback_later()
    print(f"Home overlap passed: automatic={automatic}, cached={cached}", flush=True)


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("cached", [False, True])
def test_home_snapshot_overlaps_other_matter_source_lookup(tmp_path, automatic, cached):
    env = dict(os.environ, CASE_INTELLIGENCE_STORAGE_RESERVE_GIB="0",
        CASE_INTELLIGENCE_AUTOMATIC_DISCOVERY="1" if automatic else "0",
        CASE_INTELLIGENCE_BRIEFING="1", CASE_INTELLIGENCE_MAINTENANCE_ENABLED="0")
    result = subprocess.run([sys.executable, "-m", "tests.test_briefing_lock_order",
        str(tmp_path / "runtime"), str(int(automatic)), str(int(cached))],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    try:
        _overlap(Path(sys.argv[1]), bool(int(sys.argv[2])), bool(int(sys.argv[3])))
    except BaseException:
        traceback.print_exc()
        # A blocked child may own non-daemon application workers. Never join
        # those on failure or leave them behind in the parent pytest process.
        os._exit(1)
