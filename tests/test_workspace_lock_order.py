"""Synthetic composition regressions for overlapping matter/entity actions."""

from tests.fixture_write import fixture_write
from contextlib import contextmanager
import faulthandler
import os
from pathlib import Path
import subprocess
import sys
import threading
import traceback

from fastapi.testclient import TestClient
import pytest

from case_intelligence.workbench import create_workbench_app
from case_intelligence import case_lifecycle_support
from tests.test_matter_notebook import WEB_ACTOR, _create_matter


def _overlap(runtime, action):
    # Run in a child process: a real lock regression must not hang pytest or
    # leave blocked application threads behind in the rest of the suite.
    client = TestClient(create_workbench_app(runtime, auth_mode="test"))
    client.__enter__()
    slug = _create_matter(client, "Synthetic existing matter")
    bench = client.app.state.workbench
    matter = bench.matter(slug, WEB_ACTOR)
    response = client.post(f"/matters/{slug}/uploads", files=[
        ("files", ("Synthetic memo.txt", b"Alex Example visited the synthetic depot.", "text/plain"))])
    assert response.status_code == 200
    store = bench.source_store(matter)
    document = next(iter(store.documents.values()))
    token = bench._support_token(bench._candidate(matter, document, document.parsed_units()[0], 1))
    service = bench.entity_service(matter)
    entity = service.create(matter.matter_id, WEB_ACTOR, display_name="Alex Example", support=token)
    note, _ = bench.workspace.create_notebook_item(
        matter.matter_id, WEB_ACTOR, item_type="person", status="needs_review", title="Synthetic note",
        references=(bench.notebook_reference_from_support(matter, token),))
    expected = service.detail(matter.matter_id, WEB_ACTOR, entity["entity_id"])
    assert expected[1][0]["available"]

    creation_before_cache = threading.Event()
    creation_at_cache = threading.Event()
    entity_in_transaction = threading.Event()
    source_store = bench.source_store
    cache_guard = case_lifecycle_support.source_cache_guard
    transaction = service.repository.transaction

    def paused_store(created_matter):
        assert created_matter.matter_id != matter.matter_id
        creation_before_cache.set()
        assert entity_in_transaction.wait(5), "entity did not enter its transaction"
        return source_store(created_matter)

    @contextmanager
    def observed_cache_guard(current):
        # The entity transaction owns the workspace when creation requests the
        # cache. Creation must wait without holding the shared cache lock.
        assert not current._store_lock._is_owned()
        creation_at_cache.set()
        with cache_guard(current):
            assert current.workspace._lock._is_owned()
            yield

    @contextmanager
    def paused_transaction(*args, **kwargs):
        with transaction(*args, **kwargs) as repo:
            entity_in_transaction.set()
            assert creation_at_cache.wait(5), "creation did not request the cache"
            yield repo

    bench.source_store = paused_store
    case_lifecycle_support.source_cache_guard = observed_cache_guard
    service.repository.transaction = paused_transaction
    results, errors = {}, []

    def run(name, operation):
        try:
            results[name] = operation()
        except BaseException as exc:
            errors.append(exc)

    actions = {
        "detail": lambda: service.detail(matter.matter_id, WEB_ACTOR, entity["entity_id"]),
        "export": lambda: service.export(matter.matter_id, WEB_ACTOR, entity["entity_id"]),
        "create": lambda: service.create(matter.matter_id, WEB_ACTOR, display_name="Synthetic second identity", support=token),
        "attach": lambda: service.attach(matter.matter_id, WEB_ACTOR, entity["entity_id"], expected_revision=1, support=token),
        "import": lambda: service.import_note(matter.matter_id, WEB_ACTOR, note.item_id),
    }
    creator = threading.Thread(target=run, args=("matter", lambda: bench.create_matter(
        "Synthetic concurrent matter", "Synthetic", WEB_ACTOR)), daemon=True)
    reader = threading.Thread(target=run, args=("entity", actions[action]), daemon=True)
    creator.start()
    assert creation_before_cache.wait(5), "creation did not reach source storage"
    reader.start()
    reader.join(5)
    creator.join(5)
    assert entity_in_transaction.is_set(), "actions did not overlap"
    if reader.is_alive() or creator.is_alive():
        faulthandler.dump_traceback()
    assert not reader.is_alive() and not creator.is_alive(), (
        "concurrent matter creation and entity action deadlocked on source-cache/workspace locks")
    assert not errors, errors
    bench.source_store = source_store
    case_lifecycle_support.source_cache_guard = cache_guard
    service.repository.transaction = transaction
    assert bench.matter(results["matter"].slug, WEB_ACTOR) == results["matter"]
    assert service.detail(matter.matter_id, WEB_ACTOR, entity["entity_id"]) == expected
    if action == "detail":
        assert results["entity"] == expected
    if action == "export":
        assert results["entity"]["mentions"] == expected[1]
    if action in {"create", "import"}:
        created = service.detail(matter.matter_id, WEB_ACTOR, results["entity"]["entity_id"])
        assert created[1][0]["available"]
        assert created[1][0]["support_token"] == token
        assert created[1][0]["excerpt"] == expected[1][0]["excerpt"]
    # The captured object is live: subsequent source changes must invalidate
    # old support, and authorization must still be evaluated on every action.
    store.remove(document.document_id)
    unavailable = service.detail(matter.matter_id, WEB_ACTOR, entity["entity_id"])
    assert not unavailable[1][0]["available"]
    assert unavailable[1][0]["excerpt"] == expected[1][0]["excerpt"]
    assert service.current_sources(expected[1]) == frozenset()
    with pytest.raises(KeyError):
        service.create(matter.matter_id, WEB_ACTOR, display_name="Unavailable support", support=token)
    with pytest.raises(KeyError):
        service.detail(results["matter"].matter_id, WEB_ACTOR, entity["entity_id"])
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute(
            "UPDATE workbench_principal SET active=0 WHERE principal_id=?", (WEB_ACTOR,))
    with pytest.raises(KeyError):
        service.detail(matter.matter_id, WEB_ACTOR, entity["entity_id"])
    assert bench.workspace.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    client.__exit__(None, None, None)


@pytest.mark.parametrize("action", ["detail", "export", "create", "attach", "import"])
def test_matter_creation_overlaps_entity_transaction(tmp_path, action):
    env = dict(os.environ, CASE_INTELLIGENCE_STORAGE_RESERVE_GIB="0",
               CASE_INTELLIGENCE_AUTOMATIC_DISCOVERY="0")
    result = subprocess.run(
        [sys.executable, "-m", "tests.test_workspace_lock_order", str(tmp_path / "runtime"), action],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    try:
        _overlap(Path(sys.argv[1]), sys.argv[2])
    except BaseException:
        traceback.print_exc()
        # A detected deadlock may include application-owned worker threads.
        # Exiting the isolated child is the only safe cleanup in that case.
        os._exit(1)
