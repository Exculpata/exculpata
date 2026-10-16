"""The support drawer marks readable cited text, never OCR symbol noise.

The PDF fixture is generated here: a blank page with an invisible text layer,
the form a scanned page takes after OCR, carrying synthetic noise lines.
"""
import html
import io
import re

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from case_intelligence.citation_highlight import (
    has_readable_highlight, highlight_segments, low_content,
)
from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.workbench import create_workbench_app


ACTOR = "development-taylor-morgan"
STATEMENT = "The amber lantern stayed lit at the north gate."
NOISE = ("=3s .", "~ ;: ,.", "|| l |", "- 7 -")
OCR_LINES = (NOISE[0], STATEMENT, NOISE[1], "=3s . The courier left before dawn.", NOISE[2], NOISE[3])


@pytest.mark.parametrize("text", [*NOISE, "", "   ", ".", "@@##$$ ab", "3s"])
def test_low_content_spans(text):
    assert low_content(text)


@pytest.mark.parametrize("text", [
    STATEMENT, "A. No.", "Q. Yes?", "No.", "12/03/2031", "$1,250.00", "Ok", "Page 4",
])
def test_readable_spans(text):
    assert not low_content(text)


def test_segments_keep_exact_text_and_mark_only_readable_runs():
    segments = highlight_segments(OCR_LINES)
    assert tuple("".join(text for text, _ in line) for line in segments) == OCR_LINES
    assert segments[0] == ((NOISE[0], False),)
    assert segments[1] == ((STATEMENT, True),)
    assert segments[3] == (("=3s . ", False), ("The courier left before dawn.", True))
    assert all(not marked for index in (2, 4, 5) for _, marked in segments[index])
    assert has_readable_highlight(segments)


def test_labels_and_initials_are_not_split_from_their_sentence():
    assert highlight_segments(("Q. Did you see him? A. Yes.", "J. Doe signed it.")) == (
        (("Q. Did you see him? A. Yes.", True),), (("J. Doe signed it.", True),))


def test_all_noise_unit_keeps_text_without_any_highlight():
    segments = highlight_segments(NOISE)
    assert not has_readable_highlight(segments)
    assert tuple("".join(text for text, _ in line) for line in segments) == NOISE
    assert highlight_segments(("",)) == ((("", False),),)


def _ocr_layer_pdf(lines):
    """One page whose only text is an invisible OCR-style layer (render mode 3)."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
        NameObject("/F1"): DictionaryObject({
            NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }),
    })})
    commands = []
    for index, line in enumerate(lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        commands.append(f"BT /F1 14 Tf 3 Tr 45 {740 - index * 30} Td ({escaped}) Tj ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(commands).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class EchoGenerator:
    available = True

    def generate(self, **kwargs):
        return {
            "answerable": bool(kwargs["evidence"]),
            "claims": [{"text": STATEMENT, "evidence_ids": [item.evidence_id]}
                       for item in kwargs["evidence"]],
            "limitation": None,
            "missing_information": "",
        }


def _support_excerpt(page):
    match = re.search(r'<div class="support-excerpt">(.*?)</div>', page, re.S)
    assert match is not None
    return match.group(1)


def test_drawer_marks_cited_sentence_and_leaves_ocr_noise_unmarked(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_OCR_MODE", "off")
    app = create_workbench_app(tmp_path / "runtime", auth_mode="test", generator=EchoGenerator(),
        storage_policy=StoragePolicy(reserve_bytes=0))
    with TestClient(app) as client:
        bench = app.state.workbench
        bench.answers.close()
        bench.answers = None
        matter = bench.create_matter("Synthetic OCR noise citation", "", ACTOR)
        document, _ = bench.source_store(matter).store_stream(
            "Synthetic scanned log.pdf", "application/pdf", io.BytesIO(_ocr_layer_pdf(OCR_LINES)))
        bench._sync_source_catalog(matter, [document])
        conversation = bench.workspace.get_conversation(matter.matter_id)
        saved = bench.ask(matter, conversation, "Was the amber lantern lit at the north gate?")
        assert saved.payload["answerable"] is True
        citations = saved.payload["claims"][0]["citations"]
        assert citations, "Filtering the highlight must never drop the citation"
        token = citations[0]["support_token"]

        support = bench.support(matter, token)
        assert NOISE[0] in support.lines and STATEMENT in support.lines
        page = client.get(f"/matters/{matter.slug}?support={token}&conversation={conversation.conversation_id}")
        assert page.status_code == 200, page.text
        excerpt = _support_excerpt(page.text)
        marks = [html.unescape(value) for value in re.findall(r"<mark>(.*?)</mark>", excerpt, re.S)]
        assert STATEMENT in marks
        assert "The courier left before dawn." in marks
        assert not any(low_content(value) for value in marks)
        assert all(noise not in value for noise in NOISE for value in marks)
        # Noise stays visible as unmarked source text, so nothing is hidden.
        assert html.escape(NOISE[0]) in excerpt
        assert "little readable text" not in page.text


def test_drawer_keeps_all_noise_citation_open_with_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_OCR_MODE", "off")
    app = create_workbench_app(tmp_path / "runtime", auth_mode="test", generator=EchoGenerator(),
        storage_policy=StoragePolicy(reserve_bytes=0))
    with TestClient(app) as client:
        bench = app.state.workbench
        matter = bench.create_matter("Synthetic OCR noise only", "", ACTOR)
        document, _ = bench.source_store(matter).store_stream(
            "Synthetic noise page.pdf", "application/pdf", io.BytesIO(_ocr_layer_pdf(NOISE)))
        bench._sync_source_catalog(matter, [document])
        units = document.parsed_units()
        token = bench._support_token(bench._candidate(matter, document, units[0], 1))
        page = client.get(f"/matters/{matter.slug}?support={token}")
        assert page.status_code == 200, page.text
        excerpt = _support_excerpt(page.text)
        assert "<mark>" not in excerpt
        assert html.escape(NOISE[0]) in excerpt
        assert "little readable text" in page.text
        assert "Open full PDF" in page.text
