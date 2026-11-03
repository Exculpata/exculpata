"""Bounded, deterministic parsing and threading for EML and MBOX sources."""
from __future__ import annotations

import mailbox
import re
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path

from case_intelligence.extended_extract import (
    MAX_EMAIL_PARTS,
    MAX_TEXT_CONTAINER_BYTES,
    _decoded_email_part,
    extract_email,
)

MAX_MESSAGES = 500
_ID = re.compile(r"<([^<>\s]+)>")
_SUBJECT_PREFIX = re.compile(r"^(?:(?:re|fw|fwd)\s*:\s*|\[external\]\s*)", re.IGNORECASE)
_LEAD_IN = re.compile(r"(?im)^On .{1,500} wrote:\s*$")
_BLOCK_MARKER = re.compile(
    r"(?im)^(?:-{2,}\s*(?:Original Message|Forwarded message)\s*-{2,}|Begin forwarded message:)\s*$"
)
_QUOTED_LINES = re.compile(r"(?m)^(?:[ \t]*>.*(?:\n|$))+")


@dataclass(frozen=True)
class QuotedSpan:
    start: int
    end: int
    text: str
    message_id: str | None = None


@dataclass(frozen=True)
class EmailRecord:
    source: str
    message_id: str | None
    in_reply_to: tuple[str, ...]
    references: tuple[str, ...]
    subject: str
    body: str
    quoted_spans: tuple[QuotedSpan, ...]
    error: str | None = None


@dataclass(frozen=True)
class ThreadNode:
    message: EmailRecord
    parent_id: str | None
    weak_link: bool = False


@dataclass(frozen=True)
class EmailThread:
    key: str
    messages: tuple[ThreadNode, ...]


def _ids(value: object) -> tuple[str, ...]:
    return tuple(match.casefold() for match in _ID.findall(str(value or "")))


def _subject(value: object) -> str:
    normalized = " ".join(str(value or "").split()).casefold()
    previous = None
    while normalized != previous:
        previous = normalized
        normalized = _SUBJECT_PREFIX.sub("", normalized).strip()
    return normalized


def _body(message: Message) -> str:
    parts = list(message.walk())
    if len(parts) > MAX_EMAIL_PARTS:
        raise ValueError("That email contains too many MIME parts.")
    if any(part.defects for part in parts):
        raise ValueError("That email is damaged or malformed.")
    candidates = [
        part
        for part in parts
        if not part.is_multipart()
        and part.get_content_type() == "text/plain"
        and part.get_content_disposition() != "attachment"
    ]
    if not candidates:
        candidates = [
            part
            for part in parts
            if not part.is_multipart()
            and part.get_content_type() == "text/html"
            and part.get_content_disposition() != "attachment"
        ]
    return _decoded_email_part(candidates[0]) if candidates else ""


def detect_quoted_history(body: str) -> tuple[QuotedSpan, ...]:
    """Return non-overlapping quoted or forwarded regions with exact offsets."""
    ranges: list[tuple[int, int]] = []
    markers = list(_BLOCK_MARKER.finditer(body)) + list(_LEAD_IN.finditer(body))
    if markers:
        marker = min(markers, key=lambda match: match.start())
        ranges.append((marker.start(), len(body)))
    cutoff = ranges[0][0] if ranges else len(body)
    ranges.extend((match.start(), match.end()) for match in _QUOTED_LINES.finditer(body, 0, cutoff))
    spans = []
    for start, end in sorted(ranges):
        text = body[start:end]
        matched_ids = _ids(text)
        spans.append(QuotedSpan(start, end, text, matched_ids[0] if matched_ids else None))
    return tuple(spans)


def _record(message: Message, source: str) -> EmailRecord:
    body = _body(message)
    message_ids = _ids(message.get("Message-ID"))
    return EmailRecord(
        source=source,
        message_id=message_ids[0] if message_ids else None,
        in_reply_to=_ids(message.get("In-Reply-To")),
        references=_ids(message.get("References")),
        subject=" ".join(str(message.get("Subject", "")).split()),
        body=body,
        quoted_spans=detect_quoted_history(body),
    )


def _error(source: str, exc: Exception) -> EmailRecord:
    return EmailRecord(source, None, (), (), "", "", (), str(exc))


def read_eml(path: Path) -> EmailRecord:
    """Read one EML within the existing extractor limits, returning errors as data."""
    try:
        extract_email(path)
        message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
        return _record(message, str(path))
    except Exception as exc:
        return _error(str(path), exc)


def read_mbox(path: Path, *, max_messages: int = MAX_MESSAGES) -> tuple[EmailRecord, ...]:
    """Read a bounded MBOX without allowing one malformed message to stop the batch."""
    if path.stat().st_size > MAX_TEXT_CONTAINER_BYTES:
        return (_error(str(path), ValueError("The mailbox exceeds the supported review limit.")),)
    records: list[EmailRecord] = []
    box = mailbox.mbox(
        path,
        factory=lambda stream: BytesParser(policy=policy.default).parse(stream),
        create=False,
    )
    try:
        for index, message in enumerate(box):
            if index >= max_messages:
                records.append(_error(f"{path}#{index}", ValueError("The mailbox contains too many messages.")))
                break
            try:
                records.append(_record(message, f"{path}#{index}"))
            except Exception as exc:
                records.append(_error(f"{path}#{index}", exc))
    finally:
        box.close()
    return tuple(records)


def group_threads(records: list[EmailRecord] | tuple[EmailRecord, ...]) -> tuple[EmailThread, ...]:
    """Group records using header links, then flagged normalized-subject links."""
    known = {record.message_id: record for record in records if record.message_id and not record.error}
    nodes: list[ThreadNode] = []
    subject_roots: dict[str, str] = {}
    roots: dict[str, str] = {}
    for index, record in enumerate(records):
        if record.error:
            key = f"error:{index}"
            roots[key] = key
            nodes.append(ThreadNode(record, None))
            continue
        parent_id = next(
            (candidate for candidate in reversed(record.in_reply_to + record.references) if candidate in known),
            None,
        )
        weak = False
        normalized = _subject(record.subject)
        if parent_id is None and normalized and (not record.message_id or not record.in_reply_to and not record.references):
            parent_id = subject_roots.get(normalized)
            weak = parent_id is not None
        identity = record.message_id or f"anonymous:{index}"
        subject_roots.setdefault(normalized, identity)
        roots[identity] = roots.get(parent_id, parent_id) if parent_id else identity
        nodes.append(ThreadNode(record, parent_id, weak))
    grouped: dict[str, list[ThreadNode]] = {}
    for index, node in enumerate(nodes):
        identity = node.message.message_id or f"anonymous:{index}"
        root = roots.get(identity, identity)
        while root in roots and roots[root] != root:
            root = roots[root]
        grouped.setdefault(root, []).append(node)
    return tuple(EmailThread(key, tuple(messages)) for key, messages in grouped.items())
