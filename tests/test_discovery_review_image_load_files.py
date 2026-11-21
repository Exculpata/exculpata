"""Generated image-index integration through real intake, persistence and HTTP."""
from __future__ import annotations

import hashlib
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import time
import uuid
import zipfile

import pytest

from case_intelligence.matter_archive import ArchiveProblem, build_matter_archive, cleanup_archive
from case_intelligence.matter_archive_verifier import verify_matter_archive
from case_intelligence.modules.discovery_review import load_files, reader
from case_intelligence.modules.discovery_review.production import ProductionService, ProductionStale, definition_digest
from case_intelligence.pilot_uploads import UploadProblem
from tests.case_lifecycle_helpers import lifecycle_session
from tests.case_lifecycle_web_helpers import claim_bench_exit
from tests.fixtures.load_files.generate_image_indexes import dii_bytes, lfp_bytes
from tests.test_discovery_review_dat_opt import (
    ACTOR, all_records, dat_bytes, import_paths, opened_review, record_article,
    record_findings, record_request, review_for, row_tokens,
)
from tests.test_intake_receipt_http import put, selection, upload
from tests.test_load_file_admission import forbid_path_io


def image_bytes(kind, color):
    return subprocess.run(
        ["convert", "-size", "7x5", f"xc:{color}", "-strip", f"{kind}:-"],
        check=True, capture_output=True, timeout=15,
    ).stdout


def upload_image_production(review, files):
    """One real sealed receipt supplies root provenance for every actual original."""
    media = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
    names = list(files)
    descriptors = [{"name": Path(name).name, "relative_path": name, "size": len(files[name]),
                    "media_type": media.get(Path(name).suffix.lower(), "text/plain")} for name in names]
    ordinals = list(range(len(names)))
    receipt = selection(review.client, review.matter.slug, descriptors, ordinals, key=uuid.uuid4().hex)
    session = upload(review.client, review.matter.slug, receipt, descriptors, ordinals)
    for name, item in zip(names, session["items"]):
        body = files[name]
        for offset in range(0, len(body), 1024 * 1024):
            put(review.client, item, body[offset:offset + 1024 * 1024], offset=offset)
        response = review.client.post(item["finalize_url"])
        assert response.status_code == 200, response.text
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        items = [review.bench.workspace.upload_item(review.matter.matter_id, ACTOR,
            session["upload_session_id"], item["upload_item_id"]) for item in session["items"]]
        jobs = [review.bench.workspace.ingest_job(review.matter.matter_id, item.document_id) for item in items]
        if all(job is not None and job.state in {"succeeded", "failed"} for job in jobs):
            assert all(job.state == "succeeded" for job in jobs), jobs
            break
        time.sleep(0.01)
    else:
        pytest.fail("Actual generated image production did not finish ingestion")
    for name, body in files.items():
        document = review.document(name)
        assert review.store.source_path(document.document_id, verify_digest=True).read_bytes() == body
        assert document.digest == hashlib.sha256(body).hexdigest()
    return receipt


def production_bytes(*, cross_suffix=False, parent_count="0002", parent_token=True):
    lfp_name = "Generated/pages.dii" if cross_suffix else "Generated/pages.lfp"
    dii_name = "Generated/metadata.lfp" if cross_suffix else "Generated/metadata.dii"
    page_rows = (
        {"key": "CASE0007", "boundary": "D", "volume": "VOL01", "directory": "Generated",
         "filename": "z-parent.png", "image_type": "4"},
        {"key": "TAIL0099", "boundary": " ", "volume": "VOL01", "directory": "Generated",
         "filename": "second.png", "image_type": "4", "rotation": "3"},
        {"key": "CASE0042", "boundary": "D", "volume": "VOL01", "directory": "Generated",
         "filename": "child.jpg", "image_type": "4", "rotation": "0"},
    )
    documents = [
        {"record_id": "document-alpha", "begin_bates": "CASE0007", "page_count": parent_count,
         "docid": "RAW-DOC-ALPHA", "directory": "Generated", "images": ("z-parent.png", "second.png")},
        {"record_id": "document-beta", "begin_bates": "CASE0042", "page_count": "1",
         "directory": "Generated", "images": ("child.jpg",)},
        {"record_id": "document-independent", "begin_bates": "UNRELATED1", "page_count": "1",
         "directory": "Generated", "images": ("UNRELATED1.png",)},
        {"record_id": "document-orphan", "begin_bates": "MISSING077",
         "directory": "Generated", "images": ("not-produced.png",)},
    ]
    if parent_token:
        documents[2]["parent_id"] = "CASE0007"
    files = {
        "Generated/z-parent.png": image_bytes("PNG", "red"),
        "Generated/second.png": image_bytes("PNG", "blue"),
        "Generated/child.jpg": image_bytes("JPEG", "green"),
        "Generated/UNRELATED1.png": image_bytes("PNG", "yellow"),
        "Generated/families.dat": dat_bytes(
            ("BEGBATES", "ENDBATES", "NATIVEPATH", "PAGECOUNT", "BEGATTACH", "ENDATTACH", "DAT witness"),
            [("CASE0007", "CASE0007", "Generated/z-parent.png", "0002", "CASE0007", "CASE0042", "parent supplied"),
             ("CASE0042", "CASE0042", "Generated/child.jpg", "1", "CASE0007", "CASE0042", "child supplied")]),
        lfp_name: lfp_bytes(page_rows), dii_name: dii_bytes(documents),
    }
    return files, lfp_name, dii_name


def assertions_for_provenance(review, attempt_id, category, count):
    attempt = review.repository.attempt(review.matter.matter_id, attempt_id)
    records = tuple(all_records(review, attempt_id, category=category))
    assert len(records) == count
    for ordinal, record in enumerate(records, 1):
        assert record["source_document_id"] == attempt["document_id"]
        assert record["source_version_id"] == attempt["version_id"]
        assert record["source_format"] == attempt["source_format"]
        assert record["ordinal"] == ordinal and record["line"] >= ordinal
    return records


@pytest.mark.parametrize("cross_suffix", [False, True], ids=["ordinary-names", "cross-suffix"])
@pytest.mark.parametrize("first", ["dat", "dii"])
def test_uploaded_formats_share_review_behavior(tmp_path, cross_suffix, first):
    with opened_review(tmp_path / "runtime") as review:
        files, lfp_name, dii_name = production_bytes(cross_suffix=cross_suffix)
        upload_image_production(review, files)
        order = ["Generated/families.dat", dii_name] if first == "dat" else [dii_name, "Generated/families.dat"]
        attempts = import_paths(review, [*order, lfp_name])
        lfp = review.repository.attempt(review.matter.matter_id, attempts[lfp_name])
        dii = review.repository.attempt(review.matter.matter_id, attempts[dii_name])
        assert lfp["source_format"] == "lfp" and dii["source_format"] == "dii"
        assert load_files.classify_source(review.store.source_path(lfp["document_id"]), name=lfp_name) == "lfp"
        assert load_files.classify_source(review.store.source_path(dii["document_id"]), name=dii_name) == "dii"
        pages = assertions_for_provenance(review, attempts[lfp_name], "page", 3)
        documents = assertions_for_provenance(review, attempts[dii_name], "document", 4)
        assert tuple(all_records(review, attempts[dii_name], category="page")) == ()
        assert tuple(all_records(review, attempts[lfp_name], category="document")) == ()
        assert [load_files.record_semantics(page)["page_label"] for page in pages] == ["CASE0007", "TAIL0099", "CASE0042"]
        assert [load_files.record_semantics(page)["document_break"] for page in pages] == [True, False, True]
        assert [load_files.record_semantics(page)["path"] for page in pages] == [
            "Generated/z-parent.png", "Generated/second.png", "Generated/child.jpg"]
        assert pages[0]["cells"] == ["IM", "CASE0007", "D", "0", "@VOL01;Generated;z-parent.png;4"]
        assert pages[1]["cells"] == ["IM", "TAIL0099", " ", "0", "@VOL01;Generated;second.png;4", "3"]
        assert lfp["counts"] == {"accepted": 3, "rejected": 0, "matched": 3, "unmatched": 0, "conflicted": 0}
        assert dii["counts"]["accepted"] == 4 and dii["counts"]["rejected"] == 0
        assert documents[-1]["association_status"] == "unmatched"
        assert not documents[-1]["document_id"]
        parent = review.document("Generated/z-parent.png")
        child = review.document("Generated/child.jpg")
        for name in ("Generated/z-parent.png", "Generated/child.jpg"):
            projected = review.projection(name)
            assert projected["state"] == "complete" and projected["family"]["root"] == parent.document_id
            assert ("DAT witness", "parent supplied" if name.endswith("png") else "child supplied") in projected["raw_fields"]
        unrelated = review.projection("Generated/UNRELATED1.png")
        assert unrelated["values"]["begin_bates"] == "UNRELATED1" and unrelated["family"] == {}
        assert any("parent" in code or "relationship" in code for code in record_findings(documents[2]))
        assert review.projection("Generated/z-parent.png")["values"]["load_file_pages"] == "0002"
        for endpoint in ("full", "fragment"):
            for ordinal, selected, image_name in ((1, "Generated/z-parent.png", "Generated/z-parent.png"),
                    (2, "Generated/z-parent.png", "Generated/second.png"),
                    (3, "Generated/child.jpg", "Generated/child.jpg")):
                response = record_request(review, selected, attempts[lfp_name], ordinal, endpoint=endpoint)
                shown = record_article(response, ordinal)
                assert load_files.record_semantics(pages[ordinal - 1])["page_label"] in shown
                assert lfp["version_id"] in shown and lfp["name"] in shown
                window = review.service.record_window(review.matter.matter_id, attempts[lfp_name],
                    category="page", ordinal=ordinal, document_id=review.document(selected).document_id)
                expected_image = review.document(image_name)
                assert window["current"]["image"] == {"document_id": expected_image.document_id,
                    "version_id": expected_image.version_id, "media_type": expected_image.media_type}
                selected_document = review.document(selected)
                with ExitStack() as guards:
                    for document in review.store.documents.values():
                        guards.enter_context(forbid_path_io(review.store.files / document.stored_name, metadata=True))
                    binding = reader.produced_image_binding(review.repository, matter_id=review.matter.matter_id,
                        selected_document_id=selected_document.document_id,
                        selected_version_id=selected_document.version_id, window=window)
                assert binding["image"] == window["current"]["image"] and binding["explicit_frame"] is None
                assert binding["source_id"] == lfp["document_id"] and binding["source_version_id"] == lfp["version_id"]
                assert binding["source_digest"] == lfp["digest"] and binding["root_key"] == lfp["root_key"]
                assert binding["generation"] == window["attempt"]["generation"]
                assert binding["raw_fingerprint"] == window["current"]["raw_fingerprint"]
                assert binding["ordinal"] == ordinal and binding["association_status"] == "matched"
                portable = {**binding, "image": dict(binding["image"])}
                assert json.loads(json.dumps(portable)) == portable and "path" not in binding
                with pytest.raises(TypeError):
                    binding["ordinal"] = ordinal + 1
                with pytest.raises(TypeError):
                    binding["image"]["document_id"] = "0" * 32
            second = record_request(review, "Generated/z-parent.png", attempts[lfp_name], 1,
                direction="next", endpoint=endpoint)
            assert "TAIL0099" in record_article(second, 2)
            assert 'data-record-direction="next"' not in second.text
            first_page = record_request(review, "Generated/z-parent.png", attempts[lfp_name], 2,
                direction="previous", endpoint=endpoint)
            assert "CASE0007" in record_article(first_page, 1)
            for ordinal in range(1, 5):
                response = record_request(review, dii_name, attempts[dii_name], ordinal,
                    category="dat", endpoint=endpoint)
                shown = record_article(response, ordinal)
                assert documents[ordinal - 1]["source_version_id"] in shown
                assert dict(zip(documents[ordinal - 1]["headers"], documents[ordinal - 1]["cells"]))
                if ordinal == 1:
                    assert "0002" in shown and "RAW-DOC-ALPHA" in shown
            for view in ("fields", "text"):
                response = review.client.get(review.base + "/items/" + review.token("Generated/z-parent.png"),
                                             params={"view": view})
                assert response.status_code == 200 and "TAIL0099" not in response.text
        ordered = review.client.get(review.base, params={"sort": "name", "dir": "desc"})
        assert ordered.status_code == 200
        tokens = row_tokens(ordered.text)
        assert tokens.index(review.store.action_token(parent)) < tokens.index(review.store.action_token(child))
        assert "TAIL0099" not in ordered.text
        filtered = review.client.get(review.base, params={"q": "child.jpg"})
        assert row_tokens(filtered.text) == [review.store.action_token(child)]
        assert "z-parent.png" in filtered.text and "data-family-context" in filtered.text
        neighbor = review.bench.create_matter("Generated neighboring images", "", ACTOR)
        other = review_for(review.client, neighbor)
        upload_image_production(other, {"Other/native.png": image_bytes("PNG", "white")})
        for endpoint in (other.base, other.base + "/items/" + other.token("Other/native.png")):
            response = other.client.get(endpoint, params={"doc": other.token("Other/native.png"),
                "view": "document", "record": f"opt:{attempts[lfp_name]}:1"})
            assert response.status_code == 404 and "CASE0007" not in response.text
        for name in (lfp_name, dii_name):
            assert not review.document(name).units


@pytest.mark.parametrize("damage", ["duplicate-page", "conflicting-metadata", "conflicting-docid", "changed-bytes", "stale-generation"])
def test_invalid_input_never_publishes_associations(tmp_path, damage):
    with opened_review(tmp_path / "runtime") as review:
        files, lfp_name, dii_name = production_bytes(parent_count="0003" if damage == "conflicting-metadata" else "0002")
        if damage == "duplicate-page":
            files[lfp_name] += files[lfp_name].splitlines(keepends=True)[0].replace(b"z-parent.png", b"second.png")
        if damage == "conflicting-docid":
            files["Generated/contradictory.dii"] = files[dii_name].replace(b"RAW-DOC-ALPHA", b"CONTRADICTORY-DOCID")
        upload_image_production(review, files)
        order = ["Generated/families.dat", dii_name, lfp_name]
        if damage == "conflicting-docid":
            order.insert(0, "Generated/contradictory.dii")
        attempts = import_paths(review, order)
        parent = review.document("Generated/z-parent.png")
        if damage == "duplicate-page":
            rows = tuple(all_records(review, attempts[lfp_name], category="page"))
            assert len(rows) == 4 and rows[0]["cells"][1] == rows[-1]["cells"][1] == "CASE0007"
            assert all(row["association_status"] != "matched" for row in (rows[0], rows[-1]))
            assert any("duplicate" in code or "ambiguous" in code for row in (rows[0], rows[-1])
                       for code in record_findings(row))
            for ordinal in (1, 4):
                assert "CASE0007" in record_article(record_request(review, lfp_name, attempts[lfp_name], ordinal), ordinal)
        elif damage in {"conflicting-metadata", "conflicting-docid"}:
            projection = review.projection("Generated/z-parent.png")
            assert projection["state"] == "error" and not projection["values"]
            assert "conflicting_load_file_metadata" in projection["diagnostics"]
            for endpoint in (review.base, review.base + "/items/" + review.token("Generated/z-parent.png")):
                response = review.client.get(endpoint, params={"doc": review.token("Generated/z-parent.png"), "view": "fields"})
                assert response.status_code == 200 and "conflicting" in response.text.casefold()
            supplied = "0003" if damage == "conflicting-metadata" else "RAW-DOC-ALPHA"
            assert supplied in record_article(record_request(review, dii_name, attempts[dii_name], 1, category="dat"), 1)
            if damage == "conflicting-docid":
                assert "CONTRADICTORY-DOCID" in record_article(record_request(review,
                    "Generated/contradictory.dii", attempts["Generated/contradictory.dii"], 1, category="dat"), 1)
        elif damage == "changed-bytes":
            source = review.document(lfp_name)
            review.store.source_path(source.document_id).write_bytes(files[lfp_name].replace(b"TAIL0099", b"SWAP0099"))
            with pytest.raises(UploadProblem):
                review.store.process(source.document_id)
            assert review.document(lfp_name).state == "changed"
            assert review.store.source_changes.read([source.document_id])[0].state == "changed"
            assert review.repository.catalog(review.matter.matter_id, source.document_id)["source_state"] == "changed"
            fresh = ProductionService(review.service.context, repository=review.repository)
            fresh.bind(review.matter, review.store, review.definition)
            for _ in range(100):
                state = fresh.advance(review.matter.matter_id, max_steps=128)
                if state["state"] != "pending":
                    break
            assert state["state"] == "complete" and "lfp" not in state["counts"]
            assert review.repository.attempt(review.matter.matter_id, attempts[lfp_name])["status"] == "stale"
            assert review.repository.current_attempt(review.matter.matter_id, source.document_id) is None
            with pytest.raises(ProductionStale):
                fresh.record_window(review.matter.matter_id, attempts[lfp_name], category="page",
                    ordinal=2, document_id=parent.document_id)
            page = review.client.get(review.base + "/items/" + review.token("Generated/z-parent.png"),
                                     params={"view": "document"})
            assert page.status_code == 200 and "SWAP0099" not in page.text and "TAIL0099" not in page.text
        else:
            old = review.repository.attempt(review.matter.matter_id, attempts[lfp_name])
            review.repository.update_attempt(review.matter.matter_id, attempts[lfp_name], status="stale", phase="complete")
            for endpoint in ("full", "fragment"):
                response = record_request(review, "Generated/z-parent.png", attempts[lfp_name], 2, endpoint=endpoint)
                assert response.status_code == 409 and "reload" in response.text.casefold()
            assert old["version_id"] == review.document(lfp_name).version_id


@pytest.mark.parametrize("late_subscriber", [False, True], ids=["existing-subscriber", "late-subscriber"])
@pytest.mark.parametrize("source_format", ["dat", "lfp", "dii"])
def test_committed_source_change_withdraws_attempt_before_completion(tmp_path, source_format, late_subscriber):
    with opened_review(tmp_path / "runtime") as review:
        files, lfp_name, dii_name = production_bytes()
        upload_image_production(review, files)
        paths = {"dat": "Generated/families.dat", "lfp": lfp_name, "dii": dii_name}
        attempts = import_paths(review, list(paths.values()))
        source = review.document(paths[source_format])
        before = review.repository.attempt(review.matter.matter_id, attempts[paths[source_format]])
        revision = review.store.revision
        review.store.source_path(source.document_id).write_bytes(files[paths[source_format]] + b"\n")
        with pytest.raises(UploadProblem):
            review.store.process(source.document_id)
        assert review.store.revision > revision
        assert review.store.source_changes.read([source.document_id])[0].state == "changed"
        assert review.repository.catalog(review.matter.matter_id, source.document_id)["source_state"] == "changed"
        service = ProductionService(review.service.context, repository=review.repository) if late_subscriber else review.service
        service.bind(review.matter, review.store, review.definition)
        withdrawn = service.reconcile_attempt(review.matter.matter_id, before)
        assert withdrawn["status"] == "stale"
        assert withdrawn["counts"] == before["counts"]
        assert withdrawn["digest"] == before["digest"]
        assert review.repository.current_attempt(review.matter.matter_id, source.document_id) is None
        assert source_format not in review.repository.attempt_counts(review.matter.matter_id)
        with pytest.raises(ProductionStale):
            service.record_window(review.matter.matter_id, before["attempt_id"],
                category="page" if source_format == "lfp" else "document", ordinal=1,
                document_id=source.document_id)
        for _ in range(100):
            state = service.advance(review.matter.matter_id, max_steps=128)
            if state["state"] != "pending":
                break
        assert state["state"] == "complete" and source_format not in state["counts"]


@pytest.mark.parametrize("source_format", ["lfp", "dii"])
def test_historical_damaged_tail_preserves_citations_and_coverage(tmp_path, source_format):
    """Real manifest migration is historical preservation, never an admission bypass claim."""
    from tests.test_discovery_review_dat_opt_storage import app_case, native_id

    if source_format == "lfp":
        prefix = lfp_bytes([{"key": "GEN00000001", "filename": "first.png"}])
        tail = b"IM,UNFINISHED,D,0,\xff"
        category, cursor_kind = "page", "opt"
    else:
        prefix = dii_bytes([{"record_id": "retained-first", "begin_bates": "GEN00000001",
                            "page_count": "1", "images": ("first.png",)}])
        tail = b"@T retained-damaged\n@BATESBEG NOT-PUBLISHED\n@D @I\\\n\xff"
        category, cursor_kind = "document", "dat"
    body = b"\xef\xbb\xbf" + prefix + tail
    with app_case(tmp_path, count=1, extra_files=(("retained." + source_format, body),)) as case:
        review = review_for(case.client, case.matter)
        path = "Generated/retained." + source_format
        source = review.document(path)
        assert review.store.source_path(source.document_id, verify_digest=True).read_bytes() == body
        attempts = import_paths(review, ["Generated/metadata.dat", path],
                                expected_failed=(path,) if source_format == "dii" else ())
        failed = review.repository.attempt(review.matter.matter_id, attempts[path])
        records = tuple(all_records(review, attempts[path], category=category))
        assert records and records[0]["kind"] == "accepted" and records[0]["ordinal"] == 1
        assert records[0]["source_document_id"] == source.document_id
        assert records[0]["source_version_id"] == source.version_id
        assert failed["counts"]["accepted"] == 1 and failed["counts"]["rejected"] == 1
        assert len(records) == 2 and records[1]["kind"] == "rejected"
        assert records[1]["ordinal"] == 2 and records[1]["cells"] is None
        assert "mixed_encoding" in records[1]["diagnostics"]
        assert records[1]["association_status"] != "matched" and not records[1]["document_id"]
        if source_format == "dii":
            assert failed["status"] == "failed" and failed["phase"] == "error" and failed["file_failure"]
            assert all(record["association_status"] != "matched" for record in records)
            assert failed["counts"]["matched"] == 0
        else:
            # An independently bounded damaged LFP line does not erase the
            # preceding valid page; its rejected locator stays explicitly counted.
            assert failed["status"] == "complete" and not failed["file_failure"]
            assert records[1]["line"] == 2
        for endpoint in ("full", "fragment"):
            response = record_request(review, path, attempts[path], 1, category=cursor_kind, endpoint=endpoint)
            assert "GEN00000001" in record_article(response, 1)
            if source_format == "dii":
                assert "incomplete" in response.text.casefold() or "failed" in response.text.casefold()
            rejected = record_request(review, path, attempts[path], 2, category=cursor_kind, endpoint=endpoint)
            assert "mixed_encoding" in record_article(rejected, 2)
        if source_format == "dii":
            with pytest.raises((ProductionStale, KeyError)):
                review.service.record_window(review.matter.matter_id, attempts[path], category=category,
                    ordinal=1, document_id=native_id(1))
        assert review.store.source_path(source.document_id, verify_digest=True).read_bytes() == body


@pytest.mark.parametrize("ambiguous", [False, True], ids=["current-and-stale", "ambiguous-image"])
def test_produced_image_binding_rechecks_current_scope_and_uniqueness(tmp_path, ambiguous):
    with opened_review(tmp_path / "runtime") as review:
        files, lfp_name, dii_name = production_bytes()
        if ambiguous:
            files["Generated/one/same.png"] = image_bytes("PNG", "white")
            files["Generated/two/same.png"] = image_bytes("PNG", "black")
            files[lfp_name] = files[lfp_name].replace(b"@VOL01;Generated;z-parent.png;4", b"same.png;4")
        upload_image_production(review, files)
        attempt = import_paths(review, ["Generated/families.dat", dii_name, lfp_name])[lfp_name]
        parent = review.document("Generated/z-parent.png")
        window = review.service.record_window(review.matter.matter_id, attempt, category="page",
            ordinal=1, document_id=parent.document_id)
        binding_args = dict(matter_id=review.matter.matter_id, selected_document_id=parent.document_id,
                            selected_version_id=parent.version_id, window=window)
        binding = reader.produced_image_binding(review.repository, **binding_args)
        if ambiguous:
            assert window["current"]["association_status"] == "matched"
            assert window["current"]["image"] is None and binding is None
        else:
            assert binding["image"]["document_id"] == parent.document_id
        neighbor = review.bench.create_matter("Generated foreign binding", "", ACTOR)
        with pytest.raises(KeyError):
            reader.produced_image_binding(review.repository, **{**binding_args, "matter_id": neighbor.matter_id})
        with pytest.raises(KeyError):
            reader.produced_image_binding(review.repository, **{**binding_args, "selected_version_id": "0" * 32})
        for key, value in (("generation", "different-generated-publication"),
                           ("target", "different-generated-publication"), ("version_id", "0" * 32)):
            forged = {**window, "attempt": {**window["attempt"], key: value}}
            with pytest.raises(ProductionStale):
                reader.produced_image_binding(review.repository, **{**binding_args, "window": forged})
        forged = {**window, "current": {**window["current"], "raw_fingerprint": "0" * 64}}
        with pytest.raises(ProductionStale):
            reader.produced_image_binding(review.repository, **{**binding_args, "window": forged})
        review.repository.update_attempt(review.matter.matter_id, attempt, status="stale", phase="complete")
        with pytest.raises(ProductionStale):
            reader.produced_image_binding(review.repository, **binding_args)


@pytest.mark.parametrize("supplied", [None, "0002", "9223372036854775807"], ids=["absent", "literal", "mismatch"])
def test_dii_counts_remain_document_metadata(tmp_path, supplied):
    with opened_review(tmp_path / "runtime") as review:
        block = {"record_id": "separate-raw-identifier", "begin_bates": "CASE0007",
                 "directory": "Generated", "images": ("CASE0007.png", "second.png")}
        if supplied is not None:
            block["page_count"] = supplied
        files = {"Generated/CASE0007.png": image_bytes("PNG", "red"),
                 "Generated/second.png": image_bytes("PNG", "blue"),
                 "Generated/document.dii": dii_bytes([block])}
        upload_image_production(review, files)
        attempt_id = import_paths(review, ["Generated/document.dii"])["Generated/document.dii"]
        attempt = review.repository.attempt(review.matter.matter_id, attempt_id)
        rows = tuple(all_records(review, attempt_id))
        assert attempt["counts"]["accepted"] == 1 and len(rows) == 1
        assert tuple(all_records(review, attempt_id, category="page")) == ()
        optional = dict(rows[0]["optional_values"])
        assert optional["document_begin"] == "CASE0007"
        assert optional["observed_image_entries"] == "2"
        assert optional.get("declared_count") == supplied
        assert ("page_count_mismatch" in record_findings(rows[0])) is (supplied == "9223372036854775807")
        projected = review.projection("Generated/CASE0007.png")
        assert projected["values"]["begin_bates"] == "CASE0007" and projected["family"] == {}
        assert projected["values"].get("load_file_pages") == supplied
        for endpoint in (review.base, review.base + "/items/" + review.token("Generated/CASE0007.png")):
            response = review.client.get(endpoint, params={"doc": review.token("Generated/CASE0007.png"), "view": "document"})
            assert response.status_code == 200
            assert "Supplied page Bates" not in response.text and 'data-record-kind="opt"' not in response.text
            assert "CASE0008" not in response.text and "CASE0009" not in response.text
        shown = record_article(record_request(review, "Generated/document.dii", attempt_id, 1, category="dat"), 1)
        assert "separate-raw-identifier" in shown
        if supplied is not None:
            assert supplied in shown


@pytest.mark.parametrize("source_format", ["lfp", "dii"])
def test_image_index_resume_retains_exact_records_and_locators(tmp_path, source_format):
    count = 260
    if source_format == "lfp":
        body = lfp_bytes([{"key": f"ONLY{ordinal:05}", "filename": f"image-{ordinal}.png"}
                          for ordinal in range(1, count + 1)])
        category, cursor_kind = "page", "opt"
    else:
        body = dii_bytes([{"record_id": f"block-{ordinal}", "begin_bates": f"ONLY{ordinal:05}",
                          "page_count": "0001", "images": (f"image-{ordinal}.png",)}
                         for ordinal in range(1, count + 1)])
        category, cursor_kind = "document", "dat"
    with opened_review(tmp_path / "runtime") as review:
        name = "Generated/independent." + source_format
        upload_image_production(review, {name: body})
        source = review.identity(name)
        target = f"{review.store.revision}:{definition_digest(review.definition)}"
        prior_count, cursors = 0, set()
        for _ in range(100):
            attempt = review.service.import_source(review.matter.matter_id, source,
                review.store.source_path(source.document_id, verify_digest=True), review.definition,
                target=target, max_records=7, max_bytes=4 * 1024 * 1024)
            assert attempt["status"] == "building" and attempt["counts"]["rejected"] == 0
            current_count = attempt["counts"]["accepted"]
            assert prior_count < current_count <= min(prior_count + 7, count)
            if attempt["phase"] != "parse":
                break
            cursor = json.dumps(attempt["cursor"], sort_keys=True)
            assert cursor not in cursors
            cursors.add(cursor)
            assert review.projection(name)["state"] == "pending"
            prior_count = current_count
        else:
            pytest.fail("Completed-record cursors made insufficient progress")
        assert current_count == count and len(cursors) == count // 7
        for _ in range(100):
            state = review.service.advance(review.matter.matter_id, max_steps=128)
            if state["state"] != "pending":
                break
        assert state["state"] == "complete"
        rows = assertions_for_provenance(review, attempt["attempt_id"], category, count)
        assert all(row["association_status"] != "matched" for row in rows)
        assert review.repository.attempt(review.matter.matter_id, attempt["attempt_id"])["counts"]["accepted"] == count
        assert tuple(all_records(review, attempt["attempt_id"], category="document" if category == "page" else "page")) == ()
        for endpoint in ("full", "fragment"):
            tail = record_request(review, name, attempt["attempt_id"], count, category=cursor_kind, endpoint=endpoint)
            assert f"ONLY{count:05}" in record_article(tail, count)
            assert 'data-record-direction="next"' not in tail.text
            before = record_request(review, name, attempt["attempt_id"], count, direction="previous",
                                    category=cursor_kind, endpoint=endpoint)
            assert f"ONLY{count - 1:05}" in record_article(before, count - 1)
        assert review.store.source_path(source.document_id, verify_digest=True).read_bytes() == body


def test_deleted_index_reupload_invalidates_old_source_version(tmp_path):
    with opened_review(tmp_path / "runtime") as review:
        files, lfp_name, dii_name = production_bytes()
        upload_image_production(review, files)
        old_attempt = import_paths(review, ["Generated/families.dat", dii_name, lfp_name])[lfp_name]
        old_source = review.document(lfp_name)
        response = review.client.post(f"/matters/{review.matter.slug}/sources/{review.token(lfp_name)}/remove",
                                      follow_redirects=False)
        assert response.status_code == 303
        replacement = files[lfp_name].replace(b"TAIL0099", b"OTHER077")
        upload_image_production(review, {lfp_name: replacement})
        new_source = review.document(lfp_name)
        assert new_source.document_id != old_source.document_id and new_source.version_id != old_source.version_id
        new_attempt = import_paths(review, [lfp_name])[lfp_name]
        assert new_attempt != old_attempt
        rows = tuple(all_records(review, new_attempt, category="page"))
        assert rows[1]["cells"][1] == "OTHER077" and all(row["association_status"] != "matched" for row in rows)
        for endpoint in ("full", "fragment"):
            stale = record_request(review, "Generated/z-parent.png", old_attempt, 2, endpoint=endpoint)
            assert stale.status_code in {404, 409} and "TAIL0099" not in stale.text
        assert review.repository.provenance(review.matter.matter_id, new_source.document_id, new_source.version_id) != \
            review.repository.provenance(review.matter.matter_id,
                review.document("Generated/z-parent.png").document_id, review.document("Generated/z-parent.png").version_id)


@pytest.mark.parametrize("cross_suffix", [False, True], ids=["ordinary-names", "cross-suffix"])
def test_load_file_lifecycle_preserves_originals(tmp_path, cross_suffix):
    runtime = tmp_path / "runtime"
    with opened_review(runtime) as review:
        files, lfp_name, dii_name = production_bytes(cross_suffix=cross_suffix)
        upload_image_production(review, files)
        attempts = import_paths(review, ["Generated/families.dat", dii_name, lfp_name])
        slug = review.matter.slug
        originals = {name: (review.document(name).document_id, review.document(name).version_id,
                           review.document(name).digest) for name in files}
    with opened_review(runtime, slug) as review:
        resumed = import_paths(review, ["Generated/families.dat", dii_name, lfp_name])
        for name, previous in attempts.items():
            if resumed[name] != previous:
                assert review.repository.attempt(review.matter.matter_id, previous)["status"] == "stale"
        attempts = resumed
        for name, body in files.items():
            document = review.document(name)
            assert (document.document_id, document.version_id, document.digest) == originals[name]
            assert review.store.source_path(document.document_id, verify_digest=True).read_bytes() == body
        neighbor = review.bench.create_matter("Generated foreign preservation", "", ACTOR)
        other = review_for(review.client, neighbor)
        foreign_files, foreign_lfp, foreign_dii = production_bytes()
        foreign_files[foreign_dii] = foreign_files[foreign_dii].replace(b"RAW-DOC-ALPHA", b"FOREIGN-ARCHIVE-CANARY")
        upload_image_production(other, foreign_files)
        foreign_attempts = import_paths(other, ["Generated/families.dat", foreign_dii, foreign_lfp])
        foreign_before = other.repository.attempt(other.matter.matter_id, foreign_attempts[foreign_dii])
        denied = review.client.post(f"/matters/{neighbor.slug}/sources/{review.token(lfp_name)}/remove", follow_redirects=False)
        assert denied.status_code == 404 and review.store.source_path(originals[lfp_name][0]).read_bytes() == files[lfp_name]
        session = lifecycle_session(review.bench.workspace, ACTOR)
        review.bench.workspace.case_lifecycle.begin_exit(review.matter.slug, ACTOR, session_id=session.session_id)
        artifact = build_matter_archive(review.bench, review.matter, ACTOR, source_store=review.store)
        try:
            manifest = verify_matter_archive(artifact.path, expected_sha256=artifact.sha256)
            with zipfile.ZipFile(artifact.path) as archive:
                for name, body in files.items():
                    retained = archive.read("originals/" + originals[name][0])
                    assert retained == body and hashlib.sha256(retained).hexdigest() == originals[name][2]
                for table in ("workbench_discovery_production", "workbench_discovery_dat_record", "workbench_discovery_opt_page"):
                    assert manifest["coverage"][table]["disposition"] == "included"
                    rows = [json.loads(line) for line in archive.read("records/" + table + ".jsonl").splitlines()]
                    assert rows and all(row["matter_id"] == review.matter.matter_id for row in rows)
                material = b"\n".join(archive.read(name) for name in archive.namelist())
                assert b"FOREIGN-ARCHIVE-CANARY" not in material and neighbor.matter_id.encode() not in material
        finally:
            cleanup_archive(artifact)
        changed = review.store.files / review.document(lfp_name).stored_name
        changed.write_bytes(files[lfp_name].replace(b"TAIL0099", b"SWAP0099"))
        with pytest.raises(ArchiveProblem):
            build_matter_archive(review.bench, review.matter, ACTOR, source_store=review.store)
        changed.write_bytes(files[lfp_name])
        prepared, lifecycle = claim_bench_exit(review.bench, review.matter)
        assert review.bench.execute_matter_purge(prepared, lifecycle).state == "deleted"
        assert review.repository.attempts(review.matter.matter_id, include_stale=True) == ()
        for attempt in attempts.values():
            assert review.repository.records(review.matter.matter_id, attempt, category="document") == ()
            assert review.repository.records(review.matter.matter_id, attempt, category="page") == ()
        assert other.repository.attempt(other.matter.matter_id, foreign_attempts[foreign_dii]) == foreign_before
        assert other.store.source_path(other.document(foreign_dii).document_id, verify_digest=True).read_bytes() == foreign_files[foreign_dii]
