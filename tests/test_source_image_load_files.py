"""Generated image-index contracts, provenance and bounded streaming proofs."""
from __future__ import annotations

import codecs
import gc
import hashlib
import json
import os
from pathlib import Path
import tracemalloc

import pytest

from case_intelligence.source_load_files import LoadFileError, OptPage, RecordEvent, parse_opt
from tests.fixtures.load_files.generate_image_indexes import dii_bytes, lfp_bytes, minimal_image_index


def image_api():
    from case_intelligence import source_image_load_files
    return source_image_load_files


def generated(tmp_path, body, name="generated.index"):
    path = tmp_path / name
    path.write_bytes(body)  # fixture-write: Invented parser input inside the temporary test directory.
    return path


def observed(parser, path, **kwargs):
    findings, events = [], []
    rows = list(parser(path, findings=findings, on_event=events.append, **kwargs))
    return rows, findings, events


def test_lfp_explicit_pages(tmp_path):
    api = image_api()
    records = (
        {"key": " PRE-0007-A ", "boundary": "D", "volume": "VOL®",
         "directory": "Generated\\Images", "filename": "café.png", "image_type": "4"},
        {"key": "PRE-0099-Z", "boundary": " ", "volume": "VOL®",
         "directory": "Generated\\Images", "filename": "different.png", "image_type": "4", "rotation": "0"},
        {"key": "DOC-0042", "boundary": "D", "filename": "not-a-label.tif", "image_type": "2", "rotation": "2"},
    )
    path = generated(tmp_path, lfp_bytes(records))
    original = path.read_bytes()
    pages, findings, events = observed(api.parse_lfp, path)
    assert api.OptPage is OptPage and OptPage._fields == ("values", "file", "line")
    assert all(type(page) is OptPage for page in pages)
    assert [page.values for page in pages] == [
        (" PRE-0007-A ", "VOL®", "Generated\\Images/café.png", "Y", "", "", ""),
        ("PRE-0099-Z", "VOL®", "Generated\\Images/different.png", "", "", "", ""),
        ("DOC-0042", "", "not-a-label.tif", "Y", "", "", ""),
    ]
    assert [(page.file, page.line) for page in pages] == [(str(path), 1), (str(path), 2), (str(path), 3)]
    assert findings == [] and len(events) == 3
    assert all(type(event) is RecordEvent and event.kind == "accepted" and event.category == "page" for event in events)
    assert [event.cells for event in events] == [tuple(line.split(",")) for line in original.decode().splitlines()]
    assert [event.headers for event in events] == [
        ("command", "image_key", "boundary", "offset", "location"),
        ("command", "image_key", "boundary", "offset", "location", "rotation"),
        ("command", "image_key", "boundary", "offset", "location", "rotation"),
    ]
    assert "rotation" not in dict(events[0].optional_values)
    assert dict(events[1].optional_values)["rotation"] == "0"
    assert dict(events[2].optional_values)["rotation"] == "2"
    assert [dict(event.optional_values)["boundary"] for event in events] == ["D", " ", "D"]
    assert path.read_bytes() == original


def test_dii_document_metadata_only(tmp_path):
    api = image_api()
    path = generated(tmp_path, dii_bytes())
    original = path.read_bytes()
    documents, findings, events = observed(api.parse_dii, path)
    assert all(type(document) is api.DiiDocument for document in documents)
    assert not any(isinstance(document, OptPage) for document in documents)
    assert [(document.begin_bates, document.page_count, document.observed_image_entries) for document in documents] == [
        ("SYNGR0007", "0002", 2), ("SYNGR0042", None, 1)]
    assert [(document.ordinal, document.line, document.file) for document in documents] == [
        (1, 2, str(path)), (2, 9, str(path))]
    assert findings == []
    assert [(event.kind, event.category, event.source_format, event.ordinal, event.line) for event in events] == [
        ("accepted", "document", "dii", 1, 2), ("accepted", "document", "dii", 2, 9)]
    assert dict(zip(documents[0].headers, documents[0].cells))["@DOCID"] == "separate-alpha"
    assert dict(zip(documents[0].headers, documents[0].cells))["@T"] == "synthetic-alpha"
    assert dict(zip(documents[0].headers, documents[0].cells))["@C PGCOUNT"] == "0002"
    assert not any("page_label" in dict(event.optional_values) for event in events)
    assert not any("begin_attachment" in dict(event.optional_values) for event in events)
    assert path.read_bytes() == original


def test_opt_lfp_page_parity(tmp_path):
    api = image_api()
    expected = [
        ("SYNGR0007", "SYNVOL", "Generated/Images/café.png", "Y", "", "", ""),
        ("SYNGR0099", "SYNVOL", "Generated/Images/blue.png", "", "", "", ""),
        ("SYNGR0042", "", "green.tif", "Y", "", "", ""),
    ]
    lfp = generated(tmp_path, lfp_bytes(), "production.lfp")
    opt = generated(tmp_path, ("\n".join(",".join(row) for row in expected) + "\n").encode(), "production.opt")
    pages, findings, events = observed(api.parse_lfp, lfp)
    controls, opt_findings, opt_events = observed(parse_opt, opt)
    assert [page.values for page in pages] == [page.values for page in controls] == expected
    assert pages != controls and findings == opt_findings == []
    assert [page.values[0] for page in pages] == ["SYNGR0007", "SYNGR0099", "SYNGR0042"]
    assert [event.source_format for event in events] == ["lfp"] * 3
    assert [event.source_format for event in opt_events] == ["opt"] * 3
    assert [dict(event.optional_values).get("rotation") for event in events] == [None, "0", "2"]


def test_opt_lfp_dii_document_parity(tmp_path):
    api = image_api()
    lfp = generated(tmp_path, lfp_bytes(), "documents.lfp")
    opt = generated(tmp_path, (
        "SYNGR0007,SYNVOL,Generated/Images/café.png,Y,,,0002\n"
        "SYNGR0099,SYNVOL,Generated/Images/blue.png,,,,\n"
        "SYNGR0042,,green.tif,Y,,,\n").encode(), "documents.opt")
    dii = generated(tmp_path, dii_bytes(), "documents.dii")
    lfp_pages = list(api.parse_lfp(lfp))
    opt_pages = list(parse_opt(opt))
    documents = list(api.parse_dii(dii))
    assert [page.values[0] for page in lfp_pages if page.values[3] == "Y"] == [
        page.values[0] for page in opt_pages if page.values[3] == "Y"] == [
        document.begin_bates for document in documents] == ["SYNGR0007", "SYNGR0042"]
    assert documents[0].page_count == opt_pages[0].values[6] == "0002"
    assert all(page.values[6] == "" for page in lfp_pages)
    assert [document.observed_image_entries for document in documents] == [2, 1]
    assert [page for page in documents if isinstance(page, OptPage)] == []
    assert [page.values[0] for page in opt_pages] == ["SYNGR0007", "SYNGR0099", "SYNGR0042"]
    mismatch = generated(tmp_path, dii_bytes(({
        "record_id": "independent-record", "begin_bates": "DOC-000007-A", "page_count": "0009",
        "parent_id": "unverified-parent", "images": ("unrelated-0099.png", "unrelated-8000.png")},)), "mismatch.dii")
    rows, findings, events = observed(api.parse_dii, mismatch)
    assert len(rows) == 1 and not isinstance(rows[0], OptPage)
    assert (rows[0].begin_bates, rows[0].page_count, rows[0].observed_image_entries) == ("DOC-000007-A", "0009", 2)
    assert {finding.code for finding in findings} == {"page_count_mismatch", "unsupported_parent_relationship"}
    assert events[0].kind == "accepted" and set(events[0].diagnostics) == {
        "page_count_mismatch", "unsupported_parent_relationship"}
    assert not any(key in {"page_label", "begin_attachment", "end_attachment"}
                   for key, value in events[0].optional_values)


def test_provenance_is_format_specific(tmp_path):
    api = image_api()
    paths = [generated(tmp_path, body, name) for name, body in (
        ("own.opt", b"SYN1,,red.png,Y,,,\n"),
        ("own.lfp", minimal_image_index("lfp")), ("own.dii", minimal_image_index("dii")))]
    originals = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    for source_format, parser, path, line in zip(("opt", "lfp", "dii"),
            (parse_opt, api.parse_lfp, api.parse_dii), paths, (1, 1, 2)):
        rows, findings, events = observed(parser, path)
        assert len(rows) == len(events) == 1 and findings == []
        assert (rows[0].file, rows[0].line) == (str(path), line)
        event = events[0]
        assert (event.ordinal, event.line, event.source_format) == (1, line, source_format)
        assert event.next_cursor is not None
        cursor = json.loads(json.dumps(event.next_cursor))
        assert cursor["source_format"] == source_format and cursor["ordinal"] == 2
        metadata = path.stat()
        assert cursor["source"] == [metadata.st_dev, metadata.st_ino, metadata.st_size,
                                    metadata.st_mtime_ns, metadata.st_ctime_ns]
        assert list(parser(path, cursor=cursor)) == []
        copy = generated(tmp_path, path.read_bytes(), "copied-" + source_format)
        with pytest.raises(LoadFileError) as changed:
            list(parser(copy, cursor=cursor))
        assert changed.value.code == "source_changed"
        invalid = dict(cursor, ordinal=3)
        with pytest.raises(LoadFileError) as corrupt:
            list(parser(path, cursor=invalid))
        assert corrupt.value.code == "invalid_cursor"
    assert [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths] == originals


def test_detection_uses_content(tmp_path):
    api = image_api()
    for source_format, builder in (("lfp", lfp_bytes), ("dii", dii_bytes)):
        body = builder()
        for name in ("one.lfp", "two.DII", "arbitrary-generated-name.lfp"):
            path = generated(tmp_path, body, name)
            detected = api.detect_image_load_file(path, name=name)
            assert (detected.format, detected.outcome) == (source_format, "recognized")
            parser = api.parse_lfp if detected.format == "lfp" else api.parse_dii
            rows, findings, events = observed(parser, path)
            assert len(rows) == (3 if source_format == "lfp" else 2) and findings == []
            assert events[0].ordinal == 1 and events[0].line == (1 if source_format == "lfp" else 2)
        corrupt = generated(tmp_path, body + b"@UNKNOWN opaque\n", "corrupt." + source_format)
        assert api.detect_image_load_file(corrupt).outcome == "recognized"
        rows, findings, events = observed(parser, corrupt)
        assert findings and any(event.kind != "accepted" for event in events)
        assert {event.source_format for event in events} == {source_format}
    for index, payload in enumerate((b"", b"ordinary text mentioning IM, and @FULLTEXT DOC\n",
            b"IM\n", b"@FULLTEXT DOC IM,KEY,D,0,file.png;4\n", b"@FULLTEXT PAGE\n",
            b"<<DOC>>\n@T invented\n", b" " * 65536 + b"IM,KEY,D,0,file.png;4\n",
            b"MZgenerated\n", b"\xff\xfe\x00\x00generated")):
        path = generated(tmp_path, payload, f"unrecognized-{index}.lfp")
        detected = api.detect_image_load_file(path)
        assert detected.outcome in {"unsupported", "malformed", "ambiguous"} and detected.format == ""


@pytest.mark.parametrize("source_format", ["lfp", "dii"])
def test_supported_encoding_parity(tmp_path, source_format):
    api = image_api()
    parser = api.parse_lfp if source_format == "lfp" else api.parse_dii
    builder = lfp_bytes if source_format == "lfp" else dii_bytes
    expected = None
    for number, (encoding, newline) in enumerate((encoding, newline)
            for encoding in ("utf-8", "utf-8-sig", "cp1252", "utf-16-le", "utf-16-be")
            for newline in ("\n", "\r\n", "\r")):
        path = generated(tmp_path, builder(encoding=encoding, newline=newline), f"encoding-{number}")
        rows, findings, events = observed(parser, path)
        actual = ([row.values for row in rows] if source_format == "lfp" else
                  [(row.begin_bates, row.page_count, row.observed_image_entries, row.headers, row.cells) for row in rows])
        if expected is None:
            expected = actual
        assert actual == expected and findings == []
        assert [event.ordinal for event in events] == list(range(1, len(rows) + 1))
        assert all("\ufffd" not in value for event in events for value in event.cells)
    # Put one multibyte character across the decoder's exact recognition sample.
    prefix = (b"IM,SYN1,D,0," if source_format == "lfp" else b"@FULLTEXT DOC\n@T ")
    tail = (b".png;4\n" if source_format == "lfp" else b"\n@BATESBEG SYN1\n@D @I\\\none.png\n")
    body = prefix + b"x" * (65535 - len(prefix)) + "é".encode() + tail
    assert body[65535:65537] == "é".encode()
    path = generated(tmp_path, body, "sample-boundary")
    rows, findings, events = observed(parser, path)
    assert len(rows) == 1 and findings == []
    assert "é" in "".join(events[0].cells)
    for number, (encoding, bom) in enumerate((
            ("utf-16-le", b""), ("utf-16-be", b""),
            ("utf-32-le", codecs.BOM_UTF32_LE), ("utf-32-be", codecs.BOM_UTF32_BE))):
        path = generated(tmp_path, bom + builder().decode().encode(encoding), f"unsupported-{number}")
        rows, findings, events = observed(parser, path)
        assert rows == [] and findings and any(item.code == "unsupported_encoding" for item in findings)
        assert events[-1].kind == "file_failure" and events[-1].next_cursor is None
    damaged_prefix = (b"IM,SYN1,D,0,one.png;4\n" * 4000 if source_format == "lfp" else
                      b"@FULLTEXT DOC\n@T record\n@BATESBEG SYN1\n@D @I\\\n" + b"one.png\n" * 10000)
    assert len(damaged_prefix) > 65536
    damaged = generated(tmp_path, damaged_prefix + b"\xff\n", "late-damage")
    rows, findings, events = observed(parser, damaged)
    assert any(item.code == "mixed_encoding" for item in findings)
    assert any("mixed_encoding" in event.diagnostics for event in events)
    assert all("\ufffd" not in value for event in events for value in event.cells or ())
    assert any(event.kind != "accepted" for event in events)


@pytest.mark.parametrize("source_format", ["lfp", "dii"])
def test_malformed_record_recovery(tmp_path, source_format):
    api = image_api()
    parser = api.parse_lfp if source_format == "lfp" else api.parse_dii
    if source_format == "lfp":
        bad = (
            b"IM,first,D,0,one.png;4\nIM,bad,C,0,two.png;4\n"
            b"IM,also-bad,D,1,two.png;4\nIM,broken,D,0,one.png;4,90\n"
            b'IM,"quoted,key",D,0,one.png;4\nIM,truncated\nIM,survivor,,0,last.png;4\n')
        rows, findings, events = observed(parser, generated(tmp_path, bad))
        assert [row.values[0] for row in rows] == ["first", "survivor"]
        assert [(event.kind, event.ordinal, event.line) for event in events] == [
            ("accepted", 1, 1), ("rejected", 2, 2), ("rejected", 3, 3),
            ("rejected", 4, 4), ("rejected", 5, 5), ("rejected", 6, 6), ("accepted", 7, 7)]
        assert {item.code for item in findings} >= {"lfp_boundary", "lfp_offset", "lfp_rotation", "lfp_field_count"}
    else:
        bad = (b"@FULLTEXT DOC\n@T damaged\n@BATESBEG OLD\n@BATESBEG CONFLICT\n"
               b"@PGCOUNT 0099\n@PARENTID old-parent\n@D @I\\Old\\\none.png\n"
               b"@T survivor\n@BATESBEG NEW\n@D @I\\\nlast.png\n")
        rows, findings, events = observed(parser, generated(tmp_path, bad))
        assert [(row.begin_bates, row.page_count, row.observed_image_entries) for row in rows] == [("NEW", None, 1)]
        assert [(event.kind, event.ordinal, event.line) for event in events] == [("rejected", 1, 2), ("accepted", 2, 9)]
        assert "dii_duplicate" in events[0].diagnostics and events[0].cells is not None
        assert events[1].diagnostics == () and "OLD" not in events[1].cells
        assert "@PARENTID" not in events[1].headers and "@PGCOUNT" not in events[1].headers
        assert dict(zip(events[1].headers, events[1].cells))["@D"] == "@I\\"
        for tail in (b"@UNKNOWN opaque\n", b"<<DOC>>\n", b"@EMAIL-BODY\n@T false-boundary\n"):
            opaque = generated(tmp_path, minimal_image_index("dii") + tail + bad[bad.index(b"@T survivor"):], "opaque")
            rows, findings, events = observed(parser, opaque)
            assert rows == [] and events[-1].kind == "file_failure" and events[-1].next_cursor is None
            assert not any(event.kind == "accepted" for event in events)
        truncated = generated(tmp_path, minimal_image_index("dii") + b"@T unfinished\n@BATESBEG INCOMPLETE", "truncated")
        rows, findings, events = observed(parser, truncated)
        assert len(rows) == 1 and rows[0].begin_bates == "SYN1"
        assert events[-1].kind == "rejected" and "dii_block" in events[-1].diagnostics
    assert findings and all(Path(item.file).parent == tmp_path and item.line > 0 for item in findings)


@pytest.mark.parametrize("source_format", ["lfp", "dii"])
def test_grammar_errors_are_cited_without_successful_reinterpretation(tmp_path, source_format):
    api = image_api()
    parser = api.parse_lfp if source_format == "lfp" else api.parse_dii
    if source_format == "lfp":
        cases = [
            "IM,KEY,D,0\n", "IM,KEY,D,0,one.tif;2,0,extra\n", "OTHER,KEY,D,0,one.tif;2\n",
            "IM,,D,0,one.png;4\n", "IM," + "K" * 71 + ",D,0,one.png;4\n",
            'IM,KEY,D,0,"one;two.png";4\n', "IM,KEY,D,0,@;Images;one.png;4\n",
            "IM,KEY,D,0,@VOL;;one.png;4\n", "IM,KEY,D,0,@VOL;Images;one.png;4;extra\n",
        ]
        cases.extend(f"IM,KEY,{boundary},0,one.png;4\n" for boundary in ("C", "S", "F", "d", "  "))
        cases.extend(f"IM,KEY,D,{offset},one.png;4\n" for offset in ("", "1", "-1", "00", "0.0"))
        cases.extend(f"IM,KEY,D,0,one.png;{kind}\n" for kind in ("", "1", "3", "02", "TIFF"))
        cases.extend(f"IM,KEY,D,0,one.png;4,{rotation}\n" for rotation in ("", "4", "-1", "90", "01"))
    else:
        valid = minimal_image_index("dii").decode()
        cases = [
            valid.replace("@FULLTEXT DOC\n", ""), valid.replace("@FULLTEXT DOC", "@FULLTEXT PAGE"),
            valid.replace("@FULLTEXT DOC\n", "@FULLTEXT DOC\n@FULLTEXT DOC\n"),
            valid.replace("@T SYN1", "@T "), valid.replace("@BATESBEG SYN1\n", ""),
            valid.replace("@D @I\\\n", ""), valid.replace("red.png\n", ""),
            valid.replace("@D @I\\\n", "@D @I\\\n@D @I\\\n"),
            valid.replace("@D @I\\\n", "@D @V\\Images\\\n"),
            valid.replace("@D @I\\\n", "@D @I\\..\\\n"),
            valid.replace("@D @I\\\n", "@D @I\\C:\\Images\\\n"),
            valid.replace("@D @I\\\n", "orphan.png\n@D @I\\\n"),
            valid + "@DOCID late\n", valid.replace("@T SYN1", "<<DOC>>"),
            valid.replace("@D @I\\\n", "@UNKNOWN multiline\n@T apparent\n@D @I\\\n"),
            valid.replace("@D @I\\\n", "@PGCOUNT 1\n@C PGCOUNT 1\n@D @I\\\n"),
        ]
        cases.extend(valid.replace("@D @I\\\n", f"@PGCOUNT {count}\n@D @I\\\n") for count in (
            "", "0", "-1", "+1", "1.0", "１", "9223372036854775808", "00000000000000000001"))
        cases.extend(valid.replace("@D @I\\\n", f"{tag} one\n{tag} two\n@D @I\\\n")
                     for tag in ("@BATESBEG", "@DOCID", "@PARENTID"))
    for index, body in enumerate(cases):
        path = generated(tmp_path, body.encode(), f"malformed-{index}")
        rows, findings, events = observed(parser, path)
        assert rows == [], (index, rows)
        assert findings and events and any(event.kind != "accepted" for event in events)
        assert all(event.source_format == source_format for event in events)
        assert all(item.file == str(path) and item.line > 0 for item in findings)
        with pytest.raises(LoadFileError):
            list(parser(path))


@pytest.mark.parametrize("source_format", ["lfp", "dii"])
def test_record_limits_and_findings_counts(tmp_path, source_format):
    api = image_api()
    parser = api.parse_lfp if source_format == "lfp" else api.parse_dii
    path = tmp_path / "many-rejections"
    with path.open("wb") as stream:  # fixture-write: Incrementally generated rejected logical records.
        if source_format == "dii":
            stream.write(b"@FULLTEXT DOC\n")
        for number in range(2501):
            record = (f"IM,BAD{number:06},C,0,one.png;4\n" if source_format == "lfp" else
                      f"@T BAD{number:06}\n@D @I\\\none.png\n")
            stream.write(record.encode())
        tail = minimal_image_index(source_format)
        stream.write(tail if source_format == "lfp" else tail.removeprefix(b"@FULLTEXT DOC\n"))
    rows, findings, events = observed(parser, path)
    assert len(rows) == 1 and len(events) == 2502
    assert [event.ordinal for event in events] == list(range(1, 2503))
    rejected = events[:-1]
    assert all(event.kind == "rejected" and event.cells is not None for event in rejected)
    assert [event.line for event in rejected] == ([number + 1 for number in range(2501)]
        if source_format == "lfp" else [2 + number * 3 for number in range(2501)])
    assert events[-1].kind == "accepted" and events[-1].ordinal == 2502
    code = "lfp_boundary" if source_format == "lfp" else "dii_block"
    assert all(code in event.diagnostics for event in rejected)
    examples = [finding for finding in findings if finding.code == code]
    summaries = [finding for finding in findings if finding.code == "findings_truncated"]
    assert len(examples) == 1000 and len(summaries) == 1
    assert examples[-1].line == rejected[999].line
    assert (summaries[0].truncated_code, summaries[0].count, summaries[0].line) == (code, 1501, rejected[1000].line)
    assert all(finding.file == str(path) for finding in findings)
    # The same uncapped observer works without the optional legacy examples list.
    observed_count = 0
    def count(event):
        nonlocal observed_count
        observed_count += int(event.kind == "rejected")
    assert sum(1 for row in parser(path, on_event=count)) == 1
    assert observed_count == 2501
    class ObserverFailure(Exception):
        pass
    legacy, interrupted = [], []
    def abort(event):
        interrupted.append(event)
        raise ObserverFailure
    with pytest.raises(ObserverFailure):
        list(parser(path, findings=legacy, on_event=abort))
    assert len(interrupted) == 1 and interrupted[0].kind == "rejected"
    assert legacy == []
    if source_format == "lfp":
        oversized = b"IM,KEY,D,0," + b"x" * (1024 * 1024 + 1) + b".png;4\n"
        tail = minimal_image_index("lfp")
    else:
        oversized = b"@FULLTEXT DOC\n@T " + b"x" * (1024 * 1024 + 1) + b"\n@BATESBEG BAD\n@D @I\\\none.png\n"
        tail = minimal_image_index("dii").removeprefix(b"@FULLTEXT DOC\n")
    oversized_path = generated(tmp_path, oversized + tail, "oversized-line")
    rows, findings, events = observed(parser, oversized_path)
    assert any(finding.code == "record_too_large" for finding in findings)
    assert any("record_too_large" in event.diagnostics for event in events)
    assert not any(event.kind == "accepted" and any(len(value) > 1024 * 1024 for value in event.cells or ()) for event in events)
    assert all("x" * 4096 not in "".join(event.cells or ()) for event in events if event.kind == "accepted")
    if source_format == "dii":
        value = "x" * (300 * 1024)
        body = ("@FULLTEXT DOC\n@T " + value + "\n@BATESBEG " + value + "\n@DOCID " + value
                + "\n@PARENTID " + value + "\n@D @I\\\none.png\n"
                + minimal_image_index("dii").decode().removeprefix("@FULLTEXT DOC\n")).encode()
        rows, findings, events = observed(parser, generated(tmp_path, body, "metadata-overflow"))
        assert [(row.begin_bates, row.observed_image_entries) for row in rows] == [("SYN1", 1)]
        assert events[0].kind == "rejected" and events[0].cells is None
        assert "metadata_too_large" in events[0].diagnostics
        assert any(item.code == "metadata_too_large" for item in findings)


@pytest.mark.parametrize("source_format", ["lfp", "dii"])
def test_referenced_paths_are_inert(tmp_path, monkeypatch, source_format):
    api = image_api()
    parser = api.parse_lfp if source_format == "lfp" else api.parse_dii
    neighbor = generated(tmp_path, b"Generated bytes that no parser may inspect", "neighbor.png")
    link = tmp_path / "link.png"
    link.symlink_to(neighbor)  # fixture-write: Isolated generated reference whose target must remain unread.
    references = ("neighbor.png", "link.png", "../escape.png", "/absolute.png", "C:\\drive.png",
                  "\\\\host\\share.png", "https://example.invalid/image.png", "*.png", "nul\x00.png")
    bodies = [(f"IM,SYN1,D,0,{reference};4\n" if source_format == "lfp" else
               f"@FULLTEXT DOC\n@T record\n@BATESBEG SYN1\n@D @I\\\n{reference}\n").encode()
              for reference in references]
    paths = [generated(tmp_path, body, f"reference-{index}") for index, body in enumerate(bodies)]
    calls = []
    original_open, original_stat = Path.open, Path.stat
    original_new = Path.__new__
    original_os_open, original_os_stat = os.open, os.stat
    allowed = {str(path) for path in paths}
    def check(path, operation):
        if isinstance(path, int):
            return
        value = os.fspath(path)
        calls.append((operation, value))
        assert value in allowed, f"Referenced data triggered {operation}: {value}"
    def checked_open(path, *args, **kwargs):
        check(path, "open")
        return original_open(path, *args, **kwargs)
    def checked_stat(path, *args, **kwargs):
        check(path, "stat")
        return original_stat(path, *args, **kwargs)
    def checked_os_open(path, *args, **kwargs):
        check(path, "os.open")
        return original_os_open(path, *args, **kwargs)
    def checked_os_stat(path, *args, **kwargs):
        check(path, "os.stat")
        return original_os_stat(path, *args, **kwargs)
    def checked_path(cls, *parts):
        assert len(parts) == 1, "Embedded references must not construct filesystem paths"
        check(parts[0], "Path")
        return original_new(cls, *parts)
    def forbidden(*args, **kwargs):
        raise AssertionError("The parser must not resolve, join, glob or connect supplied references")
    with monkeypatch.context() as patch:
        # Path construction interns unique names on supported Python versions;
        # references must remain lexical strings even without filesystem I/O.
        patch.setattr(Path, "__new__", checked_path)
        patch.setattr(Path, "open", checked_open)
        patch.setattr(Path, "stat", checked_stat)
        patch.setattr(os, "open", checked_os_open)
        patch.setattr(os, "stat", checked_os_stat)
        patch.setattr(Path, "resolve", forbidden)
        patch.setattr(Path, "glob", forbidden)
        patch.setattr(Path, "rglob", forbidden)
        patch.setattr(Path, "joinpath", forbidden)
        import socket
        patch.setattr(socket, "create_connection", forbidden)
        for number, path in enumerate(paths):
            rows, findings, events = observed(parser, path)
            if number < 2:
                assert len(rows) == 1 and findings == []
            else:
                assert rows == [] and findings and any(event.kind != "accepted" for event in events)
    assert calls and neighbor.read_bytes() == b"Generated bytes that no parser may inspect"


@pytest.mark.parametrize("source_format", ["lfp", "dii"])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le", "cp1252"])
@pytest.mark.parametrize("terminated", [False, True], ids=["final-no-newline", "final-newline"])
def test_resume_keeps_complete_logical_boundaries(tmp_path, monkeypatch, source_format, encoding, terminated):
    api = image_api()
    parser = api.parse_lfp if source_format == "lfp" else api.parse_dii
    body = (lfp_bytes(encoding=encoding) if source_format == "lfp" else dii_bytes(encoding=encoding))
    if not terminated:
        body = body[:-2] if encoding == "utf-16-le" else body[:-1]
    path = generated(tmp_path, body)
    expected, findings, events = observed(parser, path)
    assert findings == [] and len(events) >= 2
    cursor = json.loads(json.dumps(events[0].next_cursor))
    assert cursor["ordinal"] == 2
    opened = []
    original_open = Path.open
    class SeekObserved:
        def __init__(self, stream):
            self.stream, self.first_read, self.seeks = stream, None, []
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def seek(self, position, *args):
            self.seeks.append(position)
            return self.stream.seek(position, *args)
        def readline(self, *args):
            if self.first_read is None:
                self.first_read = self.stream.tell()
            return self.stream.readline(*args)
        def read(self, *args):
            if self.first_read is None:
                self.first_read = self.stream.tell()
            return self.stream.read(*args)
    def tracked_open(source, *args, **kwargs):
        stream = original_open(source, *args, **kwargs)
        if source == path:
            stream = SeekObserved(stream)
            opened.append(stream)
        return stream
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", tracked_open)
        resumed, resumed_findings, resumed_events = observed(parser, path, cursor=cursor)
    assert resumed == expected[1:] and resumed_findings == []
    assert [(event.ordinal, event.line) for event in resumed_events] == [
        (event.ordinal, event.line) for event in events[1:]]
    readers = [stream for stream in opened if stream.first_read is not None]
    assert readers and all(stream.first_read == cursor["cookie"] for stream in readers)
    assert any(cursor["cookie"] in stream.seeks for stream in readers)
    # Observer failure precedes legacy diagnostics and stops without reading on.
    class Pause(Exception):
        pass
    seen, legacy = [], []
    def stop(event):
        seen.append(event)
        raise Pause
    with pytest.raises(Pause):
        list(parser(path, findings=legacy, on_event=stop))
    assert len(seen) == 1 and legacy == [] and seen[0].next_cursor == events[0].next_cursor
    changed = body.replace(b"SYN", b"ALT", 1) if encoding != "utf-16-le" else body + b"\n\x00"
    path.write_bytes(changed)  # fixture-write: Generated source-version mutation after a saved cursor.
    with pytest.raises(LoadFileError) as stale:
        list(parser(path, cursor=cursor))
    assert stale.value.code == "source_changed"


@pytest.mark.parametrize("source_format", ["lfp", "dii"])
def test_source_change_during_consumption_is_terminal(tmp_path, source_format):
    api = image_api()
    parser = api.parse_lfp if source_format == "lfp" else api.parse_dii
    body = lfp_bytes() if source_format == "lfp" else dii_bytes()
    changed = body.replace(b"SYNGR0042", b"ALTER0042")
    assert changed != body and len(changed) == len(body)
    path = generated(tmp_path, body)
    findings, events = [], []
    def mutate_after_first(event):
        events.append(event)
        if len(events) == 1:
            path.write_bytes(changed)  # fixture-write: Same-length generated mutation during the real stream.
    list(parser(path, findings=findings, on_event=mutate_after_first))
    assert events[-1].kind == "file_failure" and events[-1].next_cursor is None
    assert "source_changed" in events[-1].diagnostics
    assert any(finding.code == "source_changed" for finding in findings)


def memory_profile(parser, path, check):
    gc.collect()
    findings = []
    seen = 0
    tracemalloc.start()
    try:
        for row in parser(path, findings=findings):
            check(row, seen)
            seen += 1
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert findings == []
    return seen, peak


def test_lfp_memory_is_flat(tmp_path, record_property):
    api = image_api()
    paths = []
    for count in (5000, 20000):
        path = tmp_path / f"lfp-{count}"
        with path.open("wb") as stream:  # fixture-write: Fixed-width generated records before measurement.
            for number in range(count):
                stream.write(f"IM,SYN{number:06},D,0,@VOL;Images;page-{number:06}.png;4,0\n".encode())
        paths.append((count, path))
    warm = generated(tmp_path, minimal_image_index("lfp"), "warm-lfp")
    assert sum(1 for row in api.parse_lfp(warm)) == 1
    def check(page, number):
        assert type(page) is OptPage
        assert page.values == (f"SYN{number:06}", "VOL", f"Images/page-{number:06}.png", "Y", "", "", "")
        assert page.line == number + 1
    peaks = []
    for count, path in paths:
        seen, peak = memory_profile(api.parse_lfp, path, check)
        assert seen == count
        peaks.append(peak)
        record_property(f"python_peak_{count}", peak)
    assert peaks[0] > 0
    assert peaks[1] < 1.5 * peaks[0], peaks


def test_dii_memory_is_flat(tmp_path, record_property):
    api = image_api()
    paths = []
    for count in (5000, 20000):
        path = tmp_path / f"dii-{count}"
        with path.open("wb") as stream:  # fixture-write: Fixed-width generated blocks before measurement.
            stream.write(b"@FULLTEXT DOC\n")
            for number in range(count):
                stream.write((f"@T generated-{number:06}\n@BATESBEG SYN{number:06}\n@PGCOUNT 0001\n"
                              f"@D @I\\Images\\\npage-{number:06}.png\n").encode())
        paths.append((count, path))
    warm = generated(tmp_path, minimal_image_index("dii"), "warm-dii")
    assert sum(1 for row in api.parse_dii(warm)) == 1
    def check(document, number):
        assert type(document) is api.DiiDocument and not isinstance(document, OptPage)
        assert (document.begin_bates, document.page_count, document.observed_image_entries) == (f"SYN{number:06}", "0001", 1)
        assert (document.ordinal, document.line) == (number + 1, 2 + number * 5)
    peaks = []
    for count, path in paths:
        seen, peak = memory_profile(api.parse_dii, path, check)
        assert seen == count
        peaks.append(peak)
        record_property(f"python_peak_{count}", peak)
    assert peaks[0] > 0
    assert peaks[1] < 1.5 * peaks[0], peaks


def test_dii_large_document_memory_is_flat(tmp_path, record_property):
    api = image_api()
    paths = []
    for count in (5000, 20000):
        path = tmp_path / f"dii-one-{count}"
        with path.open("wb") as stream:  # fixture-write: Real generated entries, not an expanded count token.
            stream.write(f"@FULLTEXT DOC\n@T one-generated-record\n@BATESBEG ONLY0007\n@PGCOUNT {count:06}\n@D @I\\Images\\\n".encode())
            for number in range(count):
                stream.write((f"unrelated-{number:06}-" + "x" * 100 + ".png\n").encode())
        paths.append((count, path))
    assert paths[-1][1].stat().st_size > 1024 * 1024
    warm = generated(tmp_path, minimal_image_index("dii"), "warm-one-dii")
    assert sum(1 for row in api.parse_dii(warm)) == 1
    peaks = []
    for count, path in paths:
        def check(document, number):
            assert number == 0 and type(document) is api.DiiDocument and not isinstance(document, OptPage)
            assert (document.begin_bates, document.page_count, document.observed_image_entries) == ("ONLY0007", f"{count:06}", count)
            assert (document.ordinal, document.line) == (1, 2)
        seen, peak = memory_profile(api.parse_dii, path, check)
        assert seen == 1
        peaks.append(peak)
        record_property(f"python_peak_{count}", peak)
    assert peaks[0] > 0
    assert peaks[1] < 1.5 * peaks[0], peaks
