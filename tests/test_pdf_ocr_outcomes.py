"""Synthetic regressions for bounded OCR outcomes and preserved native text."""

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import io
import subprocess
from types import SimpleNamespace

import pytest

from case_intelligence import pilot_uploads
from case_intelligence.pilot_uploads import PdfOcrResult, PilotStore
from case_intelligence.review_bench_v2 import PdfPage
from case_intelligence.exact_search_results import search_documents


def ingest(tmp_path, monkeypatch, pages, recognize, *, mode="expanded", limit="500"):
    monkeypatch.setenv("CASE_INTELLIGENCE_OCR_MODE", mode)
    monkeypatch.setenv("CASE_INTELLIGENCE_MAX_OCR_PAGES", limit)
    monkeypatch.setattr("case_intelligence.review_bench_v2.extract_pdf_pages", lambda _: pages)
    monkeypatch.setattr(pilot_uploads, "_ocr_pdf_page", lambda source, page, **_: recognize(source, page))
    store = PilotStore(tmp_path / "store")
    document, _ = store.store_stream("synthetic.pdf", "application/pdf", io.BytesIO(b"%PDF-synthetic-outcomes"))
    return store, document


def test_shorter_ocr_adds_body_without_losing_long_native_stamp(tmp_path, monkeypatch):
    native = "Synthetic exhibit stamp received by the fictional review desk reference STAMPONLY"
    _, document = ingest(tmp_path, monkeypatch, [PdfPage(1, native, 0.8, True)],
                         lambda *_: PdfOcrResult("BODYONLY", "recognized"))
    text = document.parsed_units()[0].text
    assert text == native + "\n\nBODYONLY"
    for term in ("STAMPONLY", "BODYONLY"):
        result = search_documents([document], term, scope=("synthetic", "", ""))
        assert result.total == 1
        assert result.items[0].document_id == document.document_id
    assert "Complete page reading is not established" in document.message


def test_good_existing_text_does_not_duplicate_ocr(tmp_path, monkeypatch):
    native = "Synthetic orchard evidence\nSecond native line."
    _, document = ingest(tmp_path, monkeypatch, [PdfPage(1, native, 1.0, True)],
                         lambda *_: PdfOcrResult("Synthetic orchard evidence\nSecond native line.", "recognized"))
    assert document.parsed_units()[0].text == native


@pytest.mark.parametrize("status,notice", [
    ("timed_out", "OCR timed out: 1"),
    ("failed", "OCR failed: 1"),
    ("no_text", "OCR returned no text: 1"),
    ("output_limit", "OCR text/raster limit reached: 1"),
])
def test_failed_ocr_retains_native_text_and_durable_outcome(tmp_path, monkeypatch, status, notice):
    native = "Long synthetic stamp that must survive STAMPONLY"
    store, document = ingest(tmp_path, monkeypatch, [PdfPage(1, native, 0.8, True)],
                              lambda *_: PdfOcrResult(status=status))
    assert document.state == "ready"
    assert document.parsed_units()[0].text == native
    assert notice in document.message
    assert "OCR attempted on 1" in document.message
    restarted = PilotStore(store.root).get(document.document_id)
    assert restarted.message == document.message
    assert restarted.parsed_units() == document.parsed_units()
    assert restarted.version_id == document.version_id
    assert restarted.digest == document.digest


def test_cap_and_disabled_are_skipped_not_unreadable_after_ocr(tmp_path, monkeypatch):
    pages = [PdfPage(n, "Long synthetic stamp of received exhibit", 0.9, True) for n in range(1, 4)]
    calls = []
    _, doc = ingest(tmp_path, monkeypatch, pages,
                    lambda _, n: calls.append(n) or PdfOcrResult("body", "recognized"), limit="1")
    assert calls == [1]
    assert "OCR skipped (page limit): 2" in doc.message
    assert "unreadable after OCR" not in doc.message
    assert doc.page_count == 3


@pytest.mark.parametrize("mode,notice", [("off", "disabled"), ("selective", "selective mode")])
def test_disabled_and_selective_stamped_scans_report_skipped(tmp_path, monkeypatch, mode, notice):
    def unexpected(*_):
        raise AssertionError("OCR should not run")
    _, doc = ingest(tmp_path, monkeypatch, [PdfPage(1, "Long synthetic stamped text only", 0.8, True)],
                    unexpected, mode=mode)
    assert f"OCR skipped ({notice}): 1" in doc.message
    assert "OCR attempted on 0" in doc.message


def test_combined_text_budget_retains_native_and_records_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot_uploads, "MAX_PDF_TOTAL_CHARS", 60)
    native = "Native synthetic evidence " * 2
    _, doc = ingest(tmp_path, monkeypatch, [PdfPage(1, native, 1.0, True)],
                    lambda *_: PdfOcrResult("This OCR output would exceed the total budget.", "recognized"))
    assert doc.parsed_units()[0].text == native.strip()
    assert "OCR text/raster limit reached: 1" in doc.message


def test_empty_timed_out_pages_keep_total_and_reason(tmp_path, monkeypatch):
    _, doc = ingest(tmp_path, monkeypatch, [PdfPage(1, "", 1.0, True), PdfPage(2, "", 1.0, True)],
                    lambda *_: PdfOcrResult(status="timed_out"), limit="1")
    assert doc.state == "needs_ocr" and doc.page_count == 2
    assert "0 of 2 pages with searchable text" in doc.message
    assert "OCR timed out: 1" in doc.message
    assert "OCR skipped (page limit): 1" in doc.message


def test_ocr_subprocess_timeout_is_distinguished(monkeypatch, tmp_path):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 20)
    monkeypatch.setattr(pilot_uploads.subprocess, "run", timeout)
    assert pilot_uploads._ocr_pdf_page(tmp_path / "synthetic.pdf", 1).status == "timed_out"


_RASTER = b"P5\n3 2\n255\n" + bytes(range(6))


def _fake_ocr(monkeypatch, readings, *, raster=_RASTER):
    """Replace the executables; each Tesseract run writes the next queued reading.

    A reading is (text, [(word, confidence), ...]) or an exception to raise.
    """
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0].endswith("pdftoppm"):
            return SimpleNamespace(returncode=0, stdout=raster)
        reading = readings.pop(0)
        if isinstance(reading, BaseException):
            raise reading
        text, words = reading
        rows = ["level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                "1\t1\t0\t0\t0\t0\t0\t0\t9\t9\t-1\t"]
        rows += [] if words is None else [f"5\t1\t1\t1\t1\t{n}\t0\t0\t9\t9\t{conf}\t{word}" for n, (word, conf) in enumerate(words, 1)]
        ocr_command = command[command.index("/usr/bin/tesseract"):]
        base = ocr_command[2]
        with open(base + ".txt", "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        if words is not None:
            with open(base + ".tsv", "w", encoding="utf-8") as handle:
                handle.write("\n".join(rows) + "\n")
        return SimpleNamespace(
            returncode=0,
            stdout=f"{LEGACY_STEM}-ocr-limit:{pilot_uploads.MAX_OCR_TSV_BYTES}\n".encode("ascii"),
        )

    monkeypatch.setattr(pilot_uploads.subprocess, "run", run)
    return calls


def _tesseract_calls(calls):
    return [(command[command.index("/usr/bin/tesseract"):], kwargs)
            for command, kwargs in calls if "/usr/bin/tesseract" in command]


def test_ocr_uses_first_layout_result_not_longest(monkeypatch, tmp_path):
    calls = _fake_ocr(monkeypatch, [("short body", [("short", 96), ("body", 95)])])
    result = pilot_uploads._ocr_pdf_page(tmp_path / "synthetic.pdf", 1)
    assert result == PdfOcrResult("short body", "recognized")
    (command, _), = _tesseract_calls(calls)
    assert command[command.index("--psm") + 1] == "1"


def test_empty_layout_result_falls_back_to_block_mode(monkeypatch, tmp_path):
    calls = _fake_ocr(monkeypatch, [("", []), ("block body", [("block", 90), ("body", 91)])])
    assert pilot_uploads._ocr_pdf_page(tmp_path / "synthetic.pdf", 1) == PdfOcrResult("block body", "recognized")
    assert [c[c.index("--psm") + 1] for c, _ in _tesseract_calls(calls)] == ["1", "6"]


def test_confident_reading_does_not_retry_orientations(monkeypatch, tmp_path):
    # One noisy word in five stays below the retry threshold.
    words = [("upright", 95), ("body", 93), ("noise", 20), ("text", 90), ("here", 91)]
    calls = _fake_ocr(monkeypatch, [("upright body noise text here", words)])
    result = pilot_uploads._ocr_pdf_page(tmp_path / "synthetic.pdf", 1)
    assert result == PdfOcrResult("upright body noise text here", "recognized")
    assert len(_tesseract_calls(calls)) == 1


def test_misoriented_reading_selects_confident_rotation_not_concatenation(monkeypatch, tmp_path):
    calls = _fake_ocr(monkeypatch, [
        ("ssauj wopjeq", [("ssauj", 21), ("wopjeq", 18)]),
        ("rotated body", [("rotated", 94), ("body", 96)]),
        ("", []), ("", []),
    ])
    result = pilot_uploads._ocr_pdf_page(tmp_path / "synthetic.pdf", 1)
    assert result == PdfOcrResult("rotated body", "recognized")
    tesseract = _tesseract_calls(calls)
    # Later readings cannot overwrite the best completed reading with less evidence.
    assert len(tesseract) == 4
    assert tesseract[1][1]["input"] == b"P5\n3 2\n255\n" + bytes(reversed(range(6)))
    assert tesseract[1][1]["timeout"] <= pilot_uploads.OCR_TIMEOUT_SECONDS


def test_longer_low_confidence_rotation_is_not_selected(monkeypatch, tmp_path):
    garbled = [("Ja", 30), ("MOJIM", 25), ("ledger", 85)]
    longer = [(f"w{n}", 40) for n in range(40)]
    calls = _fake_ocr(monkeypatch, [("Ja MOJIM ledger", garbled)] + [(" ".join(w for w, _ in longer), longer)] * 3)
    result = pilot_uploads._ocr_pdf_page(tmp_path / "synthetic.pdf", 1)
    assert result == PdfOcrResult("Ja MOJIM ledger", "recognized")
    assert len(_tesseract_calls(calls)) == 4


def test_upright_native_stamp_words_do_not_outvote_rotated_body(monkeypatch, tmp_path):
    native = "SYNTHETIC STAMP RECEIVED FOR REVIEW DESK ONLY."
    stamp = [(word, 95) for word in native.split()]
    # Two garbled body words among seven confident stamp words would be below
    # the retry share if the already-native stamp counted as OCR evidence.
    calls = _fake_ocr(monkeypatch, [
        (native + "\nJa MOJIM", stamp + [("Ja", 20), ("MOJIM", 15)]),
        ("amber ledger\nGEVIECEY dWVLS", [("amber", 95), ("ledger", 93), ("GEVIECEY", 19), ("dWVLS", 12)]),
        ("", []), ("", []),
    ])
    result = pilot_uploads._ocr_pdf_page(tmp_path / "synthetic.pdf", 1, native_text=native)
    assert len(_tesseract_calls(calls)) == 4
    assert result == PdfOcrResult("amber ledger\nGEVIECEY dWVLS", "recognized")


@pytest.mark.parametrize("native", [
    "SYNTHETIC/STAMP:RECEIVED-FOR.REVIEW_DESK|ONLY",
    "SYNTHETICSTAMPRECEIVEDFORREVIEWDESKONLY",
])
def test_joined_native_stamp_does_not_hide_low_confidence_body(monkeypatch, tmp_path, native):
    stamp = [(word, 95) for word in "SYNTHETIC STAMP RECEIVED FOR REVIEW DESK ONLY".split()]
    calls = _fake_ocr(monkeypatch, [
        ("SYNTHETIC STAMP RECEIVED FOR REVIEW DESK ONLY Ja MOJIM", stamp + [("Ja", 20), ("MOJIM", 15)]),
        ("SYNTHETIC STAMP RECEIVED FOR REVIEW DESK ONLY", stamp),
        ("amber ledger", [("amber", 95), ("ledger", 95)]),
        ("", []),
    ])
    assert pilot_uploads._ocr_pdf_page(tmp_path / "synthetic.pdf", 1, native_text=native) == PdfOcrResult("amber ledger", "recognized")
    assert len(_tesseract_calls(calls)) == 4


def test_joined_ocr_stamp_does_not_hide_low_confidence_body(monkeypatch, tmp_path):
    native = "SYNTHETIC STAMP RECEIVED FOR REVIEW DESK ONLY TODAY"
    stamp = [(word, 95) for word in "SYNTHETICSTAMP RECEIVEDFOR REVIEWDESK ONLYTODAY".split()]
    calls = _fake_ocr(monkeypatch, [
        ("SYNTHETICSTAMP RECEIVEDFOR REVIEWDESK ONLYTODAY MOJIM", stamp + [("MOJIM", 15)]),
        ("SYNTHETICSTAMP RECEIVEDFOR REVIEWDESK ONLYTODAY", stamp),
        ("amber ledger", [("amber", 95), ("ledger", 95)]),
        ("", []),
    ])
    result = pilot_uploads._ocr_pdf_page(tmp_path / "synthetic.pdf", 1, native_text=native)
    assert result == PdfOcrResult("amber ledger", "recognized")
    assert len(_tesseract_calls(calls)) == 4


def test_small_confident_fragment_does_not_preempt_later_body(monkeypatch, tmp_path):
    calls = _fake_ocr(monkeypatch, [
        ("Ja MOJIM", [("Ja", 20), ("MOJIM", 15)]),
        ("fragment", [("fragment", 95)]),
        ("amber ledger original body", [(word, 95) for word in "amber ledger original body".split()]),
        ("", []),
    ])
    result = pilot_uploads._ocr_pdf_page(tmp_path / "synthetic.pdf", 1)
    assert result == PdfOcrResult("amber ledger original body", "recognized")
    assert len(_tesseract_calls(calls)) == 4


@pytest.mark.parametrize(("native", "words", "expected"), [
    ("RECEIVEDFORREVIEW", ["FOR"], ["for"]),
    ("RECEIVEDFORREVIEW", ["RECEIVED", "FOR"], ["received", "for"]),
    ("SCARLET", ["CAR"], ["car"]),
    ("ledger", ["ledger", "ledger"], ["ledger"]),
    ("RECEIVEDFORREVIEW", ["RECEIVED", "FOR", "REVIEW", "FOR"], ["for"]),
    ("RECEIVED FOR REVIEW", ["RECEIVEDFORREVIEW"], []),
    ("RECEIVED FOR REVIEW", ["FORRECEIVED"], ["forreceived"]),
    ("RECEIVED FOR REVIEW", ["RECEIVEDREVIEW"], ["receivedreview"]),
    ("RECEIVED FOR REVIEW", ["RECEIVEDFOR", "RECEIVEDFOR"], ["receivedfor"]),
    ("RECEIVED FOR REVIEW RECEIVED FOR", ["RECEIVEDFOR", "RECEIVEDFOR"], []),
    ("RECEIVED FOR REVIEW", ["FOR", "RECEIVEDFOR"], ["receivedfor"]),
    ("RECEIVED FOR REVIEW", ["RECEIVEDFOR", "FOR"], ["for"]),
    ("RECEIVED GAP FOR", ["GAP", "RECEIVEDFOR"], ["receivedfor"]),
    ("SCAR LET", ["CARLET"], ["carlet"]),
    ("AB A B", ["AB", "AB", "AB"], ["ab"]),
    ("A B AB", ["AB", "A", "B"], []),
    ("A B C A B", ["AB", "BC", "AB"], ["bc"]),
])
def test_native_discount_requires_complete_available_occurrences(native, words, expected):
    reading = pilot_uploads._OcrReading(" ".join(words), tuple((word, 95) for word in words))
    added = pilot_uploads._new_ocr_words(reading, pilot_uploads._ocr_word_keys(native))
    assert [word for word, _ in added] == expected


@pytest.mark.parametrize("terms,discounted", [(12, True), (13, False)])
def test_native_join_matching_has_a_term_limit_in_both_directions(terms, discounted):
    separate = ["a"] * terms
    joined = "".join(separate)
    for native, words in ((separate, [joined]), ([joined], separate)):
        reading = pilot_uploads._OcrReading(" ".join(words), tuple((word, 95) for word in words))
        added = pilot_uploads._new_ocr_words(reading, native)
        assert [word for word, _ in added] == ([] if discounted else words)


@pytest.mark.parametrize("characters,discounted", [(256, True), (257, False)])
def test_native_join_matching_has_a_character_limit_in_both_directions(characters, discounted):
    separate = ["a" * 128, "b" * (characters - 128)]
    joined = "".join(separate)
    for native, words in ((separate, [joined]), ([joined], separate)):
        reading = pilot_uploads._OcrReading(" ".join(words), tuple((word, 95) for word in words))
        added = pilot_uploads._new_ocr_words(reading, native)
        assert [word for word, _ in added] == ([] if discounted else words)


def test_failed_later_orientation_keeps_best_completed_reading(monkeypatch, tmp_path):
    calls = _fake_ocr(monkeypatch, [
        ("Ja MOJIM", [("Ja", 20), ("MOJIM", 15)]),
        ("amber ledger", [("amber", 95), ("ledger", 95)]),
        subprocess.TimeoutExpired("tesseract", 1),
    ])
    assert pilot_uploads._ocr_pdf_page(tmp_path / "synthetic.pdf", 1) == PdfOcrResult("amber ledger", "recognized")
    assert len(_tesseract_calls(calls)) == 3


@pytest.mark.parametrize("failure", [
    subprocess.TimeoutExpired("tesseract", 1),
    OSError("synthetic executable failure"),
])
def test_orientation_retry_failure_keeps_first_reading(monkeypatch, tmp_path, failure):
    calls = _fake_ocr(monkeypatch, [("Ja MOJIM", [("Ja", 20), ("MOJIM", 15)]), failure])
    result = pilot_uploads._ocr_pdf_page(tmp_path / "synthetic.pdf", 1)
    assert result == PdfOcrResult("Ja MOJIM", "recognized")
    assert len(_tesseract_calls(calls)) == 2


def test_native_only_initial_reading_retries_for_image_body(monkeypatch, tmp_path):
    calls = _fake_ocr(monkeypatch, [
        ("RECEIVED", [("RECEIVED", 95)]),
        ("RECEIVED", [("RECEIVED", 95)]),
        ("amber ledger", [("amber", 95), ("ledger", 95)]),
        ("", []),
    ])
    result = pilot_uploads._ocr_pdf_page(tmp_path / "synthetic.pdf", 1, native_text="RECEIVED.")
    assert result == PdfOcrResult("amber ledger", "recognized")
    assert len(_tesseract_calls(calls)) == 4


@pytest.mark.parametrize("retries", [
    [("RECEIVED", [("RECEIVED", 95)]), ("", []), ("", [])],
    [subprocess.TimeoutExpired("tesseract", 1)],
])
def test_native_only_initial_reading_survives_unhelpful_or_failed_retries(monkeypatch, tmp_path, retries):
    calls = _fake_ocr(monkeypatch, [("RECEIVED", [("RECEIVED", 95)]), *retries])
    result = pilot_uploads._ocr_pdf_page(tmp_path / "synthetic.pdf", 1, native_text="RECEIVED.")
    assert result == PdfOcrResult("RECEIVED", "recognized")
    assert len(_tesseract_calls(calls)) == 1 + len(retries)


def test_native_only_retry_does_not_end_search_before_useful_rotation(monkeypatch, tmp_path):
    # The first retry is fully confident but adds nothing native text lacks.
    calls = _fake_ocr(monkeypatch, [
        ("RECEIVED Ja MOJIM", [("RECEIVED", 95), ("Ja", 20), ("MOJIM", 15)]),
        ("RECEIVED", [("RECEIVED", 95)]),
        ("amber ledger", [("amber", 95), ("ledger", 95)]),
        ("", []),
    ])
    result = pilot_uploads._ocr_pdf_page(tmp_path / "synthetic.pdf", 1, native_text="RECEIVED.")
    assert result == PdfOcrResult("amber ledger", "recognized")
    assert len(_tesseract_calls(calls)) == 4


def test_confident_non_improving_retry_does_not_end_search(monkeypatch, tmp_path):
    calls = _fake_ocr(monkeypatch, [
        ("amber Ja MOJIM", [("amber", 90), ("Ja", 20), ("MOJIM", 15)]),
        ("amber", [("amber", 95)]),
        ("", []),
        ("amber ledger", [("amber", 95), ("ledger", 95)]),
    ])
    assert pilot_uploads._ocr_pdf_page(tmp_path / "synthetic.pdf", 1) == PdfOcrResult("amber ledger", "recognized")
    assert len(_tesseract_calls(calls)) == 4


def test_orientation_retries_share_one_additional_timeout(monkeypatch, tmp_path):
    # Deadline at 120; each retry reads the clock before and after rotation.
    clock = iter([100.0, 100.0, 100.0, 110.0, 115.0, 119.0, 121.0])
    monkeypatch.setattr(pilot_uploads.time, "monotonic", lambda: next(clock))
    low = ("Ja MOJIM", [("Ja", 20), ("MOJIM", 15)])
    calls = _fake_ocr(monkeypatch, [low, low, low])
    assert pilot_uploads._ocr_pdf_page(tmp_path / "synthetic.pdf", 1) == PdfOcrResult("Ja MOJIM", "recognized")
    timeouts = [kwargs["timeout"] for _, kwargs in _tesseract_calls(calls)]
    # First reading uses the existing timeout; retries consume one shared budget,
    # and the third retry is not launched once rotation passes the deadline.
    assert timeouts == [pilot_uploads.OCR_TIMEOUT_SECONDS, 20.0, 5.0]


def test_retry_is_not_launched_when_rotation_exhausts_the_deadline(monkeypatch, tmp_path):
    clock = iter([100.0, 119.5, 120.0])
    monkeypatch.setattr(pilot_uploads.time, "monotonic", lambda: next(clock))
    rotations = []
    actual = pilot_uploads._rotate_pgm
    monkeypatch.setattr(pilot_uploads, "_rotate_pgm", lambda *a: rotations.append(a[1]) or actual(*a))
    calls = _fake_ocr(monkeypatch, [("Ja MOJIM", [("Ja", 20), ("MOJIM", 15)])])
    assert pilot_uploads._ocr_pdf_page(tmp_path / "synthetic.pdf", 1) == PdfOcrResult("Ja MOJIM", "recognized")
    # Rotation began with budget left, but no Tesseract run follows expiry.
    assert rotations == [180]
    assert len(_tesseract_calls(calls)) == 1


def test_unrecognized_raster_format_keeps_first_reading(monkeypatch, tmp_path):
    calls = _fake_ocr(monkeypatch, [("Ja MOJIM", [("Ja", 20), ("MOJIM", 15)])], raster=b"P6\n1 1\n255\n\0\0\0")
    assert pilot_uploads._ocr_pdf_page(tmp_path / "synthetic.pdf", 1) == PdfOcrResult("Ja MOJIM", "recognized")
    assert len(_tesseract_calls(calls)) == 1


def test_rotate_pgm_turns_pixels_clockwise():
    # 3x2 image: rows (0 1 2) and (3 4 5).
    assert pilot_uploads._rotate_pgm(_RASTER, 90) == b"P5\n2 3\n255\n" + bytes([3, 0, 4, 1, 5, 2])
    assert pilot_uploads._rotate_pgm(_RASTER, 270) == b"P5\n2 3\n255\n" + bytes([2, 5, 1, 4, 0, 3])
    assert pilot_uploads._rotate_pgm(_RASTER, 180) == b"P5\n3 2\n255\n" + bytes([5, 4, 3, 2, 1, 0])
    assert pilot_uploads._rotate_pgm(b"P5\n3 2\n255\n\0", 90) is None


@pytest.mark.parametrize("native", ["", "Ja MOJIM"])
@pytest.mark.parametrize("words", [None, []])
def test_missing_confidence_words_keep_reading_without_retry(monkeypatch, tmp_path, native, words):
    calls = _fake_ocr(monkeypatch, [("Ja MOJIM", words)])
    assert pilot_uploads._ocr_pdf_page(tmp_path / "synthetic.pdf", 1, native_text=native) == PdfOcrResult("Ja MOJIM", "recognized")
    assert len(_tesseract_calls(calls)) == 1


def test_ocr_output_table_limit_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(pilot_uploads, "MAX_OCR_TSV_BYTES", 10)
    _fake_ocr(monkeypatch, [("body", [("body", 90)])])
    assert pilot_uploads._ocr_pdf_page(tmp_path / "synthetic.pdf", 1).status == "output_limit"


def test_ready_pdf_reextraction_requires_new_source_identity(tmp_path, monkeypatch):
    store, old = ingest(tmp_path, monkeypatch,
        [PdfPage(1, "Historical synthetic stamp with adequate native text", 1.0, True)],
        lambda *_: PdfOcrResult("newbody", "recognized"), mode="off")
    original = old.document_id, old.version_id, old.digest, old.parsed_units(), old.message
    monkeypatch.setenv("CASE_INTELLIGENCE_OCR_MODE", "expanded")
    with pytest.raises(pilot_uploads.UploadProblem, match="cannot be retried"):
        store.retry(old.document_id)
    new, _ = store.store_stream("synthetic-new-extraction.pdf", "application/pdf",
                                io.BytesIO(b"%PDF-synthetic-outcomes"))
    assert new.document_id != old.document_id and new.version_id != old.version_id
    assert new.digest == old.digest
    assert "newbody" in new.parsed_units()[0].text
    assert original == (old.document_id, old.version_id, old.digest, old.parsed_units(), old.message)
