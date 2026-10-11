"""Address-space ceiling regressions for the PDF extraction child.

Synthetic pages only. A platform that refuses RLIMIT_AS must still return text
pypdf can read, and that refusal must not be reported as a damaged PDF.
"""
from __future__ import annotations

import json
import resource
import subprocess
import sys
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from case_intelligence import pdf_extract_helper
from case_intelligence.review_bench_v2 import extract_pdf_pages

ROOT = Path(__file__).resolve().parents[1]
HARBOR_STREET_PDFS = (
    ROOT / "demo_data/harbor-street/exhibits/38-planned-route-sheet.pdf",
    ROOT / "demo_data/harbor-street/exhibits/39-follow-up-count-sheet.pdf",
)
CEILING = pdf_extract_helper._ADDRESS_SPACE_CEILING


def _readable_pdf(path: Path, text: str) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 36 100 Td ({text}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(path)


def _patch_limits(monkeypatch, getrlimit, setrlimit) -> None:
    monkeypatch.setattr(pdf_extract_helper.resource, "getrlimit", getrlimit)
    monkeypatch.setattr(pdf_extract_helper.resource, "setrlimit", setrlimit)


def test_refused_address_space_ceiling_is_not_document_damage(tmp_path, monkeypatch, capsys):
    source = tmp_path / "synthetic-readable.pdf"
    _readable_pdf(source, "Synthetic harbor page")
    calls = []

    def getrlimit(kind):
        assert kind == resource.RLIMIT_AS
        return (resource.RLIM_INFINITY, resource.RLIM_INFINITY)

    def setrlimit(kind, limits):
        calls.append((kind, limits))
        if kind == resource.RLIMIT_AS:
            raise ValueError("current limit exceeds maximum limit")

    _patch_limits(monkeypatch, getrlimit, setrlimit)
    monkeypatch.setattr(sys, "argv", ["pdf_extract_helper.py", str(source)])

    assert pdf_extract_helper.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pages"][0]["text"] == "Synthetic harbor page"
    assert "error" not in payload
    assert calls == [
        (resource.RLIMIT_CPU, (10, 10)),
        (resource.RLIMIT_AS, (CEILING, CEILING)),
        (resource.RLIMIT_AS, (CEILING, resource.RLIM_INFINITY)),
        (resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024)),
        (resource.RLIMIT_NOFILE, (32, 32)),
    ]


def test_address_space_ceiling_is_installed_when_the_platform_allows_it(monkeypatch):
    calls = []

    def getrlimit(kind):
        assert kind == resource.RLIMIT_AS
        return (resource.RLIM_INFINITY, resource.RLIM_INFINITY)

    def setrlimit(kind, limits):
        assert kind == resource.RLIMIT_AS
        calls.append(limits)

    _patch_limits(monkeypatch, getrlimit, setrlimit)
    pdf_extract_helper._limit_address_space()
    assert calls == [(CEILING, CEILING)]


def test_failed_hard_ceiling_keeps_the_current_hard_limit(monkeypatch):
    current_soft = 1024 * 1024 * 1024
    current_hard = 2 * 1024 * 1024 * 1024
    calls = []

    def getrlimit(kind):
        assert kind == resource.RLIMIT_AS
        return (current_soft, current_hard)

    def setrlimit(kind, limits):
        assert kind == resource.RLIMIT_AS
        calls.append(limits)
        if limits[1] < current_soft:
            raise ValueError("current limit exceeds maximum limit")

    _patch_limits(monkeypatch, getrlimit, setrlimit)
    pdf_extract_helper._limit_address_space()
    assert calls == [(CEILING, CEILING), (CEILING, current_hard)]


def test_tighter_address_space_soft_limit_is_left_in_place(monkeypatch):
    def getrlimit(kind):
        assert kind == resource.RLIMIT_AS
        return (128 * 1024 * 1024, 256 * 1024 * 1024)

    def setrlimit(kind, limits):
        raise AssertionError(limits)

    _patch_limits(monkeypatch, getrlimit, setrlimit)
    pdf_extract_helper._limit_address_space()


def test_tighter_soft_limit_still_lowers_a_higher_hard_ceiling(monkeypatch):
    soft = 128 * 1024 * 1024
    calls = []

    def getrlimit(kind):
        assert kind == resource.RLIMIT_AS
        return (soft, resource.RLIM_INFINITY)

    def setrlimit(kind, limits):
        assert kind == resource.RLIMIT_AS
        calls.append(limits)

    _patch_limits(monkeypatch, getrlimit, setrlimit)
    pdf_extract_helper._limit_address_space()
    assert calls == [(soft, CEILING)]


def test_tighter_soft_limit_remains_when_the_hard_ceiling_is_refused(monkeypatch):
    soft = 128 * 1024 * 1024
    calls = []

    def getrlimit(kind):
        assert kind == resource.RLIMIT_AS
        return (soft, resource.RLIM_INFINITY)

    def setrlimit(kind, limits):
        assert kind == resource.RLIMIT_AS
        calls.append(limits)
        raise ValueError("current limit exceeds maximum limit")

    _patch_limits(monkeypatch, getrlimit, setrlimit)
    pdf_extract_helper._limit_address_space()
    assert calls == [(soft, CEILING), (soft, resource.RLIM_INFINITY)]


def test_unlimited_soft_limit_uses_an_existing_lower_hard_ceiling(monkeypatch):
    hard = 128 * 1024 * 1024
    calls = []

    def getrlimit(kind):
        assert kind == resource.RLIMIT_AS
        return (resource.RLIM_INFINITY, hard)

    def setrlimit(kind, limits):
        assert kind == resource.RLIMIT_AS
        calls.append(limits)

    _patch_limits(monkeypatch, getrlimit, setrlimit)
    pdf_extract_helper._limit_address_space()
    assert calls == [(hard, hard)]


def test_unreadable_address_space_limit_is_skipped(monkeypatch):
    def getrlimit(kind):
        assert kind == resource.RLIMIT_AS
        raise OSError("unsupported")

    def setrlimit(kind, limits):
        raise AssertionError(limits)

    _patch_limits(monkeypatch, getrlimit, setrlimit)
    pdf_extract_helper._limit_address_space()


def test_both_address_space_attempts_can_fail_without_raising(monkeypatch):
    calls = []

    def getrlimit(kind):
        assert kind == resource.RLIMIT_AS
        return (1024 * 1024 * 1024, 1024 * 1024 * 1024)

    def setrlimit(kind, limits):
        assert kind == resource.RLIMIT_AS
        calls.append(limits)
        raise ValueError("current limit exceeds maximum limit")

    _patch_limits(monkeypatch, getrlimit, setrlimit)
    pdf_extract_helper._limit_address_space()
    hard = 1024 * 1024 * 1024
    assert calls == [(CEILING, CEILING), (CEILING, hard)]


def test_harbor_street_pdfs_stay_searchable():
    for path in HARBOR_STREET_PDFS:
        direct = PdfReader(path, strict=True)
        expected = tuple((page.extract_text() or "").strip() for page in direct.pages)
        assert any(expected)
        pages = extract_pdf_pages(path)
        assert tuple(page.text for page in pages) == expected


def test_child_follows_the_platform_address_space_ceiling():
    probe = """
import json, resource
from case_intelligence.pdf_extract_helper import _ADDRESS_SPACE_CEILING
before = resource.getrlimit(resource.RLIMIT_AS)
direct = "applied"
try:
    resource.setrlimit(resource.RLIMIT_AS, (_ADDRESS_SPACE_CEILING, _ADDRESS_SPACE_CEILING))
except (ValueError, OSError):
    direct = "refused"
print(json.dumps({"before": before, "direct": direct, "ceiling": _ADDRESS_SPACE_CEILING}))
"""
    helper = """
import json, resource
from case_intelligence.pdf_extract_helper import _ADDRESS_SPACE_CEILING, _limit_address_space
before = resource.getrlimit(resource.RLIMIT_AS)
_limit_address_space()
print(json.dumps({
    "before": before,
    "after": resource.getrlimit(resource.RLIMIT_AS),
    "ceiling": _ADDRESS_SPACE_CEILING,
}))
"""

    def run(code: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=10, check=False,
        )

    probed = run(probe)
    helped = run(helper)
    assert probed.returncode == 0, probed.stderr
    assert helped.returncode == 0, helped.stderr
    probe_payload = json.loads(probed.stdout)
    helper_payload = json.loads(helped.stdout)
    assert probe_payload["before"] == helper_payload["before"]
    before_soft, before_hard = helper_payload["before"]
    after = tuple(helper_payload["after"])
    ceiling = helper_payload["ceiling"]
    infinity = resource.RLIM_INFINITY
    target = ceiling if before_hard == infinity or ceiling <= before_hard else before_hard
    if before_soft != infinity and before_soft <= target:
        assert after == (before_soft, before_hard)
    elif probe_payload["direct"] == "applied":
        assert after == (ceiling, ceiling)
    else:
        assert after == (before_soft, before_hard) or after == (target, before_hard)
