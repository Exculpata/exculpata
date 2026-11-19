"""Operator checks for the packaged, local scanned-document fixture."""
from dataclasses import replace
import json
import os
import subprocess
import sys

import pytest
from pypdf import PdfReader

from case_intelligence import exact_search_results, ocr_diagnostic, pilot_uploads


@pytest.mark.skipif(sys.platform != "linux", reason="The app image provides Poppler and Tesseract on Linux.")
def test_packaged_scan_is_searchable_on_its_original_page(tmp_path):
    pages = PdfReader(ocr_diagnostic._SCAN).pages
    assert len(pages) == 2
    assert pages[0].extract_text().strip()
    assert not pages[1].extract_text().strip()
    assert "silver lantern" not in pages[0].extract_text().casefold()
    environment = os.environ | {
        "TMPDIR": str(tmp_path),
        "CASE_INTELLIGENCE_OCR_MODE": "expanded",
        "CASE_INTELLIGENCE_MAX_OCR_PAGES": "1",
        "CASE_INTELLIGENCE_OCR_LANGUAGE": "eng",
    }
    command = [sys.executable, "-m", "case_intelligence.ocr_diagnostic"]
    result = subprocess.run(command, env=environment, capture_output=True, check=True, timeout=120)
    assert json.loads(result.stdout) == {"ocr": ocr_diagnostic.OK}
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("primary,legacy", [("off", "expanded"), ("", "selective"), (None, None)])
def test_disabled_mode_never_opens_the_fixture(monkeypatch, tmp_path, primary, legacy):
    for key, value in (("CASE_INTELLIGENCE_OCR_MODE", primary), ("CASE_REVIEW_OCR_MODE", legacy)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(ocr_diagnostic, "_SCAN", tmp_path / "missing.pdf")
    assert ocr_diagnostic.doctor() == ocr_diagnostic.DISABLED


@pytest.mark.parametrize("status,text", [("failed", ""), ("recognized", "Different words.")])
def test_missing_recognition_or_wrong_words_fail_even_with_native_text(monkeypatch, status, text):
    monkeypatch.delenv("CASE_INTELLIGENCE_OCR_MODE", raising=False)
    monkeypatch.setenv("CASE_REVIEW_OCR_MODE", "selective")
    attempted = []

    def recognize(source, page_number):
        attempted.append(page_number)
        return pilot_uploads.PdfOcrResult(text=text, status=status)

    monkeypatch.setattr(pilot_uploads, "_ocr_pdf_page", recognize)
    assert ocr_diagnostic.doctor() == ocr_diagnostic.FAILED
    assert attempted == [2]


def test_missing_packaged_scan_is_a_fixed_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("CASE_INTELLIGENCE_OCR_MODE", "expanded")
    monkeypatch.setattr(ocr_diagnostic, "_SCAN", tmp_path / "missing.pdf")
    assert ocr_diagnostic.doctor() == ocr_diagnostic.FAILED


@pytest.mark.parametrize("wrong_field", ["source_version", "document_id", "passages"])
def test_match_must_cite_the_original_source_version_and_page(monkeypatch, wrong_field):
    monkeypatch.setenv("CASE_INTELLIGENCE_OCR_MODE", "selective")
    monkeypatch.setattr(pilot_uploads, "_ocr_pdf_page", lambda *_: pilot_uploads.PdfOcrResult(
        status="recognized", text="A silver lantern marks the second page.",
    ))
    search = exact_search_results.search_documents

    def change_locator(documents, query, *, scope):
        assert scope == ("synthetic-ocr",)
        result = search(documents, query, scope=scope)
        hit, = result.items
        change = (replace(hit.passages[0], number=1),) if wrong_field == "passages" else "another-source"
        return replace(result, items=(replace(hit, **{wrong_field: change}),))

    monkeypatch.setattr(exact_search_results, "search_documents", change_locator)
    assert ocr_diagnostic.doctor() == ocr_diagnostic.FAILED


def test_health_reports_the_effective_pdf_ocr_mode(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from case_intelligence.workbench import create_workbench_app

    monkeypatch.delenv("CASE_INTELLIGENCE_OCR_MODE", raising=False)
    monkeypatch.setenv("CASE_REVIEW_OCR_MODE", " Selective ")
    with TestClient(create_workbench_app(tmp_path / "runtime", auth_mode="preview")) as client:
        assert client.get("/health").json()["capabilities"]["ocr"] == "selective"
        monkeypatch.setenv("CASE_INTELLIGENCE_OCR_MODE", "off")
        assert client.get("/health").json()["capabilities"]["ocr"] == "off"
