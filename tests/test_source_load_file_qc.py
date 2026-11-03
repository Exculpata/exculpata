"""LF1b acceptance items 17 to 29, including both tests named in item 27."""

import gc
import hashlib
import importlib.util
import time
import tracemalloc
from pathlib import Path

import pytest

from case_intelligence import bates_gaps
from case_intelligence.source_load_file_qc import BatesRange, QCReport, parse_plain_label, qc_volume
from case_intelligence.source_load_files import FieldProfile, parse_dat, parse_opt, propose_field_map

_spec = importlib.util.spec_from_file_location(
    "load_file_fixtures", Path(__file__).parent / "fixtures/load_files/generate.py")
fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixtures)


def _qc(root, fields=None):
    if fields is None:
        records = parse_dat(root / "DATA/volume.dat", findings=[])
        try:
            fields = propose_field_map(next(records).headers)
        finally:
            records.close()
    return qc_volume(root, dat_path=root / "DATA/volume.dat", opt_path=root / "DATA/volume.opt",
                     profile=FieldProfile("synthetic", fields))


def _lines(report, code, root, name="volume.dat"):
    return sorted(f.line for f in report.findings
                  if f.code == code and Path(f.file).resolve() == root / "DATA" / name)


@pytest.mark.parametrize("ambiguous,check_date", [(False, False), (False, True), (True, True)])
def test_missing_bates_mapping_is_reported(tmp_path, ambiguous, check_date):
    headers = ("Bates Begin", "Bates Begin" if ambiguous else "Bates End", "DATE")
    root = fixtures.volume(tmp_path, headers, [("A1", "A1", "Jan 5")])
    fields = {"DATE": "date"} if check_date else {}
    if ambiguous:
        fields["Bates Begin"] = "begin_bates"
    report = _qc(root, fields)
    code = "ambiguous_mapping" if ambiguous else "missing_bates_mapping"
    assert _lines(report, code, root) == [1]
    assert report.document_count == 1
    assert report.ranges == ()
    assert _lines(report, "unparseable_date", root) == ([2] if check_date else [])


def test_every_row_counted_or_cited(tmp_path):
    rows = [("A1", "A1"), ("ragged",), "\n", ("x" * (2 * 1024 * 1024), "A2"), ("A3", "A3")]
    root = fixtures.volume(tmp_path, ("BEGDOC", "ENDDOC"), rows)
    report = _qc(root)
    counted = {row.line for row in report.documents}
    cited = set(_lines(report, "field_count_mismatch", root)
                + _lines(report, "record_too_large", root))
    assert counted == {2, 6}
    assert cited == {3, 4, 5}
    assert report.document_count == len(counted)
    assert counted.isdisjoint(cited)
    assert report.document_count + len(cited) == len(rows)


def test_generated_volume_exact(tmp_path):
    root = fixtures.exact_volume(tmp_path)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    report = _qc(root)
    assert isinstance(report, QCReport)
    assert report.document_count == len(report.documents) == 50
    assert report.page_count == len(report.pages) == 180
    assert tuple(report.documents) == tuple(parse_dat(root / "DATA/volume.dat"))
    assert tuple(report.pages) == tuple(parse_opt(root / "DATA/volume.opt"))
    expected_ranges, expected_join = [], {}
    start = 1
    for index in range(50):
        count = 4 if index < 30 else 3
        expected_ranges.append((f"SYN{start:04}", f"SYN{start + count - 1:04}", index + 2, count))
        expected_join[index + 2] = tuple(f"SYN{n:04}" for n in range(start, start + count))
        start += count
    assert all(isinstance(r, BatesRange) and Path(r.file) == root / "DATA/volume.dat"
               for r in report.ranges)
    assert [(r.begin, r.end, r.line, r.page_count) for r in report.ranges] == expected_ranges
    assert {line: tuple(p.values[0] for p in pages)
            for line, pages in report.joined_pages.items()} == expected_join
    assert tuple(p for pages in report.joined_pages.values() for p in pages) == tuple(report.pages)
    assert report.families == (("SYN0001", "SYN0005", "SYN0009"),)
    assert report.findings == ()
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_range_checks(tmp_path):
    rows = [("A-7", "A-7"), ("B1", "C2"), ("D9", "D1"), ("E1", "E1"),
            ("E1", "E1"), ("A1", "A100"), ("A10", "A20"), ("A30", "A40"),
            ("F1", "F2"), ("G1", "G2"), ("H1", "H1")]
    pages = [fixtures.opt_page("F1", count=2), fixtures.opt_page("F3", first=False),
             fixtures.opt_page("G1"), fixtures.opt_page("J1"), fixtures.opt_page("J1")]
    root = fixtures.volume(tmp_path, ("BEGDOC", "ENDDOC"), rows, pages)
    report = _qc(root)
    assert [(r.begin, r.end, r.line) for r in report.ranges] == [
        (*row, index + 2) for index, row in enumerate(rows)]
    for code, lines in {"unparseable_label": [2], "prefix_mismatch": [3],
                        "reversed_range": [4], "duplicate_begin": [5, 6]}.items():
        assert _lines(report, code, root) == lines
    assert {8, 9} <= set(_lines(report, "overlap", root))
    assert _lines(report, "page_outside_range", root, "volume.opt") == [2]
    assert 11 in _lines(report, "page_count_mismatch", root)
    assert _lines(report, "duplicate_opt_key", root, "volume.opt") == [4, 5]
    assert 12 in _lines(report, "dat_only", root)
    assert _lines(report, "opt_only", root, "volume.opt") == [4, 5]


def _gap_volume(tmp_path):
    return fixtures.volume(tmp_path, ("BEGDOC", "ENDDOC"), [("A001", "A002"), ("A005", "A006")],
                           [fixtures.opt_page("A001", count=2), fixtures.opt_page("A002", first=False),
                            fixtures.opt_page("A005", count=2), fixtures.opt_page("A006", first=False)])


def test_no_gap_facts_in_qc(tmp_path):
    report = _qc(_gap_volume(tmp_path))
    assert report.findings == ()
    assert [(r.begin, r.end) for r in report.ranges] == [("A001", "A002"), ("A005", "A006")]


def test_ranges_feed_bates_gaps(tmp_path):
    report = _qc(_gap_volume(tmp_path))
    assert [r.page_count for r in report.ranges] == [2, 2]
    docs = [bates_gaps.BatesDocument(str(r.line), "synthetic-v1", ((r.begin, r.end),),
                                     page_count=r.page_count) for r in report.ranges]
    facts = bates_gaps.compute_bates_gaps(docs)
    missing = [f for f in facts if f.kind == "missing"]
    assert len(missing) == 1
    assert (missing[0].series, missing[0].lower, missing[0].upper) == ("A", 3, 4)
    assert missing[0].cited_ids == (("2", "synthetic-v1"), ("3", "synthetic-v1"))
    assert {f.kind for f in facts} == {"missing", "computed"}


@pytest.mark.parametrize("label,expected", [
    ("A1", ("A", 1, 1)), ("a000", ("a", 0, 3)), ("Ab001", ("Ab", 1, 3)),
    ("Z" + "9" * 18, ("Z", 999999999999999999, 18)), ("A" + "9" * 19, None),
    ("A-7", None), ("A®1", None), (" A1", None), ("A1 ", None), ("A1\n", None),
    ("A１", None), ("A١", None), ("Å1", None), ("A1a", None), ("1", None), ("", None),
])
def test_plain_label_rule_agrees_with_bates_gaps(label, expected):
    assert parse_plain_label(label) == bates_gaps._plain(label) == expected


@pytest.mark.parametrize("column", ["TEXT", "NATIVE"])
@pytest.mark.parametrize("value", [r"..\..\x", r"C:\x", r"\\synthetic\share\x", "/x",
                                   r"\x", "", ".", r"TEXT\\x", "a:b", "escape"])
def test_unsafe_dat_paths_via_qc(tmp_path, column, value):
    root = fixtures.volume(tmp_path / "volume", ("BEGDOC", column), [("A1", value)])
    outside = fixtures.write_file(tmp_path, "outside.txt", b"synthetic")
    (root / "escape").symlink_to(outside)  # fixture-write: Synthetic escape inside tmp_path.
    report = _qc(root)
    assert _lines(report, "unsafe_path", root) == [2]
    assert report.document_count == 1


def test_orphan_scan_limited_to_root(tmp_path, monkeypatch):
    root = fixtures.volume(tmp_path / "volume", ("BEGDOC", "TEXT"), [("A1", r"TEXT\001\D.txt")],
                           [fixtures.opt_page("A1")], {"TEXT/001/D.txt": b"synthetic"})
    fixtures.write_file(root, "orphan.txt", b"synthetic orphan")
    fixtures.write_file(tmp_path, "beside.txt", b"synthetic outside")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)  # fixture-write: Synthetic root alias.
    monkeypatch.chdir(tmp_path)
    for path in (root, Path("volume"), alias):
        report = _qc(path)
        assert [(f.code, Path(f.file).resolve(), f.line) for f in report.findings] == [
            ("orphan_file", root / "orphan.txt", 0)]
        assert [(r.begin, r.end, r.page_count) for r in report.ranges] == [("A1", "A1", 1)]


def test_dates_accepted_formats(tmp_path):
    valid = [date + suffix for date in ("2026-01-05", "20260105", "01/05/2026", "1/5/2026")
             for suffix in ("", " 03:04", " 03:04:05")]
    dates = valid + ["31/01/2026", "2026-13-01", "Jan 5"]
    root = fixtures.volume(tmp_path, ("BEGDOC", "DATE"), [(f"A{i}", d) for i, d in enumerate(dates)])
    report = _qc(root)
    assert _lines(report, "unparseable_date", root) == list(range(len(valid) + 2, len(dates) + 2))
    assert [row.values[1] for row in report.documents] == dates


@pytest.mark.parametrize("algorithm", ["md5", "sha1", "sha256"])
def test_hash_mismatch_cited(tmp_path, algorithm):
    digest = hashlib.new(algorithm, b"synthetic native").hexdigest()
    root = fixtures.volume(tmp_path, ("BEGDOC", "NATIVE", algorithm.upper()),
                           [("A1", "NATIVES/a.bin", digest), ("A2", "NATIVES/b.bin", "0" * len(digest))],
                           files={"NATIVES/a.bin": b"synthetic native", "NATIVES/b.bin": b"synthetic native"})
    assert _lines(_qc(root), "hash_mismatch", root) == [3]


def test_family_and_orphan_attachment(tmp_path):
    rows = [("A1", "A2", "A1", "A4"), ("A3", "A4", "A1", "A4"), ("B2", "B2", "B1", "B2")]
    root = fixtures.volume(tmp_path, ("BEGDOC", "ENDDOC", "BEGATTACH", "ENDATTACH"), rows)
    report = _qc(root)
    assert report.families == (("A1", "A3"),)
    assert _lines(report, "orphan_attachment", root) == [4]


def test_qc_hostile_labels_scale_linearly(tmp_path):
    _qc(fixtures.hostile_volume(tmp_path / "warmup", 30))
    elapsed = []
    for count in (5000, 20000):
        root = fixtures.hostile_volume(tmp_path / str(count), count)
        started = time.perf_counter()
        report = _qc(root)
        elapsed.append(time.perf_counter() - started)
        assert report.document_count == len(report.documents) == count
        assert len([f for f in report.findings if f.code == "unparseable_label"]) == 1000
        summaries = [f for f in report.findings
                     if f.code == "findings_truncated" and f.truncated_code == "unparseable_label"]
        assert len(summaries) == 1
        assert summaries[0].count == (count + 1) // 3 - 1000
    assert max(elapsed) < 15, elapsed
    assert elapsed[1] < 8 * elapsed[0] + 0.5, elapsed


def test_qc_memory_grows_linearly(tmp_path):
    roots = [fixtures.hostile_volume(tmp_path / str(count), count) for count in (5000, 20000)]
    _qc(fixtures.hostile_volume(tmp_path / "warmup", 30))
    peaks = []
    for count, root in zip((5000, 20000), roots):
        gc.collect()
        tracemalloc.start()
        try:
            report = _qc(root)
            peaks.append(tracemalloc.get_traced_memory()[1])
        finally:
            tracemalloc.stop()
        assert report.document_count == len(report.documents) == count
        del report
    assert peaks[0] > 0
    assert peaks[1] < 6 * peaks[0], peaks
