"""Read PDF page-label metadata from the verified original, not PilotDocument fields."""
from hashlib import sha256
from io import BytesIO

from pypdf import PdfReader
from pypdf.generic import IndirectObject, DictionaryObject, ArrayObject

from .contracts import SourceLocator
from .production_bates import parse_bates
from .workspace_store import WorkspaceProblem

MAX_METADATA_BYTES = 64 * 1024 * 1024
MAX_METADATA_NODES = 20_000


def pdf_bates_locators(store, document, *, charge, page_limit):
    """Only explicit decimal/prefix PDF page labels are Bates candidates.

    Workbench has no persisted SourceVersion locator metadata. Its canonical
    source bytes are authoritative; convert their page labels into SourceLocator
    values, preserving physical page identity for citation and deduplication.
    """
    if document.page_count > page_limit:
        raise WorkspaceProblem('PDF metadata page limit reached; no partial summary was returned.')
    charge(document.size, document.page_count)
    path = store.source_path(document.document_id)
    raw = store._read_regular_file(path, MAX_METADATA_BYTES)
    if len(raw) != document.size or sha256(raw).hexdigest() != document.digest:
        raise ValueError('PDF original differs from its recorded source version.')
    reader = PdfReader(BytesIO(raw), strict=True)
    tree = reader.trailer['/Root'].get('/PageLabels')
    if tree is None:
        return []
    pending, seen, entries = [tree], set(), {}
    while pending:
        value = pending.pop()
        key = (value.idnum, value.generation) if isinstance(value, IndirectObject) else id(value)
        if key in seen:
            raise ValueError('Repeated PDF page-label node.')
        seen.add(key)
        if len(seen) > MAX_METADATA_NODES:
            raise WorkspaceProblem('PDF metadata node limit reached; no partial summary was returned.')
        node = value.get_object()
        if not isinstance(node, DictionaryObject):
            raise ValueError('Invalid PDF page-label node.')
        nums = node.get('/Nums', ArrayObject()).get_object()
        kids = node.get('/Kids', ArrayObject()).get_object()
        if not isinstance(nums, ArrayObject) or not isinstance(kids, ArrayObject):
            raise ValueError('Invalid PDF page-label entries.')
        if len(nums) % 2 or len(nums) + len(kids) + len(pending) > MAX_METADATA_NODES:
            raise WorkspaceProblem('PDF metadata entry limit reached; no partial summary was returned.')
        pending.extend(kids)
        for offset in range(0, len(nums), 2):
            index, spec = nums[offset], nums[offset + 1].get_object()
            if not isinstance(spec, DictionaryObject):
                raise ValueError('Invalid PDF page-label specification.')
            if not isinstance(index, int) or not 0 <= index < document.page_count or index in entries:
                raise ValueError('Invalid PDF page-label page.')
            entries[index] = spec
            if len(entries) > page_limit:
                raise WorkspaceProblem('PDF metadata page limit reached; no partial summary was returned.')
    result = []
    positions = sorted(entries)
    for position, start in enumerate(positions):
        spec = entries[start]
        prefix, style, number = spec.get('/P', ''), spec.get('/S'), spec.get('/St', 1)
        if not isinstance(prefix, str) or len(prefix) > 128 or not isinstance(number, int) or number < 1:
            raise ValueError('Invalid PDF page-label value.')
        if style not in (None, '/D'):
            continue
        end = positions[position + 1] if position + 1 < len(positions) else document.page_count
        for page in range(start, end):
            label = prefix + (str(number + page - start) if style == '/D' else '')
            if parse_bates(label):
                result.append(SourceLocator(page=page + 1, bates_start=label))
    return result


def locator_record(locator, document_id, source_href):
    """Consume the canonical contract's Bates fields with a physical-page link."""
    return dict(bates_start=locator.bates_start, bates_end=locator.bates_end,
        identity=(document_id, locator.page), link=source_href + '/content#page=' + str(locator.page))
