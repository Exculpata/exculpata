"""LF1a acceptance items 1 to 16; synthetic inputs are generated under tmp_path."""

import codecs
import gc
import json
import tracemalloc
from pathlib import Path

import pytest

from case_intelligence.source_load_files import (
    FieldProfile,
    LoadFileError,
    parse_dat,
    parse_opt,
    propose_field_map,
    resolve_volume_path,
)


def _dat_bytes(rows, encoding="utf-8", ending="\n"):
    text = ending.join("\x14".join(f"þ{value}þ" for value in row) for row in rows)
    return (text + ending).encode(encoding)


def _write(tmp_path: Path, data: bytes, name="synthetic.dat") -> Path:
    path = tmp_path / name
    path.write_bytes(data)  # fixture-write: Generated synthetic parser input under tmp_path.
    return path


def _citations(findings):
    return [(finding.code, finding.line) for finding in findings]


def _assert_files(findings, path):
    assert all(finding.file == str(path) for finding in findings)


def test_values_kept_exactly_as_produced(tmp_path):
    headers = (" BEGDOC ", "TiTle", "Trailing ")
    values = (" PRE-000007-A ", "alpha\x14inside\r\nsecond\rthird\nfourth", "  end  ")
    path = _write(tmp_path, _dat_bytes([headers, values], encoding="cp1252"))
    findings = []
    rows = list(parse_dat(path, findings=findings))
    assert len(rows) == 1
    assert rows[0].headers == headers
    assert rows[0].values == values
    assert [value.encode("cp1252") for value in rows[0].values] == [
        value.encode("cp1252") for value in values
    ]
    assert (rows[0].file, rows[0].line) == (str(path), 2)
    assert findings == []


def test_registered_mark_never_converted(tmp_path):
    headers = ("KEY", "Unmapped®")
    path = _write(tmp_path, _dat_bytes([headers, ("A®1", "A®1")]))
    findings = []
    rows = list(parse_dat(path, findings=findings))
    assert [row.values for row in rows] == [("A®1", "A®1")]
    assert rows[0].headers == headers
    assert findings == []


@pytest.mark.parametrize("ending", ["\r\n", "\r"])
def test_encodings_identical(tmp_path, ending):
    expected = [("SYN0001", "café®ÿ "), ("SYN0002", " multi\r\nline ")]
    variants = [
        ("cp1252", b""), ("utf-8", b""), ("utf-8", codecs.BOM_UTF8),
        ("utf-16-le", codecs.BOM_UTF16_LE), ("utf-16-be", codecs.BOM_UTF16_BE),
    ]
    for index, (encoding, bom) in enumerate(variants):
        path = _write(tmp_path, bom + _dat_bytes([("BEGDOC", "TITLE"), *expected],
                                               encoding, ending), f"encoding-{index}.dat")
        findings = []
        rows = list(parse_dat(path, findings=findings))
        assert [row.values for row in rows] == expected
        assert [row.headers for row in rows] == [("BEGDOC", "TITLE")] * 2
        assert [row.line for row in rows] == [2, 3]
        assert findings == []


def test_utf8_char_straddling_sample_boundary(tmp_path):
    prefix = _dat_bytes([("TITLE",)]) + "þ".encode("utf-8")
    value = "x" * (65535 - len(prefix)) + "é"
    payload = prefix + value.encode("utf-8") + "þ\n".encode("utf-8")
    assert payload.index("é".encode("utf-8")) == 65535
    assert payload[65535:65537] == b"\xc3\xa9"
    path = _write(tmp_path, payload)
    findings = []
    assert [row.values for row in parse_dat(path, findings=findings)] == [(value,)]
    assert findings == []


@pytest.mark.parametrize("encoding,bom", [
    ("utf-16-le", b""), ("utf-16-be", b""),
    ("utf-32-le", codecs.BOM_UTF32_LE), ("utf-32-be", codecs.BOM_UTF32_BE),
])
def test_unsupported_encoding_reported(tmp_path, encoding, bom):
    path = _write(tmp_path, bom + _dat_bytes([("KEY",), ("A1",)], encoding))
    findings = []
    assert list(parse_dat(path, findings=findings)) == []
    assert _citations(findings) == [("unsupported_encoding", 1)]
    _assert_files(findings, path)


def test_mixed_encoding_cites_line(tmp_path):
    values = [("x" * 70000,), ("A3",), ("A4",), ("A5",), ("A6",)]
    prefix = _dat_bytes([("KEY",), *values])
    assert len(prefix) > 65536
    path = _write(tmp_path, prefix + b"\xc3\xbe\xff\xc3\xbe\n" + _dat_bytes([("A8",)]))
    findings = []
    rows = list(parse_dat(path, findings=findings))
    assert [row.values for row in rows] == [*values, ("A8",)]
    assert [row.line for row in rows] == [2, 3, 4, 5, 6, 8]
    assert _citations(findings) == [("mixed_encoding", 7)]
    _assert_files(findings, path)


def test_last_row_without_newline_is_kept(tmp_path):
    path = _write(tmp_path, _dat_bytes([("KEY", "TEXT"), ("A1", " exact ")])[:-1])
    findings = []
    rows = list(parse_dat(path, findings=findings))
    assert [row.values for row in rows] == [("A1", " exact ")]
    assert rows[0].line == 2
    assert findings == []


def test_unclosed_quote_at_eof_is_truncated_row(tmp_path):
    path = _write(tmp_path, _dat_bytes([("KEY",), ("A1",)]) + "þunfinished".encode())
    findings = []
    assert [row.values for row in parse_dat(path, findings=findings)] == [("A1",)]
    assert _citations(findings) == [("truncated_row", 3)]
    _assert_files(findings, path)


def test_invalid_header_reports_once(tmp_path):
    values = [(f"A{index}", "synthetic", "extra") for index in range(50)]
    path = _write(tmp_path, b"\x81\n" + _dat_bytes(values, encoding="cp1252"))
    findings = []
    rows = list(parse_dat(path, findings=findings))
    assert [row.values for row in rows] == values
    assert [row.line for row in rows] == list(range(2, 52))
    assert all(row.headers == () for row in rows)
    assert _citations(findings) == [("invalid_header", 1)]
    _assert_files(findings, path)


def test_findings_capped_per_code(tmp_path):
    payload = _dat_bytes([("KEY",)]) + _dat_bytes([("A1", "extra")]) * 5000
    path = _write(tmp_path, payload + "þunfinished".encode())
    findings = []
    assert list(parse_dat(path, findings=findings)) == []
    mismatches = [item for item in findings if item.code == "field_count_mismatch"]
    assert [item.line for item in mismatches] == list(range(2, 1002))
    summaries = [item for item in findings if item.code == "findings_truncated"]
    assert len(summaries) == 1
    assert summaries[0].truncated_code == "field_count_mismatch"
    assert summaries[0].count == 4000
    assert summaries[0].line == 1002
    assert [(item.code, item.line) for item in findings
            if item.code not in {"field_count_mismatch", "findings_truncated"}] == [
        ("truncated_row", 5002)
    ]
    assert len(findings) == 1002
    _assert_files(findings, path)


def test_duplicate_and_empty_headers_reported(tmp_path):
    headers = ("KEY", "KEY", "")
    values = ("A1", "A2", "retained")
    path = _write(tmp_path, _dat_bytes([headers, values]))
    findings = []
    rows = list(parse_dat(path, findings=findings))
    assert len(rows) == 1
    assert rows[0].headers == headers
    assert rows[0].values == values
    assert sorted(_citations(findings)) == [("duplicate_header", 1), ("empty_header", 1)]
    _assert_files(findings, path)


def test_windows_separators_resolve(tmp_path):
    root = tmp_path / "volume"
    folder = root / "TEXT" / "001"
    folder.mkdir(parents=True)  # fixture-write: Synthetic volume layout under tmp_path.
    target = _write(folder, b"synthetic", "DOC1.txt")
    source = _write(root, _dat_bytes([("TEXT",)]))
    assert resolve_volume_path(root, r"TEXT\001\DOC1.txt", str(source), 2) == target.resolve()
    assert resolve_volume_path(root, "TEXT/001/DOC1.txt", str(source), 2) == target.resolve()


@pytest.mark.parametrize("value", [
    r"..\..\x", r"TEXT\..\..\x", r"C:\x", r"\\srv\sh\x", "/etc/x", r"\x",
    "a\x00b", "", ".", r"TEXT\\x", "a:b", "outward-link",
])
def test_volume_path_rejects_escapes(tmp_path, value):
    root = tmp_path / "volume"
    root.mkdir()  # fixture-write: Isolated synthetic volume root under tmp_path.
    source = _write(root, _dat_bytes([("TEXT",)]))
    if value == "outward-link":
        outside = _write(tmp_path, b"synthetic", "outside.txt")
        (root / value).symlink_to(outside)  # fixture-write: Synthetic escape within tmp_path.
    with pytest.raises(LoadFileError) as caught:
        resolve_volume_path(root, value, str(source), 7)
    assert caught.value.code == "unsafe_path"
    assert caught.value.file == str(source)
    assert caught.value.line == 7


def test_opt_rows_kept_exactly(tmp_path):
    expected = [(" PRE-0007-A ", " VOL001 ", r"IMAGES\P1.TIF", "Y", "", "", "001"),
                ("A®1", "VOL001", "IMAGES/P2.TIF", "", "", "", "")]
    payload = "\r\n".join(",".join(row) for row in expected) + "\r\n"
    path = _write(tmp_path, payload.encode("cp1252"), "synthetic.opt")
    findings = []
    rows = list(parse_opt(path, findings=findings))
    assert [row.values for row in rows] == expected
    assert [(row.file, row.line) for row in rows] == [(str(path), 1), (str(path), 2)]
    assert findings == []


def test_opt_bad_rows_cited(tmp_path):
    payload = b"\x81,V,p,Y,,,1\nA1,V,p,Y,,,1\nbad,row\nA2,V,p,,,,\n\n"
    path = _write(tmp_path, payload, "synthetic.opt")
    findings = []
    rows = list(parse_opt(path, findings=findings))
    assert [row.values for row in rows] == [
        ("A1", "V", "p", "Y", "", "", "1"), ("A2", "V", "p", "", "", "", ""),
    ]
    assert [row.line for row in rows] == [2, 4]
    assert _citations(findings) == [
        ("mixed_encoding", 1), ("field_count_mismatch", 3), ("field_count_mismatch", 5),
    ]
    _assert_files(findings, path)


@pytest.mark.parametrize("closed", [True, False])
def test_record_size_cap_multiline_unclosed_quote(tmp_path, closed):
    payload = (_dat_bytes([("KEY",), ("A1",)], encoding="cp1252")
               + b"\xfe\n" + (b"x" * 1023 + b"\n") * 2048)
    if closed:
        payload += b"\xfe\n" + _dat_bytes([("A2",), ("A3",)], encoding="cp1252")
    path = _write(tmp_path, payload)
    findings = []
    rows = list(parse_dat(path, findings=findings))
    assert [row.values for row in rows] == ([("A1",), ("A2",), ("A3",)] if closed else [("A1",)])
    assert [row.line for row in rows] == ([2, 2053, 2054] if closed else [2])
    assert _citations(findings) == [("record_too_large", 3)]
    _assert_files(findings, path)


@pytest.mark.parametrize("bad_line_closes", [False, True])
def test_mixed_encoding_inside_multiline_field_keeps_following_rows(tmp_path, bad_line_closes):
    clean = ("x" * 70000,)
    prefix = _dat_bytes([("KEY",), clean])
    damaged = "þaʹ\n".encode() + b"B\x81C" + ("þ\n" if bad_line_closes else "\ndþ\n").encode()
    path = _write(tmp_path, prefix + damaged + _dat_bytes([("A2",), ("A3",)]))
    findings = []
    rows = list(parse_dat(path, findings=findings))
    assert [row.values for row in rows] == [clean, ("A2",), ("A3",)]
    assert [row.line for row in rows] == ([2, 5, 6] if bad_line_closes else [2, 6, 7])
    assert _citations(findings) == [("mixed_encoding", 3)]
    _assert_files(findings, path)


def test_missing_dat_reports_read_error(tmp_path):
    path = tmp_path / "missing.dat"
    findings = []
    assert list(parse_dat(path, findings=findings)) == []
    assert _citations(findings) == [("read_error", 1)]
    _assert_files(findings, path)


@pytest.mark.parametrize("encoding,bom,bad", [
    ("utf-8", b"", b"\xed\xa0\x80"), ("utf-8", codecs.BOM_UTF8, b"\xed\xa0\x80"),
    ("cp1252", b"", b"\x81"),
    ("utf-16-le", codecs.BOM_UTF16_LE, b"\x00\xd8"),
    ("utf-16-be", codecs.BOM_UTF16_BE, b"\xd8\x00"),
])
def test_local_decoder_recovers_across_chunk_boundary(tmp_path, encoding, bom, bad):
    clean = ("x" * 70000,)
    prefix = bom + _dat_bytes([("KEY",), clean], encoding)
    prefix += "þ".encode(encoding)
    width = len("x".encode(encoding))
    offset = len(bom) if encoding == "utf-8" else 0
    padding = ((8192 - (len(prefix) - offset) % 8192) // width - 1) % (8192 // width)
    prefix += ("x" * padding).encode(encoding)
    assert (len(prefix) - offset) % 8192 == 8192 - width
    path = _write(tmp_path, prefix + bad + "þ\n".encode(encoding) + _dat_bytes([("A2",)], encoding))
    findings = []
    assert [row.values for row in parse_dat(path, findings=findings)] == [clean, ("A2",)]
    assert _citations(findings) == [("mixed_encoding", 3)]


@pytest.mark.parametrize("encoding,bom,bad", [
    ("utf-8", codecs.BOM_UTF8, b"\x81"),
    ("utf-16-le", codecs.BOM_UTF16_LE, b"\x00\xd8"),
    ("utf-16-be", codecs.BOM_UTF16_BE, b"\xd8\x00"),
])
def test_decode_error_in_first_bom_chunk_keeps_header(tmp_path, encoding, bom, bad):
    payload = bom + _dat_bytes([("KEY",)], encoding) + "þ".encode(encoding)
    path = _write(tmp_path, payload + bad + "þ\n".encode(encoding) + _dat_bytes([("A2",)], encoding))
    findings = []
    rows = list(parse_dat(path, findings=findings))
    assert [row.values for row in rows] == [("A2",)]
    assert rows[0].headers == ("KEY",)
    assert rows[0].line == 3
    assert _citations(findings) == [("mixed_encoding", 2)]


@pytest.mark.parametrize("ending", ["\r\n", "\r", "\n"])
def test_oversized_physical_line_keeps_closing_quote_and_following_rows(tmp_path, ending):
    payload = _dat_bytes([("KEY",), ("A1",), ("x" * (2 * 1024 * 1024),), ("A2",)], ending=ending)
    path = _write(tmp_path, payload)
    findings = []
    rows = list(parse_dat(path, findings=findings))
    assert [row.values for row in rows] == [("A1",), ("A2",)]
    assert [row.line for row in rows] == [2, 4]
    assert _citations(findings) == [("record_too_large", 3)]


@pytest.mark.parametrize("wrapped", [False, True])
def test_balanced_inner_thorns_are_exact_or_cited(tmp_path, wrapped):
    value = "Aþ1þ"
    row = _dat_bytes([(value,)]) if wrapped else (value + "\n").encode()
    path = _write(tmp_path, _dat_bytes([("KEY",)]) + row)
    findings = []
    rows = list(parse_dat(path, findings=findings))
    assert ([item.values for item in rows] == [(value,)] and not findings) or (
        not rows and len(findings) == 1 and findings[0].line == 2
    )
    _assert_files(findings, path)


def test_parse_memory_is_flat(tmp_path):
    paths = []
    for count in (5000, 20000):
        path = tmp_path / f"rows-{count}.dat"
        with path.open("wb") as stream:  # fixture-write: Generated synthetic scale input.
            stream.write(_dat_bytes([("KEY", "TITLE")]))  # fixture-write: Synthetic header.
            for index in range(count):
                row = (f"SYN{index:06}", "x" * 128)
                stream.write(_dat_bytes([row]))  # fixture-write: Synthetic fixed-width row.
        paths.append((count, path))
    warmup = _write(tmp_path, _dat_bytes([("KEY",), ("A1",)]))
    assert sum(1 for _ in parse_dat(warmup)) == 1
    peaks = []
    for count, path in paths:
        gc.collect()
        findings = []
        tracemalloc.start()
        try:
            seen = 0
            for row in parse_dat(path, findings=findings):
                assert row.values == (f"SYN{seen:06}", "x" * 128)
                seen += 1
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert seen == count
        assert findings == []
        peaks.append(peak)
    assert peaks[0] > 0
    assert peaks[1] < 1.5 * peaks[0], peaks


def test_field_profile_roundtrip_and_rejects_malformed(tmp_path):
    fields = propose_field_map(("BEGDOC", "ENDDOC", "TEXT", "Unmapped®"))
    assert fields == {"BEGDOC": "begin_bates", "ENDDOC": "end_bates", "TEXT": "text_path"}
    profile = FieldProfile("synthetic", fields)
    data = {"producer": "synthetic", "fields": fields}
    assert profile.to_data() == data
    path = _write(tmp_path, json.dumps(data).encode("utf-8"), "profile.json")
    assert FieldProfile.from_data(json.loads(path.read_text(encoding="utf-8"))) == profile
    for malformed in [
        None, [], {}, {"producer": 3, "fields": {}},
        {"producer": "synthetic", "fields": []},
        {"producer": "synthetic", "fields": {"KEY": "unknown"}},
        {"producer": "synthetic", "fields": {"A": "begin_bates", "B": "begin_bates"}},
        {"producer": "synthetic", "fields": {"A": 7}},
        {"producer": "synthetic", "fields": {7: "begin_bates"}},
    ]:
        with pytest.raises(ValueError):
            FieldProfile.from_data(malformed)


def test_field_profile_hash_and_proposal_are_consistent():
    fields = propose_field_map(("BEGBATES", "BEGDOC", "TEXT", "TEXTPATH"))
    assert fields == {"BEGBATES": "begin_bates", "TEXT": "text_path"}
    profile = FieldProfile("synthetic", fields)
    reversed_profile = FieldProfile("synthetic", dict(reversed(list(fields.items()))))
    assert profile == reversed_profile
    assert hash(profile) == hash(reversed_profile)
    assert {profile: "retained"}[reversed_profile] == "retained"
    fields.clear()
    assert profile.fields == {"BEGBATES": "begin_bates", "TEXT": "text_path"}


def test_parse_dat_rejects_unused_field_map(tmp_path):
    with pytest.raises(TypeError, match="field_map"):
        parse_dat(tmp_path / "unused.dat", field_map={"KEY": "begin_bates"})


@pytest.mark.parametrize("payload,code", [(b"", "invalid_header"), (b"KEY\n" + b"x" * 65536 + b"\0", "unsupported_encoding")])
def test_empty_or_unsupported_dat_is_cited(tmp_path, payload, code):
    path = _write(tmp_path, payload)
    findings = []
    assert list(parse_dat(path, findings=findings)) == []
    assert _citations(findings) == [(code, 1)]
    _assert_files(findings, path)
