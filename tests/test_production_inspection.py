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


def test_reference_qc_counts_each_occurrence_and_cites_late_dat_rows(tmp_path):
    native = r"NATIVES\One.PDF"
    paths = [native] * 21 + ["gone.pdf", "../outside.pdf", "Volume/images/both.pdf",
                            "unsupported.bin", "foreign.pdf", "case.pdf", "byname.txt"]
    body = dat_record(("NOTE", "NATIVEPATH")) + "".join(dat_record(("multi\nline", ref)) for ref in paths)
    selected = [["Volume/DATA/index.dat", True], ["Volume/Natives/one.pdf", True],
                ["Volume/images/both.pdf", True], ["Volume/Volume/images/both.pdf", True],
                ["Volume/unsupported.bin", False], ["Other/foreign.pdf", True],
                ["Volume/case.pdf", True], ["Volume/CASE.PDF", False], ["Volume/extras/byname.txt", True],
                ["volume/foreign.pdf", True], ["../Volume/gone.pdf", False]]
    report = inspect_load_file(write_file(tmp_path, "data", body.encode()), ".dat",
        inventory={"index": 0, "files": selected})
    assert len(report["rows"]) == 20 and report["rows"][0]["references"][0]["status"] == "matched"
    qc = report["reference_qc"]
    assert qc["total"] == 28
    assert qc["counts"] == {"matched": 21, "missing": 2, "unsafe": 1, "ambiguous": 2, "ineligible": 1, "name_only": 1}
    first = qc["examples"][0]
    assert (first["path"], first["role"], first["ordinal"], first["line"]) == ("gone.pdf", "native", 22, 44)


def test_dii_reference_qc_counts_beyond_sample_and_cites_originating_block(tmp_path):
    blocks = [{"record_id": f"r{number}", "begin_bates": f"X{number}", "directory": "Images",
               "images": ("ok.png",) * 9 + ("late.png",) * 3} for number in range(23)]
    report = inspect_load_file(write_file(tmp_path, "index", dii_bytes(blocks)), ".dii",
        inventory={"index": 1, "files": [["Vol/Images/ok.png", True], ["Vol/index.dii", True]]})
    qc = report["reference_qc"]
    assert qc["total"] == 276 and qc["counts"]["matched"] == 207 and qc["counts"]["missing"] == 69
    assert len(qc["examples"]) == 5 and qc["examples_truncated"]
    assert [(s["ordinal"], s["line"], s["path"]) for s in qc["examples"]] == [
        (1, 2, "Images/late.png")] * 3 + [(2, 17, "Images/late.png")] * 2
    assert all(row["references_truncated"] and len(row["references"]) == 8 for row in report["rows"])


def test_loose_selection_names_do_not_prove_a_production_path(tmp_path):
    source = write_file(tmp_path, "index", b"A1,V,Images/A1.png,Y,,,1\n")
    manifest = {"index": 0, "files": [["index.opt", True], ["a1.PNG", True], ["Foreign/Images/A1.png", True]]}
    qc = inspect_load_file(source, ".opt", inventory=manifest)["reference_qc"]
    assert qc["counts"]["name_only"] == 1 and qc["counts"]["matched"] == 0


@pytest.mark.parametrize("manifest", [
    {}, {"index": True, "files": [["index.opt", True]]}, {"index": 1, "files": [["index.opt", True]]},
    {"index": 0, "files": [["index.opt", False]]}, {"index": 0, "files": [["../index.opt", True]]},
    {"index": 0, "files": [["index.opt", True], ["INDEX.OPT", False]]},
    {"index": 0, "files": [["index.opt", True], ["x.png", 1]]},
])
def test_invalid_inventory_cannot_return_a_clean_reference_report(tmp_path, manifest):
    source = write_file(tmp_path, "index", b"A1,V,A1.png,Y,,,1\n")
    with pytest.raises(ValueError):
        inspect_load_file(source, ".opt", inventory=manifest)
