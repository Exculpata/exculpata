"""Verify local PDF OCR and page-level exact search with a packaged scan."""
import argparse
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory


_SCAN = Path(__file__).with_name("config") / "generated-scan.pdf"
_SCAN_SHA256 = "c65007b0ab52a86da99333dde0932ea33db05389b6357ce948f84f73739405b1"

OK = "ok"
DISABLED = "Local PDF OCR is disabled."
FAILED = "Local PDF OCR check failed. Check the application installation, Poppler, Tesseract and the configured OCR language."
DIAGNOSTIC_MESSAGES = frozenset({OK, DISABLED, FAILED})


def doctor() -> str:
    from .exact_search_results import search_documents
    from .pilot_uploads import PilotStore, pdf_ocr_mode

    if pdf_ocr_mode() == "off":
        return DISABLED
    try:
        with _SCAN.open("rb") as stream:
            scan = stream.read(16 * 1024 + 1)
        if hashlib.sha256(scan).hexdigest() != _SCAN_SHA256:
            return FAILED
        with TemporaryDirectory(prefix="exculpata-ocr-") as scratch:
            with closing(PilotStore(Path(scratch))) as store:
                document, _ = store.store_stream("Synthetic scan.pdf", "application/pdf", io.BytesIO(scan))
                result = search_documents([document], '"silver lantern"', scope=("synthetic-ocr",))
                if len(result.items) != 1:
                    return FAILED
                hit = result.items[0]
                if (hit.document_id == document.document_id and hit.source_version == document.version_id
                        and [(unit.number, unit.location) for unit in hit.passages] == [(2, "Page 2")]):
                    return OK
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        return FAILED
    return FAILED


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    print(json.dumps({"ocr": doctor()}))
