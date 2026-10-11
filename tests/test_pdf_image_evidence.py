"""Synthetic PDF geometry regressions; no OCR engine is mocked by these tests."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject,
    NumberObject, RectangleObject,
)

from case_intelligence import pdf_extract_helper
from case_intelligence.review_bench_v2 import PdfPage, extract_pdf_pages


def _array(values):
    return ArrayObject([FloatObject(value) for value in values])


def _stream(data):
    stream = DecodedStreamObject()
    stream.set_data(data)
    return stream


def _image():
    image = _stream(bytes([255, 0, 255, 0] * 16))
    image.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(8),
        NameObject("/Height"): NumberObject(8),
        NameObject("/ColorSpace"): NameObject("/DeviceGray"),
        NameObject("/BitsPerComponent"): NumberObject(8),
    })
    return image


def _pdf(path, commands=b"", native="", *, rotation=0, crop=None, form=None):
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    resources = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
        NameObject("/XObject"): DictionaryObject({NameObject("/Scan"): writer._add_object(_image())}),
    })
    if form is not None:
        form[NameObject("/Resources")] = resources
        # Give the form its own resources before adding it to page resources.
        resources = DictionaryObject(dict(resources))
        resources[NameObject("/XObject")] = DictionaryObject(dict(resources["/XObject"]))
        resources["/XObject"][NameObject("/Body")] = writer._add_object(form)
    page[NameObject("/Resources")] = resources
    if native:
        commands += f"\nBT /F1 7 Tf 4 92 Td ({native}) Tj ET\n".encode("ascii")
    page[NameObject("/Contents")] = writer._add_object(_stream(commands))
    if rotation:
        page.rotate(rotation)
    if crop:
        page.cropbox = RectangleObject(crop)
    writer.write(path)


def _read_pages(source):
    reader = PdfReader(source, strict=True)
    return tuple(
        PdfPage(number, (page.extract_text() or "").strip(),
                *pdf_extract_helper.page_image_evidence(page, reader))
        for number, page in enumerate(reader.pages, 1)
    )


@pytest.mark.parametrize(("commands", "native", "expected"), [
    (b"", "A useful native paragraph without painted images.", 0.0),
    (b"q 80 0 0 80 10 10 cm /Scan Do Q", "", 0.64),
    (b"q 80 0 0 80 10 10 cm /Scan Do Q", "STAMP", 0.64),
    (b"q 80 0 0 80 10 10 cm /Scan Do Q", "RECEIVED FOR SYNTHETIC RECORD REVIEW", 0.64),
    (b"q 100 0 0 40 0 0 cm /Scan Do Q", "Native text above an image body.", 0.4),
    (b"q 10 0 0 10 5 5 cm /Scan Do Q", "A useful native paragraph with a small logo.", 0.01),
    (b"q 80 0 0 80 110 110 cm /Scan Do Q", "An off-page image is not a scan body.", 0.0),
    (b"q 0 80 -80 0 90 10 cm /Scan Do Q", "A rotated scan body and native stamp.", 0.64),
])
def test_real_pdf_reports_painted_image_geometry_and_preserves_native_text(tmp_path, commands, native, expected):
    source = tmp_path / "synthetic.pdf"
    _pdf(source, commands, native)
    pages = _read_pages(source)
    assert len(pages) == 1
    assert pages[0].page_number == 1
    assert pages[0].text == native
    assert pages[0].image_evidence_known is True
    assert pages[0].image_coverage == pytest.approx(expected)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_cropbox_and_page_rotation_keep_image_fraction(tmp_path, rotation):
    source = tmp_path / "synthetic-rotation.pdf"
    _pdf(source, b"q 50 0 0 50 25 25 cm /Scan Do Q", rotation=rotation, crop=(25, 25, 75, 75))
    page, = _read_pages(source)
    assert page.image_evidence_known is True
    assert page.image_coverage == 1.0


def test_inline_image_is_counted_without_pixel_decoding(tmp_path):
    source = tmp_path / "synthetic-inline.pdf"
    _pdf(source, b"q 80 0 0 80 10 10 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \xff EI Q")
    page, = _read_pages(source)
    assert page.image_evidence_known is True
    assert page.image_coverage == pytest.approx(0.64)


@pytest.mark.parametrize(("color_space", "color_operator", "paint"), [
    (b"cs", b"scn", b"f"),
    (b"CS", b"SCN", b"S"),
])
def test_stamped_image_painted_by_pattern_has_unknown_evidence(tmp_path, color_space, color_operator, paint):
    source = tmp_path / "synthetic-pattern.pdf"
    stamp = "SYNTHETIC RECEIVED STAMP WITH MANY NATIVE CHARACTERS"
    _pdf(source, native=stamp)
    writer = PdfWriter()
    page = writer.add_page(PdfReader(source).pages[0])
    resources = page["/Resources"]
    pattern = _stream(b"q 80 0 0 80 10 10 cm /Scan Do Q")
    pattern.update({
        NameObject("/Type"): NameObject("/Pattern"),
        NameObject("/PatternType"): NumberObject(1),
        NameObject("/PaintType"): NumberObject(1),
        NameObject("/TilingType"): NumberObject(1),
        NameObject("/BBox"): _array([0, 0, 100, 100]),
        NameObject("/XStep"): NumberObject(100),
        NameObject("/YStep"): NumberObject(100),
        NameObject("/Resources"): DictionaryObject({NameObject("/XObject"): resources["/XObject"]}),
    })
    resources[NameObject("/Pattern")] = DictionaryObject({NameObject("/Body"): writer._add_object(pattern)})
    # Only the pattern cell contains an image Do; the page paints the pattern
    # and then its native stamp. No image bytes need decoding for selection.
    commands = b"q /Pattern " + color_space + b" /Body " + color_operator + b" 0 0 100 100 re " + paint + b" Q\n"
    page[NameObject("/Contents")] = writer._add_object(_stream(commands + page.get_contents().get_data()))
    writer.write(source)
    extracted, = _read_pages(source)
    assert extracted.text == stamp
    assert extracted.image_coverage == 0.0
    assert extracted.image_evidence_known is False


@pytest.mark.parametrize(("color_space", "color_operator"), [(b"cs", b"scn"), (b"CS", b"SCN")])
def test_numeric_colors_with_native_text_remain_known_without_images(tmp_path, color_space, color_operator):
    source = tmp_path / "synthetic-numeric-colors.pdf"
    _pdf(source, b"/DeviceRGB " + color_space + b" 0 0 0 " + color_operator,
         "Useful native text with ordinary numeric colors.")
    page, = _read_pages(source)
    assert page.image_evidence_known is True
    assert page.image_coverage == 0.0


@pytest.mark.parametrize("font_operator", ["Tf", "gs"])
def test_stamped_type3_glyph_image_has_unknown_evidence(tmp_path, font_operator):
    source = tmp_path / "synthetic-type3.pdf"
    stamp = "SYNTHETIC RECEIVED STAMP WITH MANY NATIVE CHARACTERS"
    _pdf(source, native=stamp)
    writer = PdfWriter()
    page = writer.add_page(PdfReader(source).pages[0])
    resources = page["/Resources"]
    glyph = _stream(b"1000 0 d0 q 1000 0 0 1000 0 0 cm /Scan Do Q")
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type3"),
        NameObject("/FontBBox"): _array([0, 0, 1000, 1000]),
        NameObject("/FontMatrix"): _array([0.001, 0, 0, 0.001, 0, 0]),
        NameObject("/CharProcs"): DictionaryObject({NameObject("/A"): writer._add_object(glyph)}),
        NameObject("/Encoding"): DictionaryObject({NameObject("/Differences"): ArrayObject([NumberObject(65), NameObject("/A")])}),
        NameObject("/FirstChar"): NumberObject(65),
        NameObject("/LastChar"): NumberObject(65),
        NameObject("/Widths"): _array([1000]),
        NameObject("/Resources"): DictionaryObject({NameObject("/XObject"): resources["/XObject"]}),
    })
    font_reference = writer._add_object(font)
    resources["/Font"][NameObject("/ImageGlyph")] = font_reference
    if font_operator == "gs":
        resources[NameObject("/ExtGState")] = DictionaryObject({
            NameObject("/ImageFont"): DictionaryObject({
                NameObject("/Font"): ArrayObject([font_reference, NumberObject(80)]),
            }),
        })
        commands = b"BT /ImageFont gs 10 10 Td (A) Tj ET\n"
    else:
        commands = b"BT /ImageGlyph 80 Tf 10 10 Td (A) Tj ET\n"
    page[NameObject("/Contents")] = writer._add_object(_stream(commands + page.get_contents().get_data()))
    writer.write(source)
    extracted, = _read_pages(source)
    assert stamp in extracted.text
    assert extracted.image_coverage == 0.0
    assert extracted.image_evidence_known is False


def _annotation_pdf(source, *, appearance=True, subtype="/Stamp", count=1):
    stamp = "SYNTHETIC RECEIVED STAMP WITH MANY NATIVE CHARACTERS"
    _pdf(source, native=stamp)
    writer = PdfWriter()
    page = writer.add_page(PdfReader(source).pages[0])
    annotation = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"),
        NameObject("/Subtype"): NameObject(subtype),
        NameObject("/Rect"): _array([0, 0, 100, 100]),
        NameObject("/F"): NumberObject(4),
    })
    if appearance:
        body = _stream(b"q 80 0 0 80 10 10 cm /Scan Do Q")
        body.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): _array([0, 0, 100, 100]),
            NameObject("/Resources"): DictionaryObject({
                NameObject("/XObject"): page["/Resources"]["/XObject"],
            }),
        })
        annotation[NameObject("/AP")] = DictionaryObject({NameObject("/N"): writer._add_object(body)})
    page[NameObject("/Annots")] = ArrayObject([writer._add_object(annotation)] * count)
    writer.write(source)
    return stamp


@pytest.mark.parametrize("subtype", ["/Stamp", "/Widget", "/Link"])
def test_image_annotation_appearance_with_long_stamp_is_unknown(tmp_path, subtype):
    source = tmp_path / "synthetic-appearance.pdf"
    stamp = _annotation_pdf(source, subtype=subtype)
    page, = _read_pages(source)
    assert page.text == stamp
    assert page.image_coverage == 0.0
    assert page.image_evidence_known is False


def test_link_only_annotations_without_appearances_preserve_native_selection(tmp_path):
    source = tmp_path / "synthetic-link.pdf"
    stamp = _annotation_pdf(source, subtype="/Link", appearance=False)
    page, = _read_pages(source)
    assert page.text == stamp
    assert page.image_coverage == 0.0
    assert page.image_evidence_known is True


@pytest.mark.parametrize("annotations", [
    DictionaryObject(), ArrayObject([NumberObject(3)]),
    ArrayObject([DictionaryObject({NameObject("/Subtype"): NameObject("/Link")})]),
])
def test_malformed_annotations_are_unknown(tmp_path, annotations):
    source = tmp_path / "synthetic-malformed-annotations.pdf"
    _pdf(source, native="Useful synthetic native paragraph remains available.")
    writer = PdfWriter()
    page = writer.add_page(PdfReader(source).pages[0])
    page[NameObject("/Annots")] = annotations
    writer.write(source)
    extracted, = _read_pages(source)
    assert extracted.text == "Useful synthetic native paragraph remains available."
    assert extracted.image_evidence_known is False


def test_annotation_inspection_is_bounded(tmp_path, monkeypatch):
    source = tmp_path / "synthetic-bounded-annotations.pdf"
    _annotation_pdf(source, subtype="/Link", appearance=False, count=2)
    monkeypatch.setattr(pdf_extract_helper, "MAX_IMAGE_EVIDENCE_ANNOTATIONS", 1)
    page, = _read_pages(source)
    assert page.image_evidence_known is False


@pytest.mark.parametrize(("state_kind", "known"), [
    ("image_mask", False), ("none_mask", True), ("opacity", True),
    ("standard_font", True), ("malformed_mask", False),
])
def test_graphics_state_image_masks_are_unknown_but_simple_states_remain_known(tmp_path, state_kind, known):
    source = tmp_path / "synthetic-graphics-state.pdf"
    stamp = "SYNTHETIC RECEIVED STAMP WITH MANY NATIVE CHARACTERS"
    _pdf(source, native=stamp)
    writer = PdfWriter()
    page = writer.add_page(PdfReader(source).pages[0])
    resources = page["/Resources"]
    state = DictionaryObject({NameObject("/Type"): NameObject("/ExtGState")})
    if state_kind == "image_mask":
        body = _stream(b"q 80 0 0 80 10 10 cm /Scan Do Q")
        body.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): _array([0, 0, 100, 100]),
            NameObject("/Resources"): DictionaryObject({NameObject("/XObject"): resources["/XObject"]}),
            NameObject("/Group"): DictionaryObject({
                NameObject("/S"): NameObject("/Transparency"),
                NameObject("/CS"): NameObject("/DeviceGray"),
            }),
        })
        state[NameObject("/SMask")] = DictionaryObject({
            NameObject("/S"): NameObject("/Luminosity"),
            NameObject("/G"): writer._add_object(body),
        })
    elif state_kind == "none_mask":
        state[NameObject("/SMask")] = NameObject("/None")
    elif state_kind == "malformed_mask":
        state[NameObject("/SMask")] = NumberObject(3)
    elif state_kind == "standard_font":
        state[NameObject("/Font")] = ArrayObject([
            writer._add_object(resources["/Font"]["/F1"]), NumberObject(7),
        ])
    else:
        state[NameObject("/ca")] = FloatObject(0.5)
    resources[NameObject("/ExtGState")] = DictionaryObject({NameObject("/PaintState"): writer._add_object(state)})
    commands = b"q /PaintState gs 0 g 0 0 100 100 re f Q\n"
    page[NameObject("/Contents")] = writer._add_object(_stream(commands + page.get_contents().get_data()))
    writer.write(source)
    extracted, = _read_pages(source)
    assert extracted.text == stamp
    assert extracted.image_coverage == 0.0
    assert extracted.image_evidence_known is known


def test_form_image_placement_uses_form_and_parent_matrices_and_bbox(tmp_path):
    source = tmp_path / "synthetic-form.pdf"
    form = _stream(b"/Scan Do")
    form.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Form"),
        NameObject("/BBox"): _array([0, 0, 1, 1]),
        NameObject("/Matrix"): _array([2, 0, 0, 2, 0, 0]),
    })
    _pdf(source, b"q 40 0 0 40 10 10 cm /Body Do Q", "FORM STAMP", form=form)
    page, = _read_pages(source)
    assert page.text == "FORM STAMP"
    assert page.image_evidence_known is True
    assert page.image_coverage == pytest.approx(0.64)


@pytest.mark.parametrize("limit", ["MAX_IMAGE_EVIDENCE_FORMS", "MAX_IMAGE_EVIDENCE_DEPTH"])
def test_form_traversal_limits_report_unknown(tmp_path, monkeypatch, limit):
    source = tmp_path / "synthetic-bounded-form.pdf"
    form = _stream(b"/Scan Do")
    form.update({NameObject("/Subtype"): NameObject("/Form"),
                 NameObject("/BBox"): _array([0, 0, 1, 1])})
    _pdf(source, b"q 80 0 0 80 10 10 cm /Body Do Q", "RETAIN NATIVE STAMP", form=form)
    monkeypatch.setattr(pdf_extract_helper, limit, 0)
    page, = _read_pages(source)
    assert page.text == "RETAIN NATIVE STAMP"
    assert page.image_evidence_known is False


@pytest.mark.parametrize(("limit", "value", "commands"), [
    ("MAX_IMAGE_EVIDENCE_OPERATIONS", 1, b"q 80 0 0 80 0 0 cm /Scan Do Q"),
    ("MAX_IMAGE_EVIDENCE_PLACEMENTS", 1, b"/Scan Do /Scan Do"),
    ("MAX_IMAGE_EVIDENCE_DEPTH", 0, b"q q /Scan Do Q Q"),
])
def test_bounded_traversal_returns_unknown_instead_of_absence(tmp_path, monkeypatch, limit, value, commands):
    source = tmp_path / "synthetic-bounded.pdf"
    _pdf(source, commands)
    reader = PdfReader(source)
    monkeypatch.setattr(pdf_extract_helper, limit, value)
    coverage, known = pdf_extract_helper.page_image_evidence(reader.pages[0], reader)
    assert 0 <= coverage <= 1
    assert known is False


def test_unresolved_image_resource_preserves_native_text_and_reports_unknown(tmp_path):
    source = tmp_path / "synthetic-unresolved.pdf"
    _pdf(source, b"/Missing Do", "Useful native text remains available.")
    page, = _read_pages(source)
    assert page.text == "Useful native text remains available."
    assert page.image_evidence_known is False


@pytest.mark.parametrize("image_evidence", [
    {}, {"image_coverage": 0.5},
    {"image_coverage": -0.1, "image_evidence_known": True},
    {"image_coverage": 1.1, "image_evidence_known": True},
    {"image_coverage": 10 ** 1000, "image_evidence_known": True},
    {"image_coverage": float("nan"), "image_evidence_known": True},
    {"image_coverage": float("inf"), "image_evidence_known": True},
    {"image_coverage": True, "image_evidence_known": True},
    {"image_coverage": "0.5", "image_evidence_known": True},
    {"image_coverage": 0.5, "image_evidence_known": "true"},
    {"image_coverage": 0.5, "image_evidence_known": 1},
])
def test_child_image_metadata_is_validated_before_use(tmp_path, monkeypatch, image_evidence):
    source = tmp_path / "synthetic-output.pdf"
    source.write_bytes(b"%PDF-synthetic-test")
    payload = {"pages": [{"page_number": 1, "text": "native", **image_evidence}]}
    completed = SimpleNamespace(stdout=json.dumps(payload).encode(), returncode=0)
    monkeypatch.setattr("case_intelligence.review_bench_v2.subprocess.run", lambda *a, **kw: completed)
    with pytest.raises(ValueError, match="invalid image evidence"):
        extract_pdf_pages(source)


def test_old_pdf_page_constructors_explicitly_have_unknown_image_evidence():
    page = PdfPage(1, "Existing caller")
    assert page.image_coverage == 0.0
    assert page.image_evidence_known is False


def test_resource_limited_child_transports_native_text_and_image_evidence(tmp_path):
    source = tmp_path / "synthetic-child.pdf"
    _pdf(source, b"q 80 0 0 80 10 10 cm /Scan Do Q", "LONG SYNTHETIC RECEIVED STAMP")
    page, = extract_pdf_pages(source)
    assert page.text == "LONG SYNTHETIC RECEIVED STAMP"
    assert page.image_evidence_known is True
    assert page.image_coverage == pytest.approx(0.64)
