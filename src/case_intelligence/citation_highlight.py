"""Choose which parts of a cited source unit the support drawer marks.

OCR and text extraction can leave symbol runs such as ``=3s .`` on their own
lines or beside readable sentences. Marking those as the cited passage tells a
reviewer that noise is the support. Low-content segments are therefore shown
unmarked; every source character and the citation itself remain visible.
"""
from __future__ import annotations

import re

MIN_WORD_CHARS = 4
_WORD_CHAR = re.compile(r"[^\W_]", re.UNICODE)
_LETTER_RUN = re.compile(r"[^\W\d_]{2,}", re.UNICODE)
_BOUNDARY = re.compile(r"[.!?]+\s+")


def low_content(text: str) -> bool:
    """True for spans that are mostly punctuation/symbols or too short to read.

    A span with a real word (two or more letters) or at least MIN_WORD_CHARS
    letters/digits is readable, unless symbols outnumber its word characters.
    Speaker labels and short answers such as ``A. No.`` stay readable.
    """
    words = len(_WORD_CHAR.findall(text))
    symbols = sum(1 for character in text if not character.isspace()) - words
    if symbols > words:
        return True
    return words < MIN_WORD_CHARS and _LETTER_RUN.search(text) is None


def _segments(line: str) -> list[str]:
    """Split at sentence ends without separating initials or list labels."""
    pieces, start = [], 0
    for boundary in _BOUNDARY.finditer(line):
        token = line[start:boundary.start()].rsplit(None, 1)
        if token and len(token[-1].lstrip("(")) == 1 and token[-1][-1].isalnum():
            continue  # "A. Yes." and "J. Doe" stay one readable segment.
        pieces.append(line[start:boundary.end()])
        start = boundary.end()
    if start < len(line):
        pieces.append(line[start:])
    return pieces


def highlight_segments(lines: tuple[str, ...]) -> tuple[tuple[tuple[str, bool], ...], ...]:
    """Return each line as exact (text, marked) runs; joined runs equal the line."""
    result = []
    for line in lines:
        runs: list[tuple[str, bool]] = []
        for piece in _segments(line):
            marked = not low_content(piece)
            if runs and runs[-1][1] == marked:
                runs[-1] = (runs[-1][0] + piece, marked)
            else:
                runs.append((piece, marked))
        result.append(tuple(runs) or (("", False),))
    return tuple(result)


def has_readable_highlight(segments) -> bool:
    return any(marked for line in segments for _, marked in line)
