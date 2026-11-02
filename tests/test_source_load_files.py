from pathlib import Path

import pytest

from case_intelligence.source_load_files import LoadFileError, parse_dat, parse_opt, resolve_volume_path


def _dat_bytes(rows, encoding="cp1252"):
    return ("\n".join("\x14".join(f"\xfe{value}\xfe" for value in row) for row in rows) + "\n").encode(encoding)


def test_dat_preserves_bates_and_special_characters(tmp_path: Path):
    path = tmp_path / "synthetic.dat"
    path.write_bytes(_dat_bytes([
        ("BEGDOC", "TITLE"),
        ("PRE-0007-A", "alpha\x14inside\xaecontinued"),
    ]))  # fixture-write: Create a synthetic load file for parser coverage.
    rows = list(parse_dat(path))
    assert rows[0].values == ("PRE-0007-A", "alpha\x14inside\ncontinued")
    assert rows[0].line == 2


def test_opt_preserves_page_keys_exactly(tmp_path: Path):
    path = tmp_path / "synthetic.opt"
    path.write_text("PRE-0007-A,VOL001,IMAGES/P1.TIF,Y,,,1\n", encoding="utf-8")  # fixture-write: Create a synthetic page load file.
    assert list(parse_opt(path))[0].image_key == "PRE-0007-A"


@pytest.mark.parametrize("encoding", ["cp1252", "utf-8", "utf-16"])
def test_supported_dat_encodings_are_identical(tmp_path: Path, encoding: str):
    path = tmp_path / f"synthetic-{encoding}.dat"
    text = "þBEGDOCþ\x14þENDDOCþ\nþSYN0001þ\x14þSYN0002þ\n"
    path.write_bytes(text.encode(encoding))  # fixture-write: Create equivalent synthetic encoding variants.
    assert [row.values for row in parse_dat(path)] == [("SYN0001", "SYN0002")]


@pytest.mark.parametrize("tail", ["þunfinished", "þaþ\x14þbþ"])
def test_truncated_or_wrong_width_dat_is_cited(tmp_path: Path, tail: str):
    path = tmp_path / "broken.dat"
    path.write_bytes(("þAþ\n" + tail).encode("cp1252"))  # fixture-write: Create a hostile synthetic DAT row.
    with pytest.raises(LoadFileError, match=r"broken.dat:2:"):
        list(parse_dat(path))


def test_middle_bom_is_rejected(tmp_path: Path):
    path = tmp_path / "mixed.dat"
    path.write_bytes(b"\xfeA\xfe\n\xef\xbb\xbf\xfeB\xfe\n")  # fixture-write: Create a synthetic mixed-encoding marker.
    with pytest.raises(LoadFileError, match="mixed encoding"):
        list(parse_dat(path))


@pytest.mark.parametrize("hostile", ["../outside.txt", "/outside.txt"])
def test_volume_paths_reject_escape(tmp_path: Path, hostile: str):
    with pytest.raises(LoadFileError, match="contained"):
        resolve_volume_path(tmp_path, hostile, "synthetic.dat", 2)


def test_volume_paths_reject_outward_symlink(tmp_path: Path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("synthetic", encoding="utf-8")  # fixture-write: Create a synthetic outside target for containment coverage.
    (tmp_path / "link.txt").symlink_to(outside)  # fixture-write: Create a hostile synthetic volume symlink.
    with pytest.raises(LoadFileError, match="contained"):
        resolve_volume_path(tmp_path, "link.txt", "synthetic.dat", 3)

from case_intelligence import source_load_files as lf
import hashlib
import runpy
import time
import tracemalloc


def volume(tmp_path, rows, opt="", encoding="cp1252"):
    dat, pages = tmp_path / "test.dat", tmp_path / "test.opt"
    dat.write_bytes(_dat_bytes(rows, encoding))  # fixture-write: Synthetic DAT payload.
    pages.write_bytes(opt.encode(encoding))  # fixture-write: Synthetic OPT payload.
    return dat, pages


def codes(report):
    return {(f.code, f.line) for f in report.findings}


def test_qc_recovers_ragged_empty_encoding_and_truncated_rows(tmp_path):
    dat, opt = volume(tmp_path, [("BEGDOC", "ENDDOC", "TITLE"), ("A1", "A1", "ok"),
                                tuple("x" for _ in range(30)), ("", "", ""), ("A2", "A2", "ok")])
    with dat.open("ab") as stream:
        stream.write(b"\xfeA3\xfe\x14\xfeA3\xfe\x14\xfe\x81\xfe\n\xfeunfinished")  # fixture-write: Bad encoding and truncation.
    report = lf.qc_volume(dat, opt)
    assert report.document_count == 3
    assert {("field_count_mismatch", 3), ("mixed_encoding", 6), ("truncated_row", 7)} <= codes(report)


@pytest.mark.parametrize("encoding", ["cp1252", "utf-8", "utf-16", "utf-16-be"])
def test_cr_only_multibyte_peek(tmp_path, encoding):
    data = "þA þ\rþB þ\r".encode(encoding)
    if encoding == "utf-16-be":
        data = b"\xfe\xff" + data
    path = tmp_path / "cr.dat"
    path.write_bytes(data)  # fixture-write: CR-only encoding regression payload.
    assert [r.values for r in parse_dat(path)] == [("B ",)]


def test_encoding_prefix_boundary_and_cp1252_thorn_ydieresis(tmp_path, monkeypatch):
    dat, opt = volume(tmp_path, [("TITLE",), ("x" * 65525 + "é",)], encoding="utf-8")
    # é starts at byte 65535, across the detection boundary.
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("whole-file read"))
    assert next(parse_dat(dat)).values == ("x" * 65525 + "é",)
    dat, opt = volume(tmp_path, [("TITLE",), ("ÿx",)])
    assert next(parse_dat(dat)).values == ("ÿx",)
    dat.write_bytes("þAþ\nþBþ\n".encode("utf-32"))  # fixture-write: Unsupported BOM.
    assert ("mixed_encoding", 1) in codes(lf.qc_volume(dat, opt))


def test_opt_encoding_error_and_blank_line(tmp_path):
    path = tmp_path / "bad.opt"
    path.write_bytes(b"\x81,V,p,Y,,,1\n")  # fixture-write: First-line decoding failure.
    with pytest.raises(LoadFileError, match=r"bad.opt:1:"):
        list(parse_opt(path))
    path.write_bytes(b"A1,V,p,Y,,,1\n\n")  # fixture-write: Trailing blank OPT line.
    assert len(list(parse_opt(path))) == 1


@pytest.mark.parametrize("headers", [("BEGDOC", "BEGDOC"), ("BEGDOC", "")])
def test_ambiguous_headers_never_collapse(tmp_path, headers):
    dat, opt = volume(tmp_path, [headers, ("A", "B")])
    assert ("duplicate_header" if headers[1] else "empty_header", 1) in codes(lf.qc_volume(dat, opt))
    with pytest.raises(LoadFileError):
        lf.DatRow(("A", "B"), headers, str(dat), 2).as_dict()


@pytest.mark.parametrize("path", ["C:\\x", "\\\\srv\\sh\\x", "..\\..\\x", "TEXT\\DOC1.txt", "TEXT/a\x00b", "", ".", "../x", "/x"])
def test_qc_unsafe_paths(tmp_path, path):
    dat, opt = volume(tmp_path, [("BEGDOC", "ENDDOC"), ("A1", "A1")], f"A1,V,{path},Y,,,1\n")
    assert ("unsafe_path", 1) in codes(lf.qc_volume(dat, opt))


def test_ranges_duplicates_and_labels(tmp_path):
    dat, opt = volume(tmp_path, [("BEGDOC", "ENDDOC", "TITLE"), ("A001", "A100", "secret-title"),
        ("A010", "A020", ""), ("A030", "A040", ""), ("A030", "A040", ""),
        ("A200", "A199", ""), ("A" + "9" * 5000, "A1", ""), ("SYN١", "SYN2", ""),
        ("PRE-0007-A", "PRE-0007-A", ""), ("A300", "B301", ""), ("A400", "A400", "")], encoding="utf-8")
    report = lf.qc_volume(dat, opt)
    assert {("bates_overlap", 4), ("bates_duplicate", 4), ("bates_duplicate", 5),
            ("reversed_range", 6), ("unparseable_bates", 7), ("unparseable_bates", 8),
            ("unparseable_bates", 9), ("prefix_mismatch", 10)} <= codes(report)
    assert ("bates_missing", 4) not in codes(report)
    assert not any(f.code == "bates_missing" for f in report.findings)
    assert all("secret-title" not in f.message for f in report.findings)


def test_join_family_profile_and_qc_checks(tmp_path):
    dat, opt = volume(tmp_path, [("BEGDOC", "ENDDOC", "BEGATTACH", "ENDATTACH", "TEXT", "DATE", "MD5"),
        ("A1", "A2", "A1", "A3", "text", "invalid", "wrong"),
        ("A3", "A3", "A1", "A3", "text", "2020-01-01", hashlib.md5(b"synthetic").hexdigest()),
        ("A5", "A5", "A4", "A5", "missing", "", "")],
        "A1,V,text,Y,,,3\nA2,V,text,,,,\nA2,V,text,,,,\nA4,V,text,,,,\nA3,V,text,Y,,,1\nA8,V,text,Y,,,1\n")
    (tmp_path / "text").write_bytes(b"synthetic")  # fixture-write: Hashable synthetic file.
    (tmp_path / "orphan").write_bytes(b"synthetic")  # fixture-write: Unreferenced synthetic file.
    report = lf.qc_volume(dat, opt)
    assert report.joined_pages["A1"] == ("A1", "A2", "A2", "A4")
    assert report.families[("A1", "A3")] == ("A1", "A3")
    assert {"unparseable_date", "hash_mismatch", "missing_file", "orphan_file", "orphan_attachment",
            "dat_only_key", "opt_only_key", "opt_duplicate", "page_outside_range", "page_count_mismatch"} <= {f.code for f in report.findings}
    assert all(f.file and f.line > 0 for f in report.findings)
    profile = lf.FieldProfile("synthetic", lf.propose_field_map(("Beg Bates", "TEXT")))
    assert lf.FieldProfile.from_data(profile.to_data()) == profile
    with pytest.raises(ValueError):
        lf.FieldProfile.from_data({"producer": "synthetic", "fields": {"A": "unknown"}})


@pytest.mark.parametrize("mode", ["relative", "symlink"])
def test_orphan_root_identity(tmp_path, monkeypatch, mode):
    root = tmp_path / "volume"
    root.mkdir()
    dat, opt = volume(root, [("BEGDOC", "ENDDOC", "TEXT"), ("A1", "A1", "text")], "A1,V,text,Y,,,1\n")
    (root / "text").write_text("synthetic")  # fixture-write: Referenced synthetic file.
    (root / "alias").symlink_to(root / "text")  # fixture-write: Contained alias.
    (tmp_path / "alias").symlink_to(root, target_is_directory=True)  # fixture-write: Alias for the root.
    monkeypatch.chdir(tmp_path)
    folder = Path("volume" if mode == "relative" else "alias")
    assert not lf.qc_volume(folder / dat.name, folder / opt.name).findings
    dat, opt = volume(root, [("BEGDOC", "ENDDOC", "TEXT"), ("A1", "A1", "subdir")])
    (root / "subdir").mkdir()
    assert ("missing_file", 2) in codes(lf.qc_volume(dat, opt))


def test_raw_bates_and_bounded_field_recovery(tmp_path):
    dat, opt = volume(tmp_path, [("BEGDOC", "ENDDOC", "TI®TLE"), ("A®1", "A®1", "a®b")])
    row = next(parse_dat(dat))
    assert row.headers[-1] == "TI®TLE" and row.values == ("A®1", "A®1", "a\nb")
    with dat.open("ab") as stream:
        stream.write(b"\xfe" + b"x" * (1024 * 1024 + 1) + b"\n" + _dat_bytes([("A2", "A2", "ok")]))  # fixture-write: Oversized unclosed field then recovery row.
    report = lf.qc_volume(dat, opt)
    assert ("record_too_large", 3) in codes(report) and report.document_count == 2


def test_generated_fixture_volumes(tmp_path):
    runpy.run_path(str(Path(__file__).parent / "fixtures/load_files/SYNTH001/generate.py"))["generate"](tmp_path)
    first, second = tmp_path / "SYNTH001", tmp_path / "SYNTH002"
    report = lf.qc_volume(first / "SYNTH001.dat", first / "SYNTH001.opt")
    assert (report.document_count, report.page_count) == (50, 180)
    assert not report.findings and report.families == {("SYN000001", "SYN000008"): ("SYN000001", "SYN000005")}
    expected = [row.values for row in parse_dat(first / "SYNTH001.dat")]
    assert expected[0][:2] == ("SYN000001", "SYN000004")
    assert expected[-1][:2] == ("SYN000178", "SYN000180")
    assert [row[4] for row in expected] == [f"TEXT/DOC{n}.txt" for n in range(1, 51)]
    assert sum(bool(row[5]) for row in expected) == 3
    assert report.joined_pages["SYN000178"] == ("SYN000178", "SYN000179", "SYN000180")
    for encoding in ("cp1252", "utf-8", "utf-16", "utf-16-be"):
        assert [row.values for row in parse_dat(tmp_path / f"{encoding}.dat")] == expected
    report = lf.qc_volume(second / "SYNTH002.dat", second / "SYNTH002.opt")
    assert (report.document_count, report.page_count) == (2, 3)
    assert {"dat_only_key", "opt_only_key", "missing_file", "orphan_file", "unparseable_date",
            "hash_mismatch", "orphan_attachment", "bates_missing"} <= {f.code for f in report.findings}
    assert next(parse_dat(second / "overlay.dat")).values[0] == "SYN000181"


@pytest.mark.timeout(30)
def test_large_volume_linear_budget(tmp_path):
    dat, opt = volume(tmp_path, [("BEGDOC", "ENDDOC")])
    with dat.open("ab") as stream, opt.open("w") as pages:
        for n in range(200000):
            stream.write(_dat_bytes([(f"A{n:06}", f"A{n:06}")]))  # fixture-write: Generated scale DAT.
            pages.write(f"A{n:06},V,text,Y,,,1\n")  # fixture-write: Generated scale OPT.
    (tmp_path / "text").write_bytes(b"synthetic")  # fixture-write: Shared contained page file.
    tracemalloc.start()
    start = time.monotonic()
    try:
        report = lf.qc_volume(dat, opt)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    elapsed = time.monotonic() - start
    print(f"scale: 200000 rows, {elapsed:.2f}s, peak {peak / 1024 / 1024:.2f} MiB")
    assert report.document_count == report.page_count == 200000 and not report.findings
    assert peak < 256 * 1024 * 1024 and elapsed < 25, (peak, elapsed)


@pytest.mark.parametrize("ranges", [
    [("A001", "A100"), ("A010", "A020"), ("A030", "A040"), ("A102", "A102")],
    [("A1", "A1"), ("A03", "A03")],
    [("A003", "A002"), ("A005", "A005")],
    [("A001", "B002"), ("A005", "A005")],
    [("A001", "A001"), ("A010", "A010"), ("A-0005", "A-0005")],
    [("A001", "A001"), ("A010", "A010"), ("A", "A")],
    [("A001", "A001"), ("A010", "A010"), ("", "")],
    [("A001", "A001"), ("A010", "A010"), (" Ｎ／Ａ ", " Ｎ／Ａ ")],
    [("A001", "A001"), ("A010", "A010"), ("A١", "A١")],
    [("A001", "A001"), ("A010", "A010"), ("A003", "A-0005")],
])
def test_gap_parity(tmp_path, ranges):
    from case_intelligence.bates_gaps import BatesDocument, compute_bates_gaps
    dat, opt = volume(tmp_path, [("BEGDOC", "ENDDOC"), *ranges], encoding="utf-8")
    facts = compute_bates_gaps(BatesDocument(str(n), "v", ((a, b),)) for n, (a, b) in enumerate(ranges))
    expected = [(f.kind, f.lower, f.upper) for f in facts if f.kind in {"missing", "possible"}]
    actual = [(f.code.removeprefix("bates_"), *map(int, f.message.split()[-2:]))
              for f in lf.qc_volume(dat, opt).findings if f.code in {"bates_missing", "bates_possible"}]
    assert actual == expected


@pytest.mark.parametrize("data,code,line", [
    (b"A1,V,text,Y,,,1\n\x81,V,p,Y,,,1\nA2,V,text,Y,,,1\n", "mixed_encoding", 2),
    (b"A1,V,text,Y,,,1\nbad\nA2,V,text,Y,,,1\n", "field_count_mismatch", 2),
])
def test_opt_qc_recovers(tmp_path, data, code, line):
    dat, opt = volume(tmp_path, [("BEGDOC", "ENDDOC"), ("A1", "A1"), ("A2", "A2")])
    opt.write_bytes(data)  # fixture-write: Recoverable hostile OPT record.
    report = lf.qc_volume(dat, opt)
    assert (code, line) in codes(report) and report.page_count == 2


@pytest.mark.parametrize("data", [None, {}, {"producer": 3, "fields": {}},
    {"producer": "s", "fields": {"A": "begin_bates", "B": "begin_bates"}}])
def test_profile_rejects_malformed_data(data):
    with pytest.raises(ValueError):
        lf.FieldProfile.from_data(data)


def test_embedded_line_endings_and_mapped_bates_are_exact(tmp_path):
    dat, _ = volume(tmp_path, [("KEY", "END"), ("A\r\n®1", "A\r®1")], encoding="utf-8")
    mapping = {"KEY": "begin_bates", "END": "end_bates"}
    assert next(parse_dat(dat, field_map=mapping)).values == ("A\r\n®1", "A\r®1")


def test_bad_header_is_not_replaced_by_a_data_row(tmp_path):
    dat, opt = volume(tmp_path, [("BEGDOC", "ENDDOC"), ("A1", "A1"), ("A2", "A2")])
    dat.write_bytes(b"\x81\n" + _dat_bytes([("A1", "A1"), ("A2", "A2")]))  # fixture-write: Undecodable synthetic header.
    report = lf.qc_volume(dat, opt)
    assert ("invalid_header", 1) in codes(report)
    assert not report.joined_pages
