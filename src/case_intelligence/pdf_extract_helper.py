"""Resource-bounded child process for born-digital PDF extraction."""
from __future__ import annotations

import json
import math
import resource
import sys
from pathlib import Path

MAX_PDF_PAGES = 500
MAX_PDF_PAGE_CHARS = 250_000
MAX_PDF_TOTAL_CHARS = 5_000_000
MAX_PDF_CHUNKS = 2_000
MAX_IMAGE_EVIDENCE_OPERATIONS = 50_000
MAX_IMAGE_EVIDENCE_PLACEMENTS = 256
MAX_IMAGE_EVIDENCE_FORMS = 64
MAX_IMAGE_EVIDENCE_DEPTH = 8
MAX_IMAGE_EVIDENCE_ANNOTATIONS = 256
_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _numbers(values: object, count: int) -> tuple[float, ...]:
    if not isinstance(values, (list, tuple)) or len(values) != count:
        raise ValueError("invalid geometry")
    result = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in result):
        raise ValueError("invalid geometry")
    return result


def _compose(local: tuple[float, ...], parent: tuple[float, ...]) -> tuple[float, ...]:
    a, b, c, d, e, f = local
    g, h, i, j, k, l = parent
    return _numbers(
        (a * g + b * i, a * h + b * j, c * g + d * i,
         c * h + d * j, e * g + f * i + k, e * h + f * j + l), 6,
    )


def _transformed_bounds(
    rectangle: tuple[float, ...], matrix: tuple[float, ...],
) -> tuple[float, ...]:
    left, bottom, right, top = rectangle
    if left >= right or bottom >= top:
        raise ValueError("invalid rectangle")
    a, b, c, d, e, f = matrix
    points = [(a * x + c * y + e, b * x + d * y + f)
              for x, y in ((left, bottom), (left, top), (right, bottom), (right, top))]
    return _numbers((min(x for x, _ in points), min(y for _, y in points),
                     max(x for x, _ in points), max(y for _, y in points)), 4)


def _intersection(first: tuple[float, ...], second: tuple[float, ...]) -> tuple[float, ...]:
    return (max(first[0], second[0]), max(first[1], second[1]),
            min(first[2], second[2]), min(first[3], second[3]))


def page_image_evidence(page: object, reader: object) -> tuple[float, bool]:
    """Bounded conservative image-placement area, never a text-quality score.

    Inspect painted images, including inline images and nested Forms, without
    decoding their pixels. Sum clipped bounding boxes, so overlapping images,
    nonrectangular clipping and skew can overestimate coverage. Unknown or
    exhausted traversal is explicit rather than mistaken for a native-only page.
    Page rotation does not change the fraction of the page occupied by an image.
    Parsing itself remains inside the existing resource-limited child process.
    """
    from pypdf.generic import ArrayObject, ContentStream, DictionaryObject, NameObject

    area = 0.0
    operations = placements = forms = 0
    page_area = 1.0

    def walk(content, resources, matrix, clip, ancestors, depth):
        nonlocal area, operations, placements, forms
        if depth > MAX_IMAGE_EVIDENCE_DEPTH:
            raise ValueError("image evidence depth limit")
        stream = ContentStream(content, reader)
        stack = []
        for operands, operator in stream.operations:
            operations += 1
            if operations > MAX_IMAGE_EVIDENCE_OPERATIONS:
                raise ValueError("image evidence operation limit")
            if operator == b"q":
                if len(stack) >= MAX_IMAGE_EVIDENCE_DEPTH * 8:
                    raise ValueError("image evidence graphics stack limit")
                stack.append(matrix)
            elif operator == b"Q":
                if not stack:
                    raise ValueError("invalid graphics stack")
                matrix = stack.pop()
            elif operator == b"cm":
                matrix = _compose(_numbers(operands, 6), matrix)
            elif operator in {b"scn", b"SCN"} and any(
                isinstance(operand, NameObject) for operand in operands
            ):
                # A named pattern can paint images without any page-level Do.
                # Do not decode or expand its repeated cells merely to select
                # OCR. Numeric colors alone do not imply a pattern.
                raise ValueError("pattern image evidence is unknown")
            elif operator == b"Tf":
                if len(operands) != 2:
                    raise ValueError("invalid font reference")
                font = resources["/Font"][operands[0]].get_object()
                if font.get("/Subtype") == "/Type3":
                    # Type3 glyph programs can paint images, too. Their text
                    # extraction does not establish absence of image content.
                    raise ValueError("glyph image evidence is unknown")
            elif operator == b"gs":
                if len(operands) != 1:
                    raise ValueError("invalid graphics state reference")
                state = resources["/ExtGState"][operands[0]].get_object()
                mask = state.get("/SMask")
                if mask is not None and mask.get_object() != "/None":
                    # A transparency-group image can turn an ordinary fill
                    # into visible text without a page-level image placement.
                    raise ValueError("soft mask image evidence is unknown")
                font_setting = state.get("/Font")
                if font_setting is not None:
                    font_setting = font_setting.get_object()
                    if not isinstance(font_setting, ArrayObject) or len(font_setting) != 2:
                        raise ValueError("invalid graphics state font")
                    font = font_setting[0].get_object()
                    if font.get("/Subtype") == "/Type3":
                        raise ValueError("glyph image evidence is unknown")
            elif operator in {b"Do", b"INLINE IMAGE"}:
                if operator == b"Do":
                    if len(operands) != 1:
                        raise ValueError("invalid image reference")
                    obj = resources["/XObject"][operands[0]].get_object()
                    subtype = obj.get("/Subtype")
                    if subtype == "/Form":
                        forms += 1
                        if forms > MAX_IMAGE_EVIDENCE_FORMS or id(obj) in ancestors:
                            raise ValueError("image evidence form limit")
                        form_matrix = _compose(_numbers(obj.get("/Matrix", _IDENTITY), 6), matrix)
                        form_clip = _intersection(
                            clip, _transformed_bounds(_numbers(obj["/BBox"], 4), form_matrix),
                        )
                        form_resources = obj.get("/Resources", resources).get_object()
                        walk(obj, form_resources, form_matrix, form_clip,
                             ancestors | {id(obj)}, depth + 1)
                        continue
                    if subtype != "/Image":
                        raise ValueError("unknown external object")
                placements += 1
                if placements > MAX_IMAGE_EVIDENCE_PLACEMENTS:
                    raise ValueError("image evidence placement limit")
                bounds = _intersection(clip, _transformed_bounds((0, 0, 1, 1), matrix))
                image_area = max(0.0, bounds[2] - bounds[0]) * max(0.0, bounds[3] - bounds[1])
                area = min(page_area, area + image_area)
        if stack:
            raise ValueError("unbalanced graphics stack")

    try:
        # CropBox is in the same unrotated coordinate system as content matrices.
        crop = _numbers(list(page.cropbox), 4)
        if crop[2] <= crop[0] or crop[3] <= crop[1]:
            raise ValueError("invalid page bounds")
        measured_area = (crop[2] - crop[0]) * (crop[3] - crop[1])
        if not math.isfinite(measured_area) or measured_area <= 0:
            raise ValueError("invalid page area")
        page_area = measured_area
        content = page.get_contents()
        if content is not None:
            resources = page.get("/Resources", DictionaryObject()).get_object()
            walk(content, resources, _IDENTITY, crop, set(), 0)
        annotations = page.get("/Annots")
        if annotations is not None:
            annotations = annotations.get_object()
            if (not isinstance(annotations, ArrayObject)
                    or len(annotations) > MAX_IMAGE_EVIDENCE_ANNOTATIONS):
                raise ValueError("annotation image evidence limit or malformed array")
            for reference in annotations:
                annotation = reference.get_object()
                if not isinstance(annotation, DictionaryObject):
                    raise ValueError("malformed annotation")
                # Appearance streams can paint images outside page Contents;
                # widgets and other annotation kinds may also generate visible
                # appearances. Do not decode or recurse into those programs.
                # An ordinary link without an appearance adds no image body.
                if "/AP" in annotation or annotation.get("/Subtype") != "/Link":
                    raise ValueError("annotation appearance evidence is unknown")
                _numbers(annotation.get("/Rect"), 4)
        return min(1.0, area / page_area), True
    except Exception:
        return min(1.0, area / page_area), False


_ADDRESS_SPACE_CEILING = 512 * 1024 * 1024


def _limit_address_space(ceiling: int = _ADDRESS_SPACE_CEILING) -> None:
    """Apply an address-space ceiling when this platform accepts it.

    Linux can lower ``RLIMIT_AS``. macOS often rejects that call with
    ``ValueError`` ("current limit exceeds maximum limit") because the current
    soft limit already exceeds the requested hard ceiling, or because the kernel
    will not set the resource. A matching hard ceiling is tried first. If that
    fails, only the soft limit is lowered and the current hard limit is kept.
    If neither applies, extraction continues. Refusal is not document damage.
    """
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    except (ValueError, OSError):
        return
    infinity = resource.RLIM_INFINITY
    target_hard = ceiling if hard == infinity or ceiling <= hard else hard
    target_soft = soft if soft != infinity and soft < target_hard else target_hard
    if (soft, hard) == (target_soft, target_hard):
        return
    attempts = [(target_soft, target_hard)]
    if hard != target_hard:
        attempts.append((target_soft, hard))
    for limits in attempts:
        try:
            resource.setrlimit(resource.RLIMIT_AS, limits)
        except (ValueError, OSError):
            continue
        return


def _limit_process() -> None:
    # The web process also enforces a wall timeout and bounded JSON validation.
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    _limit_address_space()
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))


def main() -> int:
    _limit_process()
    source = Path(sys.argv[1])
    try:
        from pypdf import PdfReader

        with source.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ValueError("signature")
            stream.seek(0)
            reader = PdfReader(stream, strict=True)
            if reader.is_encrypted:
                print(json.dumps({"error": "encrypted"}))
                return 2
            page_count = len(reader.pages)
            if page_count > MAX_PDF_PAGES:
                print(json.dumps({"error": "page_limit"}))
                return 2
            pages: list[dict[str, object]] = []
            total = 0
            chunks = 0
            for number, page in enumerate(reader.pages, 1):
                text = (page.extract_text() or "").strip()
                if len(text) > MAX_PDF_PAGE_CHARS:
                    print(json.dumps({"error": "page_text_limit"}))
                    return 2
                total += len(text)
                if total > MAX_PDF_TOTAL_CHARS:
                    print(json.dumps({"error": "total_text_limit"}))
                    return 2
                if text:
                    chunks += 1
                    if chunks > MAX_PDF_CHUNKS:
                        print(json.dumps({"error": "chunk_limit"}))
                        return 2
                image_coverage, image_evidence_known = page_image_evidence(page, reader)
                pages.append({"page_number": number, "text": text,
                              "image_coverage": image_coverage,
                              "image_evidence_known": image_evidence_known})
        print(json.dumps({"pages": pages}, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception:
        print(json.dumps({"error": "malformed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
