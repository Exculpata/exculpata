"""Interpret only the DAT profile confirmed for this source occurrence."""
import hashlib
import uuid

import pytest

from case_intelligence.modules.discovery_review import fields
from case_intelligence.modules.discovery_review.production import definition_digest
from case_intelligence.modules.discovery_review.production.matching import (
    DatProfileError, profile_positions, project_record,
)
from tests.test_discovery_review_dat_opt import (
    all_records, complete_listing, dat_bytes, import_paths, opened_review, source_text, upload_production,
)
from tests.test_intake_receipt_http import descriptor, selection
from tests.test_production_text_companions import dat_files, file_link, open_production_list


def upload_mapped_files(review, files, index, mapping, *, expected_digest=None):
    profile = {"producer": index.split("/")[0], "sha256": expected_digest or hashlib.sha256(files[index]).hexdigest(),
               "fields": mapping}
    items = [descriptor(path, len(body)) for path, body in files.items()]
    items[list(files).index(index)]["dat_profile"] = profile
    receipt = selection(review.client, review.matter.slug, items, list(range(len(files))), key=uuid.uuid4().hex)
    upload_production(review, files, receipt=receipt)


def profiled_volume(review, root, columns, mapping, *, expected_digest=None):
    index = root + "/DATA/index.dat"
    native = root + "/NATIVES/original.txt"
    data = dat_bytes(columns, [("RX001", "RX002", "NATIVES/original.txt", "2025-02-04", root)])
    files = {index: data, native: source_text(len(root))}
    upload_mapped_files(review, files, index, mapping, expected_digest=expected_digest)
    return index, native, data


def test_two_producers_reuse_bates_and_keep_exact_profile_bindings(tmp_path):
    with opened_review(tmp_path / "runtime") as review:
        volumes = []
        for root, headers in (("First", ("FROM", "Through", "Link A", "BEGBATES", "CUSTODIAN")),
                              ("Second", ("Start B", "Finish B", "Link B", "Date B", "CUSTODIAN"))):
            mapping = dict(zip(headers, ("begin_bates", "end_bates", "native_path", "date")))
            volumes.append(profiled_volume(review, root, headers, mapping))
        attempts = import_paths(review, [index for index, native, data in volumes])
        for index, native, data in volumes:
            row, = all_records(review, attempts[index])
            assert row["association_status"] == "matched"
            assert row["document_id"] == review.document(native).document_id
            assert row["cells"][0:2] == ["RX001", "RX002"]
            projection = review.projection(native)
            assert projection["values"]["begin_bates"] == "RX001"
            assert projection["values"]["end_bates"] == "RX002"
            assert projection["values"]["document_date"] == "2025-02-04"
            assert projection["values"]["custodian"] == native.split("/")[0]
            assert "email_from" not in projection["values"]
            assert tuple(projection["raw_fields"]) == tuple(zip(row["headers"], row["cells"]))
            assert review.store.source_path(review.document(index).document_id).read_bytes() == data
            listed = complete_listing(review, doc=review.token(native), view="fields")
            assert review.token(native) in listed.text and "RX001" in listed.text


@pytest.mark.parametrize("failure", ["digest", "header"])
def test_confirmed_profile_failure_withholds_all_associations(tmp_path, failure):
    with opened_review(tmp_path / "runtime") as review:
        headers = ("BEGBATES", "ENDBATES", "NATIVEPATH", "DATE", "CUSTODIAN")
        heading = "No such heading" if failure == "header" else "BEGBATES"
        index, native, _ = profiled_volume(review, "Volume", headers, {heading: "begin_bates"},
                                         expected_digest="a" * 64 if failure == "digest" else None)
        attempts = import_paths(review, [index], expected_failed=[index])
        attempt = review.repository.attempt(review.matter.matter_id, attempts[index])
        assert attempt["status"] == "failed"
        assert "confirmed" in attempt["file_failure"].lower() and failure in attempt["file_failure"].lower()
        assert "begin_bates" not in review.projection(native)["values"]
        assert attempt["file_failure"] in review.projection(index)["diagnostics"]


def test_resume_after_header_uses_original_mapped_positions(tmp_path):
    runtime = tmp_path / "runtime"
    with opened_review(runtime) as review:
        index, native, _ = profiled_volume(review, "Resume", ("Custom", "End", "Native", "Date", "Origin"),
                                           {"Custom": "begin_bates", "Native": "native_path"})
        slug = review.matter.slug
        source = review.identity(index)
        header = review.service.import_source(review.matter.matter_id, source,
            review.store.source_path(source.document_id), review.definition,
            target=f"{review.store.revision}:{definition_digest(review.definition)}", max_records=1)
        assert header["phase"] == "parse" and header["counts"]["accepted"] == 0
        assert header["profile_positions"] == [[0, "begin_bates"], [2, "native_path"]]
    with opened_review(runtime, slug=slug) as review:
        import_paths(review, [index])
        assert review.projection(native)["values"]["begin_bates"] == "RX001"


def test_explicit_family_and_native_roles_supersede_default_aliases():
    headers = ("BEGATTACH", "Household", "Terminal", "NATIVEPATH", "Original")
    values = ("older declaration", "SYN0001", "SYN0003", "other/file.txt", "confirmed/file.txt")
    mapping = [("Household", "begin_attachment"), ("Terminal", "end_attachment"), ("Original", "native_path")]
    projected = project_record(headers, values, fields.load_definition(), profile_positions(headers, mapping))
    assert projected.values == {"begin_attachment": "SYN0001", "end_attachment": "SYN0003"}
    assert projected.paths == ("confirmed/file.txt",)
    assert projected.raw_fields == tuple(zip(headers, values))
    with pytest.raises(DatProfileError, match="header"):
        profile_positions((*headers, "Household"), mapping)


def test_projection_distinguishes_conflicting_interpretations_of_identical_text_cells():
    headers = ("BEGBATES", "NATIVEPATH", "TEXTPATH", "Transcript")
    cells = ("R001", "NATIVES/R001.pdf", "OCR/first.txt", "OCR/second.txt")
    definition = fields.load_definition()
    automatic = project_record(headers, cells, definition)
    confirmed = project_record(headers, cells, definition, [[2, "text_path"]])
    other = project_record(headers, cells, definition, [[3, "text_path"]])
    assert automatic.fingerprint == confirmed.fingerprint != other.fingerprint
    assert automatic.raw_fields == other.raw_fields == tuple(zip(headers, cells))
    assert automatic.paths == other.paths == ("NATIVES/R001.pdf",)
    assert automatic.values == other.values == {"begin_bates": "R001"}


def test_custom_text_column_links_the_admitted_companion_without_establishing_identity(tmp_path):
    with opened_review(tmp_path / "runtime") as review:
        index = "Volume/DATA/index.dat"
        files = {index: dat_bytes(("BEGBATES", "Original", "Transcript", "TEXTPATH"), [
                     ("RX001", r"NATIVES\RX001.txt", r"TEXT\RX001.txt", "OTHER/old.txt"),
                     ("RX002", "", "TEXT/RX002.txt", ""),
                     ("RX003", "NATIVES/RX003.txt", "TEXT/RX003.txt", "")]),
                 "Volume/NATIVES/RX001.txt": source_text(1), "Volume/TEXT/RX001.txt": source_text(2),
                 "Volume/OTHER/old.txt": source_text(3), "Volume/TEXT/RX002.txt": source_text(4),
                 "Volume/NATIVES/RX003.txt": source_text(5), "Volume/TEXT/RX003.txt": source_text(6),
                 "Volume/OTHER/RX003.txt": source_text(7)}
        upload_mapped_files(review, files, index, {"Original": "native_path", "Transcript": "text_path"})
        open_production_list(review)
        for selected in (index, "Volume/NATIVES/RX001.txt"):
            article = dat_files(review, selected)
            assert r"TEXT\RX001.txt" in article and "OTHER/old.txt" in article
            for role, path in (("matched", "Volume/NATIVES/RX001.txt"), ("text", "Volume/TEXT/RX001.txt")):
                url = file_link(article, role)
                assert url == f"/matters/{review.matter.slug}/sources/{review.token(path)}"
                assert review.client.get(url + "/content").content == files[path]
        text_only = dat_files(review, index, 2)
        assert file_link(text_only, "matched") is None
        assert file_link(text_only, "text") == f"/matters/{review.matter.slug}/sources/{review.token('Volume/TEXT/RX002.txt')}"
        conflict = dat_files(review, index, 3)
        assert file_link(conflict, "text") is None and file_link(conflict, "matched") is None
        assert "ambiguous_match" in conflict


def test_standard_text_column_remapped_to_native_loses_text_authority(tmp_path):
    with opened_review(tmp_path / "runtime") as review:
        index = "Volume/DATA/index.dat"
        files = {index: dat_bytes(("BEGBATES", "TEXTPATH", "NATIVEPATH"), [
                     ("RX001", "confirmed/file.txt", "former/decoy.txt")]),
                 "Volume/confirmed/file.txt": source_text(1), "Volume/former/decoy.txt": source_text(2)}
        upload_mapped_files(review, files, index, {"TEXTPATH": "native_path", "NATIVEPATH": "date"})
        open_production_list(review)
        article = dat_files(review, index)
        assert file_link(article, "matched") == f"/matters/{review.matter.slug}/sources/{review.token('Volume/confirmed/file.txt')}"
        assert file_link(article, "text") is None
        assert "former/decoy.txt" in article
