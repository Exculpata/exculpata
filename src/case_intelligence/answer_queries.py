"""Compose bounded answer searches without displacing the current question."""
import re
import textwrap
from itertools import zip_longest
from typing import Sequence


MAX_SEARCH_CHARS = 512
_WORD = re.compile(r"[a-z0-9]+")
_FOLLOWUP_WORDS = {"it", "that", "those", "they", "them", "this", "these", "he", "she", "there"}


def contextual_query(question: str, messages: Sequence) -> str:
    terms = tuple(_WORD.findall(question.casefold()))
    contextual = len(terms) < 6 or bool(_FOLLOWUP_WORDS.intersection(terms))
    if not contextual:
        return question
    previous = next(
        (message.content for message in reversed(messages) if message.role == "user"), ""
    )
    # An accepted question longer than one search is windowed separately;
    # history must not displace any part of it.
    remaining = MAX_SEARCH_CHARS - len(question) - 1
    previous = previous[:max(remaining, 0)].strip()
    return f"{previous} {question}".strip()


def append_notebook_query(query: str, items: Sequence) -> str:
    remaining = MAX_SEARCH_CHARS - len(query) - 1
    if remaining <= 0:
        return query
    context = " ".join(" ".join(" ".join((
        item.title, item.date_label, item.body[:300],
    )).split()) for item in items)
    return f"{query} {context[:remaining]}".strip()


def query_windows(query: str) -> tuple[str, ...]:
    if len(query) <= MAX_SEARCH_CHARS:
        return (query,)
    # At most seven windows for a normalized question of up to 2,000 chars.
    return tuple(textwrap.wrap(query, width=MAX_SEARCH_CHARS, break_on_hyphens=False))


def search_windows(search, matter, windows: Sequence[str], *, limit: int,
                   document_ids=None, stage_callback=None) -> tuple:
    """Divide one candidate budget across windows and interleave their ranks."""
    if limit < len(windows):
        raise ValueError("Retrieval candidate limit must cover all current-question windows.")
    pages = []
    for index, query in enumerate(windows):
        window_limit = (limit + len(windows) - 1 - index) // len(windows)
        pages.append(search(matter, query, limit=window_limit,
            stage_callback=stage_callback if index == 0 else None, document_ids=document_ids))
    return tuple(row for rank in zip_longest(*pages) for row in rank if row is not None)
