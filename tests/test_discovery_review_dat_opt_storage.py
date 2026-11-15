"""Generated Item 114 storage, lifecycle and full-pipeline memory acceptance.

The subprocess profiles intentionally require the real stored-index parent.
They do not skip, fabricate its tables, or substitute a parser-only benchmark
when that implementation is absent. Historical manifests below exercise the
public migration of previously retained originals; they do not assert that a
malformed load file would pass current upload admission.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import html
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import re
import subprocess
import sys
import threading
import time
import tracemalloc
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.matter_archive_schema import (
    EXCLUDED_TABLES, SCHEMA, exported_columns, validate_schema,
)
from case_intelligence.modules import Module, ModuleContext, ModuleDatabase, registry
from case_intelligence.modules.discovery_review.fields import load_definition
from case_intelligence.modules.discovery_review.production.repository import (
    ProductionConflict, ProductionRepository, ProductionStale,
    ProductionUnavailable, SourceIdentity, decode_cursor,
)
from case_intelligence.modules.discovery_review.production.service import (
    PARSER_REVISION, ProductionService, decoded_payload_bytes, definition_digest,
)
from case_intelligence.pilot_uploads import PilotStore
from case_intelligence.source_load_files import RecordEvent, parse_dat, parse_opt
from case_intelligence.workbench import create_workbench_app
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore
from tests.fixture_write import fixture_write


MIB = 1024 * 1024
ROW_BOUND = 256
BYTE_BOUND = 4 * MIB
ACTOR = "development-taylor-morgan"
STAMP = "2026-01-01T00:00:00+00:00"
HEADERS = ("BEGDOC", "ENDDOC", "NATIVEPATH", "BEGATTACH", "ENDATTACH", "Generated text")
PRODUCTION_TABLES = (
    "workbench_discovery_production", "workbench_discovery_dat_record",
    "workbench_discovery_opt_page",
)


@pytest.fixture(autouse=True)
def zero_storage_reserve(monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_STORAGE_RESERVE_GIB", "0")


def dat_line(values):
    return ("\x14".join("þ" + value + "þ" for value in values) + "\r\n").encode("utf-8")


def generated_text(ordinal, byte_count):
    prefix = f"Generated {ordinal:08d} ® "
    remaining = byte_count - len(prefix.encode("utf-8"))
    return prefix + "界" * (remaining // 3) + "x" * (remaining % 3)


def native_id(ordinal):
    return f"{ordinal + 1:032x}"


def native_path(ordinal):
    return f"Generated/natives/item-{ordinal:08d}.txt"


def malformed_at(profile, ordinal, count):
    return profile == "malformed" and ordinal % 4 == 0 and ordinal != count


def generated_values(profile, ordinal, count, width):
    # One historical receipt can contain 9,999 natives plus its DAT. The large
    # family therefore retains equivalent duplicate rows for those members;
    # all 20,000 raw records still belong to one proven, publishable family.
    if profile == "family":
        ordinal = (ordinal - 1) % min(count, 9999) + 1
    label = f"GEN{ordinal:08d}"
    family = ("GEN00000001", f"GEN{min(count, 9999):08d}") if profile == "family" else ("", "")
    values = (label, label, native_path(ordinal), *family, generated_text(ordinal, width))
    if malformed_at(profile, ordinal, count):
        values = (*values[:-1], values[-1] + "\r\nGenerated second physical line", "extra")
    return values


def write_profile(path, profile, count, width):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(dat_line(HEADERS))
        for ordinal in range(1, count + 1):
            stream.write(dat_line(generated_values(profile, ordinal, count, width)))
    assert path.stat().st_size <= 25 * MIB


def workspace_with_matter(path, name="Generated production matter", *, provider="test"):
    workspace = WorkspaceStore(path)
    workspace.upsert_principal(provider, "taylor-morgan" if provider == "preview" else ACTOR,
                               "Generated identity", "generated",
                               preferred_principal_id=ACTOR)
    matter = workspace.create_matter(name, "Generated orientation", ACTOR)
    return workspace, matter


def source_identity(document, repository, matter):
    return SourceIdentity(document.document_id, document.version_id, document.digest,
                          document.display_name, document.relative_path,
                          Path(document.display_name).suffix.lstrip(".").lower(),
                          repository.provenance(matter.matter_id, document.document_id,
                                                document.version_id))


def _catalog_row(document):
    return dict(document_id=document.document_id, version_id=document.version_id,
                action_token=PilotStore.action_token(document), display_name=document.display_name,
                relative_path=document.relative_path, media_type=document.media_type,
                kind="TXT",
                source_state=document.state, tone="ready", state_label="Ready",
                count_label="Generated count", byte_size=document.size,
                source_sha256=document.digest, origin=document.origin)


def _seed_receipts(workspace, matter, documents):
    """Bounded synthetic historical provenance, never a browser-admission proof.

    Each receipt respects the actual 10,000-item constraint. Consequently the
    20,000-source profile includes genuinely foreign-production candidates.
    """
    batch = []
    for document in documents:
        batch.append(document)
        if len(batch) == 10_000:
            _receipt(workspace, matter, batch)
            batch.clear()
    if batch:
        _receipt(workspace, matter, batch)


def _receipt(workspace, matter, documents):
    key = matter.matter_id + "-" + documents[0].document_id
    receipt_id, session_id, collection_id = (prefix + key for prefix in ("receipt-", "upload-", "collection-"))
    with fixture_write(workspace) as connection:
        connection.execute(
            "INSERT INTO workbench_source_collection "
            "(collection_id,matter_id,name,name_key,kind,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (collection_id, matter.matter_id, key, key, "migrated", ACTOR, STAMP, STAMP))
        connection.execute(
            "INSERT INTO workbench_intake_receipt "
            "(receipt_id,matter_id,actor_id,selection_key,selection_fingerprint,collection_name,"
            "selected_count,eligible_indexes_json,state,created_at,updated_at,metadata_bytes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (receipt_id, matter.matter_id, ACTOR, key, "a" * 64, key, len(documents),
             json.dumps(list(range(len(documents)))), "ready", STAMP, STAMP, 0))
        connection.execute(
            "INSERT INTO workbench_upload_session "
            "(upload_session_id,matter_id,collection_id,actor_id,state,item_count,total_bytes,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, matter.matter_id, collection_id, ACTOR, "complete", len(documents),
             sum(document.size for document in documents), STAMP, STAMP))
        for ordinal, document in enumerate(documents):
            upload_id = "item-" + key + "-" + str(ordinal)
            connection.execute(
                "INSERT INTO workbench_intake_item "
                "(receipt_id,matter_id,ordinal,descriptor_digest,relative_path,display_name,"
                "expected_size,expected_type,supplied_type,preflight_state,selected_for_upload,reason,"
                "reviewed_state,reviewed_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (receipt_id, matter.matter_id, ordinal, document.digest, document.relative_path,
                 document.display_name, document.size, document.media_type, document.media_type,
                 "valid", 1, "Generated historical receipt", "valid", "Generated historical receipt"))
            connection.execute(
                "INSERT INTO workbench_upload_item "
                "(upload_item_id,upload_session_id,matter_id,ordinal,display_name,relative_path,media_type,"
                "expected_size,received_size,state,document_id,message,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (upload_id, session_id, matter.matter_id, ordinal + 1, document.display_name,
                 document.relative_path, document.media_type, document.size, document.size,
                 "queued", document.document_id, "Generated historical transfer", STAMP))
            connection.execute(
                "INSERT INTO workbench_intake_transfer "
                "(receipt_id,matter_id,ordinal,upload_item_id,source_version_id) VALUES (?,?,?,?,?)",
                (receipt_id, matter.matter_id, ordinal, upload_id, document.version_id))


def historical_store(workspace, matter, root, *, profile="clean", count=3, width=512,
                     extra_files=(), primary_bytes=None):
    """Create exact generated originals, then use the real manifest migration."""
    files = root / "files"
    files.mkdir(parents=True)
    primary = files / "00000000000000000000000000000001"
    if primary_bytes is None:
        write_profile(primary, profile, count, width)
    else:
        primary.write_bytes(primary_bytes)
    with (root / "manifest.json").open("w", encoding="utf-8") as manifest:
        manifest.write('{"version":4,"documents":[')
        first = True

        def entry(document_id, relative_path, raw=None):
            nonlocal first
            path = files / document_id
            if raw is not None:
                path.write_bytes(raw)
            with path.open("rb") as original:
                digest = hashlib.file_digest(original, "sha256").hexdigest()
            payload = dict(document_id=document_id, version_id=document_id,
                display_name=Path(relative_path).name, stored_name=document_id,
                media_type="text/plain", size=path.stat().st_size, state="ready",
                message="Generated previously retained original", units=[], digest=digest,
                name_key="upload:" + relative_path, origin="upload", relative_path=relative_path)
            if not first:
                manifest.write(",")
            json.dump(payload, manifest, ensure_ascii=False)
            first = False

        entry("00000000000000000000000000000001", "Generated/metadata.dat")
        native_count = min(count, 9999) if profile == "family" else count
        for ordinal in range(1, native_count + 1):
            entry(native_id(ordinal), native_path(ordinal), f"Generated native {ordinal}\n".encode())
        for ordinal, (name, raw) in enumerate(extra_files, native_count + 2):
            entry(f"{ordinal:032x}", "Generated/" + name, raw)
        manifest.write("]}")
    store = PilotStore(root)
    rows = []
    for document in store.documents.values():
        assert document.state == "ready"
        rows.append(_catalog_row(document))
        if len(rows) == ROW_BOUND:
            workspace.upsert_source_catalog(matter.matter_id, rows)
            rows.clear()
    if rows:
        workspace.upsert_source_catalog(matter.matter_id, rows)
    _seed_receipts(workspace, matter, store.documents.values())
    return store


@contextmanager
def storage_case(tmp_path, **profile):
    workspace, matter = workspace_with_matter(tmp_path / "workspace.sqlite")
    store = historical_store(workspace, matter, tmp_path / "sources", **profile)
    policy = StoragePolicy(reserve_bytes=0)

    def capacity(byte_count):
        if shutil.disk_usage(tmp_path).free < byte_count + policy.reserve_bytes:
            raise RuntimeError("Generated fixture requires available protected storage")

    context = ModuleContext(ModuleDatabase(workspace.connection, workspace._lock),
        workspace.membership, lambda unused: store, tmp_path, None, "extended", capacity)
    repository = ObservedRepository(context.db)
    definition = load_definition()
    service = ProductionService(context, repository=repository)
    service.bind(matter, store, definition)
    case = SimpleNamespace(workspace=workspace, matter=matter, store=store,
        context=context, repository=repository, definition=definition, service=service)
    try:
        yield case
    finally:
        case.store.close()
        case.workspace.close()


@contextmanager
def app_case(tmp_path, **profile):
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    workspace, matter = workspace_with_matter(runtime / "workbench.sqlite", provider="preview")
    warm = workspace.create_matter("Generated warm-up matter", "Generated orientation", ACTOR)
    neighbor = workspace.create_matter("Generated neighboring matter", "Generated orientation", ACTOR)
    for owner, options in ((matter, profile), (warm, dict(count=2)), (neighbor, dict(count=1))):
        store = historical_store(workspace, owner, runtime / "matters" / owner.matter_id / "sources", **options)
        store.close()
    workspace.close()
    captured = {}

    def capture(app, **kwargs):
        captured.update(kwargs)

    probe = Module(name="production_storage_probe", version="0", layer="modules.discovery_review", route_hook=capture)
    with patch.object(registry, "REGISTERED", (*registry.REGISTERED, probe)):
        app = create_workbench_app(runtime, auth_mode="test", generator=UnavailableGenerator(),
                                  background_ingestion=False, storage_policy=StoragePolicy(reserve_bytes=0))
    with TestClient(app) as client:
        bench = app.state.workbench
        context = captured["context"]
        repository = ObservedRepository(context.db)
        definition = load_definition()
        service = ProductionService(context, repository=repository)
        stores = {owner.matter_id: context.source_store(owner) for owner in (matter, warm, neighbor)}
        for owner in (matter, warm, neighbor):
            service.bind(owner, stores[owner.matter_id], definition)
        yield SimpleNamespace(workspace=bench.workspace, matter=matter, warm=warm, neighbor=neighbor,
            store=stores[matter.matter_id], stores=stores, context=context, repository=repository,
            definition=definition, service=service, client=client, bench=bench)


class ObservedRepository(ProductionRepository):
    """Observe actual persistence and bounded matching/QC reads, preserving work."""

    def __init__(self, db):
        super().__init__(db)
        self.reset_observations()

    def reset_observations(self, *, retain_batches=True):
        self.batches = []
        self.retain_batches = retain_batches
        self.previous_batch = None
        self.batch_count = 0
        self.max_batch_rows = 0
        self.max_batch_bytes = 0
        self.control_queue_bytes = 0
        self.queue_max_rows = 0
        self.queue_max_bytes = 0
        self.read_calls = 0
        self.control = "none"
        self.retained = []
        self.retained_keys = set()

    def append_events(self, matter_id, attempt_id, events, *, cursor=None):
        events = tuple(events)
        sizes = tuple(decoded_payload_bytes(event) for event in events)
        result = super().append_events(matter_id, attempt_id, events, cursor=cursor)
        observation = (len(events), sum(sizes), sizes[0] if sizes else 0,
                       tuple(event.ordinal for event in events))
        self.batch_count += 1
        self.max_batch_rows = max(self.max_batch_rows, observation[0])
        self.max_batch_bytes = max(self.max_batch_bytes, observation[1])
        if self.retain_batches:
            self.batches.append(observation)
        else:
            assert 0 < observation[0] <= ROW_BOUND
            assert observation[1] <= BYTE_BOUND or observation[0] == 1
            if self.previous_batch is not None:
                previous_rows, previous_bytes = self.previous_batch
                if previous_rows < ROW_BOUND and previous_bytes < BYTE_BOUND:
                    assert previous_bytes + observation[2] > BYTE_BOUND
            self.previous_batch = observation[:2]
        if self.control == "rows":
            self.retained.extend(events)
        elif self.control == "diagnostics":
            self.retained.extend((event.line, event.cells, event.diagnostics)
                                 for event in events if event.kind == "rejected")
        elif self.control == "count_only":
            # The deliberately broken batching control holds <=256 wide rows
            # across real commits, exactly the regression the byte bound forbids.
            self.retained.extend(events)
            self.control_queue_bytes += sum(sizes)
        return result

    def observe(self, rows, *, retain_family=False):
        self.read_calls += 1
        self.queue_max_rows = max(self.queue_max_rows, len(rows))
        payload = sum(sum(len(json.dumps(row.get(key), ensure_ascii=False).encode())
                          for key in ("headers", "cells", "diagnostics", "optional_values"))
                      for row in rows)
        self.queue_max_bytes = max(self.queue_max_bytes, payload)
        if self.control == "families" and retain_family:
            for row in rows:
                identity = (row["matter_id"], row["attempt_id"], row["ordinal"])
                if identity not in self.retained_keys:
                    self.retained_keys.add(identity)
                    self.retained.append(row)
        return rows

    def records(self, *args, **kwargs):
        return self.observe(super().records(*args, **kwargs))

    def candidates(self, *args, **kwargs):
        return self.observe(super().candidates(*args, **kwargs))

    def candidate_lookup(self, *args, **kwargs):
        return self.observe(super().candidate_lookup(*args, **kwargs))

    def source_catalog_page(self, *args, **kwargs):
        return self.observe(super().source_catalog_page(*args, **kwargs))

    def records_by_key(self, *args, **kwargs):
        return self.observe(super().records_by_key(*args, **kwargs))

    def competing_records(self, *args, **kwargs):
        return self.observe(super().competing_records(*args, **kwargs))

    def family_members(self, *args, **kwargs):
        return self.observe(super().family_members(*args, **kwargs), retain_family=True)

    def interval_overlap(self, *args, **kwargs):
        return self.observe(super().interval_overlap(*args, **kwargs), retain_family=True)

    def root_records(self, *args, **kwargs):
        return self.observe(super().root_records(*args, **kwargs), retain_family=True)


def parse_into(repository, matter_id, source, path, *, parser_revision=PARSER_REVISION,
               definition="generated-definition", target="generated-target"):
    attempt_id = repository.begin_attempt(matter_id, source, parser_revision=parser_revision,
        definition_digest=definition, target=target)
    batch = []

    def persist(event):
        if batch and (len(batch) == ROW_BOUND or sum(map(decoded_payload_bytes, batch))
                      + decoded_payload_bytes(event) > BYTE_BOUND):
            repository.append_events(matter_id, attempt_id, batch)
            batch.clear()
        batch.append(event)

    parser = parse_opt if source.source_format == "opt" else parse_dat
    for unused in parser(path, on_event=persist):
        del unused
    if batch:
        repository.append_events(matter_id, attempt_id, batch)
    return attempt_id


def all_records(repository, matter_id, attempt_id, *, category="document", limit=100):
    after = 0
    while rows := repository.records(matter_id, attempt_id, category=category, after=after, limit=limit):
        assert 1 <= len(rows) <= 100
        for row in rows:
            assert row["ordinal"] > after
            after = row["ordinal"]
            yield row


def finish_import(case, *, matter=None, max_records=ROW_BOUND, max_bytes=BYTE_BOUND):
    matter = matter or case.matter
    store = case.stores[matter.matter_id] if hasattr(case, "stores") else case.store
    document = store.documents["00000000000000000000000000000001"]
    source = source_identity(document, case.repository, matter)
    path = store.source_path(document.document_id, verify_digest=True)
    target = "generated-target:" + matter.matter_id
    deadline = time.monotonic() + 1800
    while True:
        attempt = case.service.import_source(matter.matter_id, source, path, case.definition,
            target=target, max_records=max_records, max_bytes=max_bytes)
        assert attempt["status"] == "building", attempt
        if attempt["phase"] != "parse":
            break
        assert time.monotonic() < deadline, "production parsing made insufficient progress"
    while True:
        result = case.service.advance(matter.matter_id, max_steps=128)
        assert result["state"] != "error", result
        if result["state"] == "complete":
            break
        assert time.monotonic() < deadline, "production matching/QC/publication did not finish"
    attempt = case.repository.attempt(matter.matter_id, attempt["attempt_id"])
    assert attempt["status"] == "complete"
    return attempt


def reopen_storage_case(case, root):
    """Replace closed owners with actual reopened DB/source capabilities."""
    case.store.close()
    case.workspace.close()
    case.workspace = WorkspaceStore(root / "workspace.sqlite")
    case.store = PilotStore(root / "sources")
    case.context = replace(case.context,
        db=ModuleDatabase(case.workspace.connection, case.workspace._lock),
        membership=case.workspace.membership, source_store=lambda unused: case.store)
    case.repository = ObservedRepository(case.context.db)
    case.service = ProductionService(case.context, repository=case.repository)
    case.service.bind(case.matter, case.store, case.definition)


@pytest.mark.parametrize("header_bytes,expected_headers,expected_codes", [
    (dat_line(("Control", "", "Control")), ["Control", "", "Control"],
     {"duplicate_header", "empty_header"}),
    (b"\x81\r\n", None, {"invalid_header"}),
])
def test_header_diagnostics_survive_restart_without_a_fabricated_record(
        tmp_path, header_bytes, expected_headers, expected_codes):
    row_bytes = dat_line(("GEN-A", "Generated retained value", "GEN-A"))
    if expected_headers is None:
        row_bytes = row_bytes.decode("utf-8").encode("cp1252")
    raw = header_bytes + row_bytes
    with storage_case(tmp_path, count=1, extra_files=(("historical-invalid-header.dat", raw),)) as case:
        document = next(document for document in case.store.documents.values()
                        if document.display_name == "historical-invalid-header.dat")
        source = source_identity(document, case.repository, case.matter)
        failed = case.service.import_source(case.matter.matter_id, source,
            case.store.source_path(document.document_id, verify_digest=True), case.definition,
            target="generated-header-diagnostics-target")
        assert failed["status"] == "failed"
        assert failed["counts"]["accepted"] == 1 and failed["counts"]["rejected"] == 0
        assert failed["headers"] == expected_headers
        assert json.loads(failed["file_failure"])["header"] == [1, sorted(expected_codes)]
        attempt_id = failed["attempt_id"]
        reopen_storage_case(case, tmp_path)
        preserved = case.repository.attempt(case.matter.matter_id, attempt_id)
        assert preserved["status"] == "failed"
        assert preserved["counts"] == failed["counts"]
        assert preserved["headers"] == expected_headers
        assert json.loads(preserved["file_failure"])["header"] == [1, sorted(expected_codes)]
        rows = tuple(all_records(case.repository, case.matter.matter_id, attempt_id))
        assert len(rows) == 1
        assert (rows[0]["ordinal"], rows[0]["line"]) == (1, 2)
        assert rows[0]["cells"] == ["GEN-A", "Generated retained value", "GEN-A"]
        with pytest.raises(ProductionConflict):
            case.repository.update_attempt(case.matter.matter_id, attempt_id,
                                           status="complete", phase="complete")
        assert case.repository.document_records(case.matter.matter_id, native_id(1)) == ()


def test_all_rejected_locators_survive_restart(tmp_path):
    workspace, matter = workspace_with_matter(tmp_path / "workspace.sqlite")
    path = tmp_path / "generated-rejections.dat"
    with path.open("wb") as stream:
        stream.write(dat_line(("Control", "Generated text")))
        stream.write(dat_line(("FIRST", "Generated accepted neighbor")))
        for ordinal in range(2501):
            stream.write(dat_line((f"BAD{ordinal}", "Generated first\r\nGenerated second", "extra")))
        stream.write(dat_line(("LAST", "Generated accepted tail")))
    source = SourceIdentity("generated-dat", "version-one", hashlib.sha256(path.read_bytes()).hexdigest(),
                            "generated-rejections.dat", "Generated/generated-rejections.dat", "dat")
    repository = ProductionRepository(ModuleDatabase(workspace.connection, workspace._lock))
    attempt_id = parse_into(repository, matter.matter_id, source, path)
    before = repository.attempt(matter.matter_id, attempt_id)
    assert before["counts"]["accepted"] == 2
    assert before["counts"]["rejected"] == 2501
    workspace.close()
    reopened = WorkspaceStore(tmp_path / "workspace.sqlite")
    try:
        repository = ProductionRepository(ModuleDatabase(reopened.connection, reopened._lock))
        rejected = accepted = 0
        for row in all_records(repository, matter.matter_id, attempt_id):
            if row["kind"] == "rejected":
                assert row["ordinal"] == rejected + 2
                assert row["line"] == 3 + 2 * rejected
                assert row["cells"] == [f"BAD{rejected}", "Generated first\r\nGenerated second", "extra"]
                assert row["diagnostics"]
                rejected += 1
            else:
                assert (row["ordinal"], row["line"], row["cells"][0]) == (
                    (1, 2, "FIRST") if accepted == 0 else (2503, 5005, "LAST"))
                accepted += 1
        assert (accepted, rejected) == (2, 2501)
        assert repository.attempt(matter.matter_id, attempt_id)["counts"] == before["counts"]
        assert repository.records(matter.matter_id, attempt_id, category="document", after=2502, limit=1)[0]["cells"][0] == "LAST"
    finally:
        reopened.close()


def assert_batch_contract(batches, *, max_records=ROW_BOUND, max_bytes=BYTE_BOUND):
    assert batches
    for index, (rows, byte_count, first_bytes, ordinals) in enumerate(batches):
        assert 0 < rows <= max_records
        assert len(ordinals) == rows
        assert byte_count <= max_bytes or rows == 1
        if byte_count > max_bytes:
            assert byte_count == first_bytes
        if index + 1 < len(batches) and rows < max_records and byte_count < max_bytes:
            # A non-full committed batch is justified only by the next event's
            # decoded bytes crossing the bound. EOF occurs only in the last batch.
            assert byte_count + batches[index + 1][2] > max_bytes


def test_batch_limits_include_wide_records(tmp_path):
    with storage_case(tmp_path / "wide", count=80, width=256 * 1024) as case:
        attempt = finish_import(case)
        assert_batch_contract(case.repository.batches)
        assert max(batch[0] for batch in case.repository.batches) < ROW_BOUND
        assert len(case.repository.batches) >= 6
        assert attempt["counts"] == dict(accepted=80, rejected=0, matched=80, unmatched=0, conflicted=0)
        last = case.repository.record(case.matter.matter_id, attempt["attempt_id"], 80, category="document")
        assert last["cells"][-1] == generated_text(80, 256 * 1024)
        assert "界" in last["cells"][-1]
    with storage_case(tmp_path / "singleton", count=3, width=256 * 1024) as case:
        attempt = finish_import(case, max_bytes=128 * 1024)
        assert_batch_contract(case.repository.batches, max_bytes=128 * 1024)
        oversized = [batch for batch in case.repository.batches if batch[1] > 128 * 1024]
        assert len(oversized) == 3
        assert all(batch[0] == 1 for batch in oversized)
        assert [batch[3] for batch in oversized] == [(1,), (2,), (3,)]
        assert attempt["counts"]["accepted"] == 3


def test_restart_version_change_and_failed_attempt_visibility(tmp_path):
    with storage_case(tmp_path / "restart", count=7) as case:
        document = case.store.documents["00000000000000000000000000000001"]
        source = source_identity(document, case.repository, case.matter)
        path = case.store.source_path(document.document_id, verify_digest=True)
        target = "generated-target:" + case.matter.matter_id
        first = case.service.import_source(case.matter.matter_id, source, path, case.definition,
                                          target=target, max_records=2)
        assert first["phase"] == "parse" and first["counts"]["accepted"] == 1
        cursor = first["cursor"]
        assert cursor and cursor["parser"]
        replay = RecordEvent("accepted", "document", "dat", 1, 2, HEADERS,
                             generated_values("clean", 1, 7, 512))
        assert case.repository.append_events(case.matter.matter_id, first["attempt_id"], [replay]) == 0
        with pytest.raises(ProductionConflict):
            case.repository.append_events(case.matter.matter_id, first["attempt_id"],
                                          [replace(replay, cells=(*replay.cells[:-1], "Changed generated text"))])
        assert case.repository.attempt(case.matter.matter_id, first["attempt_id"])["counts"]["accepted"] == 1
        # Close both owners, then rebuild capabilities around the reopened
        # workspace and real source registry. The persisted seek cookie must
        # survive the source-store epoch change without replaying raw records.
        reopen_storage_case(case, tmp_path / "restart")
        assert case.repository.attempt(case.matter.matter_id, first["attempt_id"])["cursor"] == cursor
        complete = finish_import(case, max_records=2)
        assert complete["attempt_id"] == first["attempt_id"]
        assert complete["counts"] == dict(accepted=7, rejected=0, matched=7, unmatched=0, conflicted=0)
        assert [row["ordinal"] for row in all_records(case.repository, case.matter.matter_id, first["attempt_id"])] == list(range(1, 8))
        same = case.repository.begin_attempt(case.matter.matter_id, source,
            parser_revision=PARSER_REVISION, definition_digest=definition_digest(case.definition), target=target)
        assert same == complete["attempt_id"]
        prior = same
        for new_source, revision, definition, new_target in (
            (source, PARSER_REVISION + "-new", definition_digest(case.definition), target),
            (source, PARSER_REVISION + "-new", "changed-field-definition", target),
            (replace(source, version_id="generated-new-version"), PARSER_REVISION + "-new", "changed-field-definition", target),
            (replace(source, version_id="generated-new-version"), PARSER_REVISION + "-new", "changed-field-definition", target + "-new"),
        ):
            current = case.repository.begin_attempt(case.matter.matter_id, new_source,
                parser_revision=revision, definition_digest=definition, target=new_target)
            assert current != prior
            assert case.repository.attempt(case.matter.matter_id, prior)["status"] == "stale"
            with pytest.raises(ProductionStale):
                case.repository.update_attempt(case.matter.matter_id, prior, status="complete", phase="complete")
            assert len(case.repository.attempts(case.matter.matter_id)) == 1
            prior = current
        case.repository.update_attempt(case.matter.matter_id, prior, status="failed", phase="error",
                                       failure="Generated incomplete source read")
        failed = case.repository.current_attempt(case.matter.matter_id, source.document_id)
        assert failed["status"] == "failed" and failed["file_failure"] == "Generated incomplete source read"
        with pytest.raises(ProductionConflict):
            case.repository.update_attempt(case.matter.matter_id, prior, status="complete", phase="complete")
        projection = case.service.document_projection(case.matter.matter_id, document.document_id, document.version_id)
        # This attempt deliberately names a different source version and parser;
        # it cannot certify either failure or evidence for the current original.
        assert projection["state"] == "pending"
        assert not projection.get("values")
    unreadable = b"\xff\xfe\x00\x00" + "Control\r\nGenerated\r\n".encode("utf-32-le")
    with storage_case(tmp_path / "failed", count=1, extra_files=(("unreadable.dat", unreadable),)) as case:
        document = next(document for document in case.store.documents.values()
                        if document.display_name == "unreadable.dat")
        source = source_identity(document, case.repository, case.matter)
        failed = case.service.import_source(case.matter.matter_id, source,
            case.store.source_path(document.document_id, verify_digest=True), case.definition,
            target="generated-failed-source-target")
        assert failed["status"] == "failed"
        assert "unsupported_encoding" in failed["file_failure"]
        assert failed["counts"]["accepted"] == 0
        projection = case.service.document_projection(case.matter.matter_id, document.document_id, document.version_id)
        assert projection["state"] == "error" and not projection["values"]
        assert projection["references"][0]["failure"] == failed["file_failure"]


def test_production_schema_mirror_and_restore(tmp_path):
    root = Path(__file__).resolve().parents[1]
    migration = "migrations/sqlite/0050_discovery_dat_opt.sql"
    assert (root / migration).read_bytes() == (root / "src/case_intelligence" / migration).read_bytes()
    with storage_case(tmp_path / "original", count=3,
                      extra_files=(("images.opt", b"GEN00000001,VOL,Images/a.tif,Y,unused-5,unused-6,1\r\n"),)) as case:
        attempts = []
        for document in case.store.documents.values():
            if Path(document.display_name).suffix.lower() not in {".dat", ".opt"}:
                continue
            source = source_identity(document, case.repository, case.matter)
            attempts.append(parse_into(case.repository, case.matter.matter_id, source,
                                       case.store.source_path(document.document_id, verify_digest=True)))
        validate_schema(case.workspace.connection)
        for table in PRODUCTION_TABLES:
            assert table in SCHEMA and table not in EXCLUDED_TABLES
            assert {"matter_id", "attempt_id"} <= set(SCHEMA[table])
        for table in PRODUCTION_TABLES[1:]:
            assert {"ordinal", "line", "headers_json", "cells_json", "diagnostics_json", "optional_values_json"} <= set(exported_columns(table))
        snapshots = [(case.repository.attempt(case.matter.matter_id, ident),
                      tuple(all_records(case.repository, case.matter.matter_id, ident, category="document")),
                      tuple(all_records(case.repository, case.matter.matter_id, ident, category="page"))) for ident in attempts]
        backup_path = tmp_path / "restored.sqlite"
        backup = sqlite3.connect(backup_path)
        try:
            with case.workspace._lock:
                case.workspace.connection.backup(backup)
        finally:
            backup.close()
        restored = WorkspaceStore(backup_path)
        try:
            validate_schema(restored.connection)
            repository = ProductionRepository(ModuleDatabase(restored.connection, restored._lock))
            for attempt, documents, pages in snapshots:
                assert repository.attempt(case.matter.matter_id, attempt["attempt_id"]) == attempt
                assert tuple(all_records(repository, case.matter.matter_id, attempt["attempt_id"])) == documents
                assert tuple(all_records(repository, case.matter.matter_id, attempt["attempt_id"], category="page")) == pages
                assert repository.attempt("generated-other-matter", attempt["attempt_id"]) is None
                assert repository.records("generated-other-matter", attempt["attempt_id"], category="document") == ()
        finally:
            restored.close()


@pytest.mark.parametrize("phase", ["parse", "match", "family_qc", "complete"])
def test_archive_preserves_production_checkpoints_and_raw_records(tmp_path, phase):
    from case_intelligence.matter_archive import build_matter_archive, cleanup_archive
    from case_intelligence.matter_archive_verifier import verify_matter_archive
    from tests.case_lifecycle_helpers import lifecycle_session
    from tests.test_matter_archive import records

    if phase == "parse":
        headers = ("BEGDOC", "path", "headers", "Generated field®")
        raw = dat_line(headers) + b"".join(dat_line((f"GEN{ordinal:08d}", native_path(ordinal),
            "headers is supplied evidence", "Generated retained text®")) for ordinal in range(1, 4))
    elif phase == "family_qc":
        raw = dat_line(HEADERS)
        for ordinal in range(1, 4):
            values = generated_values("family", ordinal, 3, 512)
            # The supplied terminal has no member: a real orphan diagnostic
            # must be carried by the persisted family-check continuation.
            raw += dat_line((*values[:4], "GEN00000999", values[5]))
    else:
        raw = None
    with storage_case(tmp_path, count=3, primary_bytes=raw) as case:
        document = case.store.documents["00000000000000000000000000000001"]
        source = source_identity(document, case.repository, case.matter)
        path = case.store.source_path(document.document_id, verify_digest=True)
        original = path.read_bytes()
        if phase == "complete":
            attempt = finish_import(case)
            assert attempt["cursor"] == {"eof": True}
        else:
            attempt = case.service.import_source(case.matter.matter_id, source, path, case.definition,
                target="generated-archive-checkpoint", max_records=2 if phase == "parse" else ROW_BOUND)
            if phase == "parse":
                assert attempt["phase"] == "parse" and attempt["status"] == "building"
                assert attempt["cursor"]["parser"]["headers"] == list(headers)
                assert attempt["cursor"]["parser"]["cookie"] > 0
                assert attempt["cursor"]["parser"]["line"] == 3
            else:
                for unused in range(200):
                    del unused
                    result = case.service.advance(case.matter.matter_id, max_steps=1)
                    assert result["state"] != "error"
                    attempt = case.repository.attempt(case.matter.matter_id, attempt["attempt_id"])
                    active = attempt["cursor"].get("active", {})
                    if ((phase == "match" and attempt["phase"] == "match" and active)
                            or (phase == "family_qc" and attempt["phase"] == "families" and active.get("diagnostics"))):
                        break
                else:
                    raise AssertionError("The three-record fixture did not reach its saved matching/QC continuation")
                assert attempt["status"] == "building"
                if phase == "family_qc":
                    assert "orphan_attachment" in attempt["cursor"]["active"]["diagnostics"]
                else:
                    assert attempt["cursor"]["active"]["after"] == native_id(1)
        attempt_id = attempt["attempt_id"]
        expected_cursor = attempt["cursor"]
        expected_records = tuple(all_records(case.repository, case.matter.matter_id, attempt_id))
        reopen_storage_case(case, tmp_path)
        reopened = case.repository.attempt(case.matter.matter_id, attempt_id)
        assert reopened["cursor"] == expected_cursor
        assert reopened["status"] == attempt["status"]
        assert tuple(all_records(case.repository, case.matter.matter_id, attempt_id)) == expected_records
        bench = SimpleNamespace(workspace=case.workspace,
            storage=SimpleNamespace(root=tmp_path, ensure_capacity=case.context.ensure_capacity),
            storage_policy=StoragePolicy(reserve_bytes=0), source_registry=None,
            archive_source_store=lambda unused: case.store,
            _storage_reservation_lock=threading.RLock(), _preservation_reservations={})
        bench._playback_reserved_bytes = lambda matter_id=None: sum(bench._preservation_reservations.values()) if matter_id is None else 0
        session = lifecycle_session(case.workspace, ACTOR)
        case.workspace.case_lifecycle.begin_exit(case.matter.slug, ACTOR, session_id=session.session_id)
        artifact = build_matter_archive(bench, case.matter, ACTOR, source_store=case.store)
        try:
            verify_matter_archive(artifact.path, expected_sha256=artifact.sha256)
            archived = records(artifact, "workbench_discovery_production")
            assert len(archived) == 1 and archived[0]["attempt_id"] == attempt_id
            payload = archived[0]["cursor_json"]
            assert isinstance(payload, list)

            def containers_are_arrays(value):
                assert not isinstance(value, dict)
                if isinstance(value, list):
                    for child in value:
                        containers_are_arrays(child)

            containers_are_arrays(payload)
            assert decode_cursor(json.dumps(payload, ensure_ascii=False)) == expected_cursor
            preserved_rows = records(artifact, "workbench_discovery_dat_record")
            assert len(preserved_rows) == len(expected_records)
            for preserved, expected in zip(preserved_rows, expected_records, strict=True):
                assert (preserved["ordinal"], preserved["line"]) == (expected["ordinal"], expected["line"])
                for key in ("headers", "cells", "diagnostics", "optional_values"):
                    assert preserved[key + "_json"] == expected[key]
                assert preserved["raw_fingerprint"] == expected["raw_fingerprint"]
            with zipfile.ZipFile(artifact.path) as archive:
                assert archive.read("originals/" + document.document_id) == original
            assert case.store.source_path(document.document_id, verify_digest=True).read_bytes() == original
            assert hashlib.sha256(original).hexdigest() == document.digest
        finally:
            cleanup_archive(artifact)


def test_purge_and_frozen_writers_cover_production_records(tmp_path):
    from tests.case_lifecycle_helpers import lifecycle_session
    from tests.case_lifecycle_web_helpers import claim_bench_exit

    with app_case(tmp_path, count=3, extra_files=(("images.opt", b"GEN00000001,VOL,Images/a.tif,Y,,,1\r\n"),)) as case:
        attempt_ids = []
        for matter in (case.matter, case.neighbor):
            for document in case.stores[matter.matter_id].documents.values():
                if Path(document.display_name).suffix.lower() not in {".dat", ".opt"}:
                    continue
                source = source_identity(document, case.repository, matter)
                attempt_ids.append((matter.matter_id, parse_into(case.repository, matter.matter_id,
                    source, case.stores[matter.matter_id].source_path(document.document_id, verify_digest=True))))
        owned = next(ident for owner, ident in attempt_ids if owner == case.matter.matter_id)
        neighbor = next(ident for owner, ident in attempt_ids if owner == case.neighbor.matter_id)
        before = case.repository.attempt(case.neighbor.matter_id, neighbor)
        late_event = RecordEvent("accepted", "document", "dat", 4, 5, HEADERS,
                                generated_values("clean", 4, 4, 512))
        session = lifecycle_session(case.workspace, ACTOR)
        case.workspace.case_lifecycle.begin_exit(case.matter.slug, ACTOR, session_id=session.session_id)
        with pytest.raises(ProductionUnavailable):
            case.repository.append_events(case.matter.matter_id, owned, [late_event])
        with pytest.raises(ProductionUnavailable):
            case.repository.set_association(case.matter.matter_id, owned, 1, category="document",
                                            document_id=native_id(1), status="matched")
        release, finished = threading.Event(), threading.Event()
        failures = []

        def late_writer():
            release.wait()
            try:
                case.repository.append_events(case.matter.matter_id, owned, [late_event])
            except ProductionUnavailable as error:
                failures.append(type(error).__name__)
            finally:
                finished.set()

        thread = threading.Thread(target=late_writer)
        thread.start()
        module = registry.lookup("discovery_review")
        assert module is not None and module.purge_hook is not None
        calls = []

        def interrupted_cleanup(matter_root, matter_id):
            calls.append(matter_id)
            if len(calls) == 1:
                raise OSError("Generated interrupted cleanup before the production deletion")
            module.purge_hook(matter_root, matter_id)

        try:
            prepared, lifecycle = claim_bench_exit(case.bench, case.matter)
            modules = tuple(replace(item, purge_hook=interrupted_cleanup)
                            if item.name == module.name else item for item in registry.REGISTERED)
            with patch.object(registry, "REGISTERED", modules):
                with pytest.raises(WorkspaceProblem, match="Matter deletion stopped"):
                    case.bench.execute_matter_purge(prepared, lifecycle)
                assert case.workspace.matter_lifecycle(case.matter.matter_id).state == "purge_failed"
                assert case.repository.record(case.matter.matter_id, owned, 1, category="document") is not None
                with pytest.raises(ProductionUnavailable):
                    case.repository.append_events(case.matter.matter_id, owned, [late_event])
                # Retry obtains new current confirmation through the actual
                # lifecycle; a consumed deletion authority is never replayed.
                retry, retry_lifecycle = claim_bench_exit(case.bench, case.matter)
                assert case.bench.execute_matter_purge(retry, retry_lifecycle).state == "deleted"
            assert calls == [case.matter.matter_id, case.matter.matter_id]
        finally:
            release.set()
            assert finished.wait(10)
            thread.join(10)
        assert len(failures) == 1
        assert case.repository.attempts(case.matter.matter_id, include_stale=True) == ()
        for owner, ident in attempt_ids:
            if owner == case.matter.matter_id:
                assert case.repository.records(owner, ident, category="document") == ()
                assert case.repository.records(owner, ident, category="page") == ()
        assert case.repository.attempt(case.neighbor.matter_id, neighbor) == before
        assert case.repository.record(case.neighbor.matter_id, neighbor, 1, category="document") is not None


def rss_bytes():
    # Current RSS rather than the process lifetime high-water mark keeps fixture
    # generation outside the measured interval. Missing /proc is a test failure.
    return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")


def index_request_budget(case, matter, load_records):
    """Conservative D11b setup bound for this generated, initially empty index.

    R includes the load-file source. There are no prior derived rows, Coding
    entries or Team IDs. A charges two complete dependency/winner passes over
    every possible pair rather than assuming unique paths eliminate the work.
    U/Q additionally allow preparation and revalidation of every desired value
    row; the wide-record fixture receives the same allowance, not a timing cap.
    """
    registry_rows = case.stores[matter.matter_id].document_count()
    headers = len(HEADERS)
    values = 2 * registry_rows * (1 + len(case.definition.fields) + headers)
    candidates = 2 * registry_rows * load_records
    preparation_work = 4 * values + 8 * registry_rows
    preparation_yields = 2 * registry_rows
    work = (64 + 64 * registry_rows + 32 + 16 * load_records + 8 * candidates
            + 4 * values + 8 * headers + preparation_work)
    early_yields = 16 + 8 * registry_rows + 8 + 2 * (registry_rows + 1) + preparation_yields
    steps = math.ceil(work / 192) + early_yields + 2
    return math.ceil(steps / 128), registry_rows


def complete_http_page(case, matter, *, load_records, budget=None):
    max_requests, source_count = budget or index_request_budget(case, matter, load_records)
    for number in range(1, max_requests + 1):
        response = case.client.get(f"/matters/{matter.slug}/production?page_size=100")
        assert response.status_code == 200, response.text[:1000]
        visible = html.unescape(re.sub(r"<[^>]+>", " ", response.text))
        coverage = re.search(r"Indexed\s+([\d,]+)\s+of\s+([\d,]+)", visible)
        assert coverage is not None, "Production must expose its actual index coverage"
        indexed, total = (int(value.replace(",", "")) for value in coverage.groups())
        assert 0 <= indexed <= total == source_count
        if indexed == total and "Showing all items in source order while indexing" not in visible:
            assert "Metadata pending" not in visible
            assert "Metadata unavailable: indexing error" not in visible
            return response, number
        assert "Showing all items in source order while indexing" in visible
        del response, visible
    raise AssertionError("The stable generated index exhausted its D11b request-work allowance")


def measured_pipeline(case, profile, count, width, control):
    expected_tail = list(generated_values(profile, count, count, width))
    expected_tail_digest = hashlib.sha256(expected_tail[-1].encode()).hexdigest()
    http_budget = index_request_budget(case, case.matter, count + 1)
    finish_import(case, matter=case.warm)
    warm_page, warm_requests = complete_http_page(case, case.warm, load_records=3)
    assert warm_requests > 0
    del warm_page
    case.repository.reset_observations(retain_batches=False)
    case.repository.control = control
    assert case.repository.current_attempt(case.matter.matter_id, "00000000000000000000000000000001") is None
    gc.collect()
    baseline_rss = rss_bytes()
    peak_rss = baseline_rss
    stop = threading.Event()

    def sample():
        nonlocal peak_rss
        while not stop.wait(0.002):
            peak_rss = max(peak_rss, rss_bytes())

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    tracemalloc.start()
    baseline_python = tracemalloc.get_traced_memory()[0]
    try:
        attempt = finish_import(case)
        # The actual first HTTP list read is part of the interval. It must use
        # the real s2 publication hook; no alternate reader or fake index exists.
        response, publication_requests = complete_http_page(case, case.matter, load_records=count + 1,
                                                           budget=http_budget)
        assert "Generated 00000001" in response.text or "GEN00000001" in response.text
        del response
        accepted = rejected = matched = unmatched = conflicted = family_rows = 0
        last = None
        for row in all_records(case.repository, case.matter.matter_id, attempt["attempt_id"], limit=8):
            last = row
            accepted += row["kind"] == "accepted"
            rejected += row["kind"] == "rejected"
            matched += row["association_status"] == "matched"
            unmatched += row["kind"] == "accepted" and row["association_status"] == "unmatched"
            conflicted += row["association_status"] == "conflicted"
            if profile == "family":
                assert row["family"]["root"] == native_id(1)
                assert row["family"]["parent"] == ("" if row["document_id"] == native_id(1) else native_id(1))
                assert row["family"]["order"]
                family_rows += 1
        assert last is not None and last["ordinal"] == count
        assert last["cells"] == expected_tail
        assert attempt["counts"] == dict(accepted=accepted, rejected=rejected,
                                        matched=matched, unmatched=unmatched, conflicted=conflicted)
        counts = dict(attempt["counts"])
        del last, row, attempt
        gc.collect()
        retained, peak = tracemalloc.get_traced_memory()
        peak_rss = max(peak_rss, rss_bytes())
        report = dict(profile=profile, count=count, width=width, control=control,
            python_peak=peak - baseline_python, retained=retained - baseline_python,
            rss_increment=max(0, peak_rss - baseline_rss), counts=counts,
            max_batch_rows=case.repository.max_batch_rows,
            max_batch_bytes=max(case.repository.max_batch_bytes, case.repository.control_queue_bytes),
            queue_max_rows=case.repository.queue_max_rows,
            queue_max_bytes=case.repository.queue_max_bytes, read_calls=case.repository.read_calls,
            publication_requests=publication_requests,
            tail_ordinal=count, tail_digest=expected_tail_digest, family_rows=family_rows,
            control_retained=len(case.repository.retained))
        assert case.repository.batch_count > 0
        return report
    finally:
        tracemalloc.stop()
        stop.set()
        sampler.join(10)
        assert not sampler.is_alive()


def profile_sample(tmp_path, profile, count, width, *, control="none"):
    destination = tmp_path / f"{profile}-{count}-{width}-{control}"
    destination.mkdir(parents=True)
    report_path = destination / "report.json"
    process = subprocess.run(
        [sys.executable, "-m", "tests.test_discovery_review_dat_opt_storage", "--memory-profile", profile,
         "--count", str(count), "--width", str(width), "--control", control,
         "--runtime", str(destination), "--report", str(report_path)],
        cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=2400)
    assert process.returncode == 0, process.stdout[-4000:] + process.stderr[-8000:]
    return json.loads(report_path.read_text())


def assert_memory_contract(smaller, larger):
    for sample in (smaller, larger):
        assert 0 < sample["python_peak"] <= 64 * MIB, sample
        assert sample["rss_increment"] <= 128 * MIB, sample
        assert sample["retained"] <= 4 * MIB, sample
        assert sample["max_batch_rows"] <= ROW_BOUND, sample
        assert sample["max_batch_bytes"] <= BYTE_BOUND, sample
        assert sample["queue_max_rows"] <= ROW_BOUND, sample
        assert sample["queue_max_bytes"] <= BYTE_BOUND, sample
        assert sample["read_calls"] > 0
    assert larger["python_peak"] <= smaller["python_peak"] * 1.5, (smaller, larger)


@pytest.mark.parametrize("profile,small,large,width", [
    ("wide", 20, 80, 256 * 1024),
])
def test_streamed_import_memory_and_complete_counts(tmp_path, profile, small, large, width):
    samples = [profile_sample(tmp_path, profile, count, width) for count in (small, large)]
    assert_memory_contract(*samples)
    for sample in samples:
        count = sample["count"]
        rejected = sum(malformed_at(profile, ordinal, count) for ordinal in range(1, count + 1))
        matched = count if profile == "family" else sum(
            not malformed_at(profile, ordinal, count) for ordinal in range(1, min(count, 9999) + 1))
        assert sample["counts"] == dict(accepted=count - rejected, rejected=rejected,
            matched=matched, unmatched=count - rejected - matched, conflicted=0)
        assert sample["tail_ordinal"] == count
        assert sample["tail_digest"] == hashlib.sha256(generated_values(profile, count, count, width)[-1].encode()).hexdigest()
        assert sample["family_rows"] == (count if profile == "family" else 0)


@pytest.mark.parametrize("profile,small,large,width,control", [
    ("wide", 20, 80, 256 * 1024, "count_only"),
])
def test_memory_contract_detects_materializing_controls(tmp_path, profile, small, large, width, control):
    # Fixed controls retain actual pipeline objects. Both controls and the
    # streaming implementation use precisely the same numeric detector.
    streaming = [profile_sample(tmp_path / "streaming", profile, count, width) for count in (small, large)]
    assert_memory_contract(*streaming)
    broken = [profile_sample(tmp_path / "materialized", profile, count, width, control=control)
              for count in (small, large)]
    assert all(sample["control_retained"] > 0 for sample in broken)
    with pytest.raises(AssertionError):
        assert_memory_contract(*broken)


def _main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--memory-profile", required=True, choices=("clean", "malformed", "family", "wide"))
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--control", choices=("none", "rows", "diagnostics", "families", "count_only"), default="none")
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    with app_case(args.runtime, profile=args.memory_profile, count=args.count, width=args.width) as case:
        report = measured_pipeline(case, args.memory_profile, args.count, args.width, args.control)
    args.report.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    _main()


def test_family_validation_finishes_the_active_pass(tmp_path, monkeypatch):
    count = 12
    with storage_case(tmp_path, profile='family', count=count) as case:
        calls = []
        original = case.repository.family_members

        def observed(*args, **kwargs):
            calls.append(kwargs.get('after'))
            return original(*args, **kwargs)

        monkeypatch.setattr(case.repository, 'family_members', observed)
        attempt = finish_import(case)
        records = tuple(all_records(case.repository, case.matter.matter_id, attempt['attempt_id']))
        assert len(records) == count
        assert all(row['family']['root'] == native_id(1) for row in records)
        assert len(calls) <= 2 * count + 4
