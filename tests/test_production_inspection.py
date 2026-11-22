"""Preview bounded synthetic load-file records before a selection is admitted."""
import json

import pytest

from case_intelligence.modules.discovery_review.inspection import inspect_load_file
from case_intelligence.source_load_files import LoadFileError
from tests.fixtures.load_files.generate import dat_record, opt_page, write_file
from tests.fixtures.load_files.generate_image_indexes import dii_bytes, lfp_bytes


def test_dat_preview_counts_logical_records_preserving_text_and_locator(tmp_path):
    headers = ("BEGBATES", "NATIVEPATH", "TEXTPATH", "NOTE")
    body = dat_record(headers) + "".join(dat_record((str(i), f"N/{i}.pdf", f"T/{i}.txt",
        "<example>\nsecond physical line")) for i in range(27))
    staged = write_file(tmp_path, "staged", body.encode("utf-16"))
    result = inspect_load_file(staged, ".DAT")
    assert (result["format"], result["encoding"], result["record_kind"]) == ("dat", "utf-16", "document")
    assert result["record_count"] == 27
    assert len(result["rows"]) == result["sample_limit"] == 20
    assert result["diagnostic_counts"] == {}
    last = result["rows"][-1]
    assert (last["ordinal"], last["line"]) == (20, 40)
    assert last["fields"][-1] == {"label": "NOTE", "value": "<example>\nsecond physical line"}
    assert not last["fields_truncated"] and not last["references_truncated"]
    assert last["references"] == [{"role": "native", "path": "N/19.pdf"},
                                  {"role": "text", "path": "T/19.txt"}]


def test_page_formats_show_only_produced_references(tmp_path):
    opt = write_file(tmp_path, "opt", (",".join(opt_page("SYN07")) + "\n").encode())
    lfp = write_file(tmp_path, "lfp", lfp_bytes())
    for staged, suffix, kind, total, image in (
            (opt, ".opt", "opt", 1, "IMAGES/SYN07.tif"),
            (lfp, ".dii", "lfp", 3, "Generated/Images/café.png")):
        result = inspect_load_file(staged, suffix)
        assert result["format"] == kind and result["record_kind"] == "page"
        assert result["record_count"] == total
        assert result["rows"][0]["references"] == [{"role": "image", "path": image}]


def test_dii_document_refs_and_advisory_totals_include_unsampled_blocks(tmp_path):
    documents = [{"record_id": f"doc-{i}", "begin_bates": f"SYN{i}", "page_count": "4",
                  "directory": "images", "images": ("one.png", "two.png")} for i in range(22)]
    result = inspect_load_file(write_file(tmp_path, "staged", dii_bytes(documents)), ".lfp")
    assert (result["format"], result["record_kind"], result["record_count"]) == ("dii", "document", 22)
    assert result["diagnostic_counts"] == {"page_count_mismatch": 22}
    assert result["rows"][0]["references"] == [
        {"role": "image", "path": "images/one.png"}, {"role": "image", "path": "images/two.png"}]
    assert {field["label"] for field in result["rows"][0]["fields"]} >= {"@BATESBEG", "@C PGCOUNT"}


def test_long_or_numerous_values_cannot_escape_the_preview_bounds(tmp_path):
    huge = "q" * 40000
    dat = dat_record(("NATIVEPATH", *[f"column-{i}" for i in range(100)]))
    dat += dat_record((huge + ".pdf", *["text"] * 100))
    result = inspect_load_file(write_file(tmp_path, "dat", dat.encode()), ".dat")
    row = result["rows"][0]
    assert row["fields_truncated"] and row["references_truncated"]
    assert not row["references"]
    assert len(json.dumps(result)) < 10000
    images = (huge + ".tif", *[f"{i}.tif" for i in range(500)])
    dii = dii_bytes([{"record_id": "wide", "begin_bates": "SYN01", "images": images}])
    result = inspect_load_file(write_file(tmp_path, "dii", dii), ".dii")
    row = result["rows"][0]
    assert row["references_truncated"] and 0 < len(row["references"]) < 500
    assert {"role": "image", "path": "0.tif"} in row["references"]
    assert all(ref["path"] in images for ref in row["references"])
    assert len(json.dumps(result)) < 10000


def test_malformed_tail_never_returns_a_successful_preview(tmp_path):
    body = (",".join(opt_page("SYN00")) + "\n") * 28 + "bad,tail\n"
    with pytest.raises(LoadFileError) as exc:
        inspect_load_file(write_file(tmp_path, "opt", body.encode()), ".opt")
    assert (exc.value.code, exc.value.line) == ("field_count_mismatch", 29)


def test_mapping_headers_keep_exact_spelling_and_omit_ambiguous_suggestions(tmp_path):
    headers = [" Begin Bates ", "BEGDOC", "NATIVE PATH", "Vendor date", "é" * 201, "__proto__"]
    staged = write_file(tmp_path, "staged", (dat_record(headers) + dat_record(["value"] * len(headers))).encode())
    profile = inspect_load_file(staged, ".dat")["mapping"]
    assert profile["headers"] == [*headers[:4], "__proto__"]
    assert profile["header_count"] == 6
    assert profile["suggestions"] == {"NATIVE PATH": "native_path"}


@pytest.mark.parametrize(("headers", "suggestions"), [
    (("Text Path",), {"Text Path": "text_path"}),
    (("Text Path", "TEXT"), {}),
])
def test_text_path_suggestions_require_one_recognized_header(tmp_path, headers, suggestions):
    paths = [f"TEXT/{i}.txt" for i in range(len(headers))]
    staged = write_file(tmp_path, "sample", (dat_record(headers) + dat_record(paths)).encode())
    report = inspect_load_file(staged, ".dat")
    assert report["mapping"]["suggestions"] == suggestions
    assert report["rows"][0]["references"] == [{"role": "text", "path": path} for path in paths]
