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


def profiled_volume(review, root, columns, mapping, *, expected_digest=None):
    index = root + "/DATA/index.dat"
    native = root + "/NATIVES/original.txt"
    data = dat_bytes(columns, [("RX001", "RX002", "NATIVES/original.txt", "2025-02-04", root)])
    files = {index: data, native: source_text(len(root))}
    profile = {"producer": root, "sha256": expected_digest or hashlib.sha256(data).hexdigest(),
               "fields": mapping}
    items = [dict(descriptor(index, len(data)), dat_profile=profile),
             descriptor(native, len(files[native]))]
    receipt = selection(review.client, review.matter.slug, items, [0, 1], key=uuid.uuid4().hex)
    upload_production(review, files, receipt=receipt)
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
