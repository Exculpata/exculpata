from __future__ import annotations

from dataclasses import replace

import pytest

from case_intelligence.inventory import InventoryError, InventoryService
from case_intelligence.sqlite_store import SQLiteStore
from tests.slice1_support import NOW, make_environment


def scalar(store, sql, args=()):
    # fixture-write: Every scalar caller passes a read-only SELECT count query.
    return store.connection.execute(sql, args).fetchone()[0]


def test_atomic_incremental_version_missing_reappearance_and_no_move_inference(tmp_path):
    store, _, inventory, _, roots = make_environment(tmp_path)
    first = inventory.inventory("matter-alpha", "location-alpha", "scan-1")
    assert (first.new, first.changed, first.missing) == (3, 0, 0)
    assert scalar(store, "SELECT count(*) FROM source_version WHERE matter_id='matter-alpha'") == 3
    assert scalar(store, "SELECT count(*) FROM job WHERE matter_id='matter-alpha' AND state='queued'") == 1

    same = inventory.inventory("matter-alpha", "location-alpha", "scan-2")
    assert same.unchanged == 3
    assert scalar(store, "SELECT count(*) FROM source_version WHERE matter_id='matter-alpha'") == 3

    report = roots["alpha"] / "nested/report.txt"
    original = report.read_bytes()
    report.write_bytes(original + b"new bytes\n")
    changed = inventory.inventory("matter-alpha", "location-alpha", "scan-3")
    assert changed.changed == 1
    assert scalar(store, "SELECT count(*) FROM source_version WHERE matter_id='matter-alpha'") == 4
    assert scalar(store, "SELECT count(*) FROM job WHERE matter_id='matter-alpha' AND state='queued'") == 2

    report.unlink()
    missing = inventory.inventory("matter-alpha", "location-alpha", "scan-4")
    assert missing.missing == 1
    assert scalar(store, "SELECT count(*) FROM source_file WHERE availability='missing'") == 1
    report.write_bytes(original + b"new bytes\n")
    reappeared = inventory.inventory("matter-alpha", "location-alpha", "scan-5")
    assert reappeared.reappeared == 1
    assert scalar(store, "SELECT count(*) FROM source_version WHERE matter_id='matter-alpha'") == 4

    report.rename(roots["alpha"] / "renamed.txt")
    renamed = inventory.inventory("matter-alpha", "location-alpha", "scan-6")
    assert renamed.new == 1 and renamed.missing == 1
    assert scalar(store, "SELECT count(*) FROM source_file WHERE matter_id='matter-alpha'") == 4
    store.close()


def test_two_connection_catalog_cas_supersedes_older_snapshot(tmp_path):
    store_a, registry, inventory_a, _, roots = make_environment(tmp_path)
    old = inventory_a.prepare_snapshot("matter-alpha", "location-alpha", "old")
    report = roots["alpha"] / "nested/report.txt"
    report.write_bytes(report.read_bytes() + b"newer\n")

    store_b = SQLiteStore(store_a.path)
    inventory_b = InventoryService(store_b, registry, clock=lambda: NOW)
    newer = inventory_b.prepare_snapshot("matter-alpha", "location-alpha", "newer")
    assert inventory_b.apply_snapshot(newer).state == "succeeded"
    current = scalar(store_b, "SELECT count(*) FROM source_version WHERE matter_id='matter-alpha'")
    assert inventory_a.apply_snapshot(old).state == "superseded"
    assert scalar(store_a, "SELECT count(*) FROM source_version WHERE matter_id='matter-alpha'") == current
    assert scalar(store_a, "SELECT count(*) FROM scan_run WHERE state='superseded'") == 1
    store_b.close(); store_a.close()


def test_stale_missing_snapshot_cannot_mark_newer_catalog_missing(tmp_path):
    store_a, registry, inventory_a, _, roots = make_environment(tmp_path)
    inventory_a.inventory("matter-alpha", "location-alpha", "initial")
    report = roots["alpha"] / "nested/report.txt"
    saved = report.read_bytes(); report.unlink()
    stale_missing = inventory_a.prepare_snapshot("matter-alpha", "location-alpha", "missing-old")
    report.write_bytes(saved + b"replacement\n")
    store_b = SQLiteStore(store_a.path)
    inventory_b = InventoryService(store_b, registry, clock=lambda: NOW)
    fresh = inventory_b.prepare_snapshot("matter-alpha", "location-alpha", "fresh")
    assert inventory_b.apply_snapshot(fresh).changed == 1
    assert inventory_a.apply_snapshot(stale_missing).state == "superseded"
    assert scalar(store_b, "SELECT count(*) FROM source_file WHERE availability='missing'") == 0
    store_b.close(); store_a.close()


@pytest.mark.parametrize(
    ("source", "target"),
    (("alpha", "bravo"), ("bravo", "alpha")),
)
def test_snapshot_cannot_be_substituted_across_matter_scan_runs(tmp_path, source, target):
    store, _, inventory, _, _ = make_environment(tmp_path)
    snapshot = inventory.prepare_snapshot(
        f"matter-{source}", f"location-{source}", f"{source}-substitution"
    )
    substituted = replace(
        snapshot,
        matter_id=f"matter-{target}",
        source_location_id=f"location-{target}",
    )

    with pytest.raises(InventoryError, match="does not match its running scan"):
        inventory.apply_snapshot(substituted)

    run = store.connection.execute(
        "SELECT matter_id,state FROM scan_run WHERE scan_run_id=?",
        (snapshot.scan_run_id,),
    ).fetchone()
    assert tuple(run) == (f"matter-{source}", "failed")
    assert scalar(store, "SELECT count(*) FROM scan_observation") == 0
    assert scalar(
        store,
        "SELECT count(*) FROM source_file WHERE matter_id=?",
        (f"matter-{target}",),
    ) == 0
    store.close()
