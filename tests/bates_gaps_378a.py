"""Frozen 378a reference from main c0f96cb724c42d0f3fae971600576404c5af6587.

Keep the parser helpers and gap computation independent of the live engine.
"""
from collections import defaultdict
from dataclasses import dataclass
from itertools import groupby
import re
import unicodedata
from typing import Iterable, Literal


@dataclass(frozen=True)
class BatesDocument:
    document_id: str
    version_id: str
    labels: tuple[tuple[str, str | None], ...]
    page_count: int | None = 1
    labels_complete: bool = True


@dataclass(frozen=True)
class GapFact:
    kind: Literal["missing", "possible", "not_computed", "computed"]
    series: str
    lower: int | None
    upper: int | None
    text: str
    cited_ids: tuple[tuple[str, str], ...]
    reason: str


def _plain(label):
    match = re.fullmatch(r"([A-Za-z]+)([0-9]{1,18})", label)
    return (match[1], int(match[2]), len(match[2])) if match else None


def _may_name(label, prefix):
    plain = _plain(label)
    if plain and plain[0] == prefix:
        return False
    folded = unicodedata.normalize("NFKC", label).casefold()
    letters = re.sub(r"[^a-z]", "", folded)
    return bool(re.search(r"[0-9]", folded)) and (not letters or prefix.casefold() in letters)


def _unreadable(label):
    invisible = any(c != " " and (unicodedata.category(c) in {"Cc", "Cf", "Mn", "Me"} or
                                  unicodedata.category(c).startswith("Z")) for c in label)
    return invisible or not unicodedata.normalize("NFKC", label).isascii()


def compute_bates_gaps(docs: Iterable[BatesDocument]) -> tuple[GapFact, ...]:
    """Return maximal gaps with source/version citations; never truncate facts."""
    docs = tuple(docs)
    facts, members, blocked = [], defaultdict(list), defaultdict(list)
    touches, namers = defaultdict(list), defaultdict(set)
    incomplete = {(d.document_id, d.version_id) for d in docs if not d.labels or not d.labels_complete}
    rows, unsafe = [], set()

    def emit(kind, series, lower, upper, text, ids, reason=""):
        if len(facts) >= 100000:
            raise OverflowError("Bates fact limit exceeded (100000)")
        facts.append(GapFact(kind, series, lower, upper, text, tuple(sorted(set(ids))), reason))

    for d in docs:
        cite = (d.document_id, d.version_id)
        for start, end in d.labels:
            if any(_unreadable(label) for label in (start, end or start)):
                unsafe.add(cite)
            rows.append((start, end, d, cite))
    if unsafe:
        emit("not_computed", "ALL", None, None,
             "Bates series ALL: not computed (unreadable character in a label)", unsafe,
             "unreadable character in a label")
        return tuple(facts)
    for cite in sorted(incomplete):
        emit("not_computed", "", None, None, "Bates labels not read: unlabeled or incomplete document",
             [cite], "unlabeled or incomplete document")
    for start, end, d, cite in rows:
        a, b = _plain(start), _plain(end or start)
        if a and b and a[0] == b[0] and a[1] <= b[1]:
            members[a[0]].append((a[1], b[1], a[2], b[2], cite))
            count = d.page_count
            if count is None or count <= 0 or count > b[1] - a[1] + 1:
                hi = a[1] + count - 1 if count is not None and count > 0 else None
                touches[a[0]].append((a[1], hi, cite))
        else:
            for p in (a, b):
                if p:
                    members[p[0]].append((p[1], p[1], p[2], p[2], cite))
        if a and b and (a[0] != b[0] or a[1] > b[1]):
            reason = "different prefixes" if a[0] != b[0] else "reversed range"
            for prefix in {a[0], b[0]}:
                blocked[prefix].append((cite, reason))
    for start, end, d, cite in rows:
        for prefix in members:
            if any(_may_name(label, prefix) for label in (start, end or start)):
                namers[prefix].add(cite)
        if not _plain(start) or (end is not None and not _plain(end)):
            emit("not_computed", "", None, None, f"Bates labels not read: {start}" +
                 (f" to {end}" if end else ""), [cite], "non-plain label")
    for prefix, spans in sorted(members.items()):
        if blocked[prefix]:
            reasons = "; ".join(sorted({r for _, r in blocked[prefix]}))
            emit("not_computed", prefix, None, None, f"Bates series {prefix}: not computed ({reasons})",
                 [c for c, _ in blocked[prefix]], reasons)
            continue
        spans.sort(key=lambda s: s[:4])
        first, last = spans[0][0], max(s[1] for s in spans)
        width = max(max(s[2:4]) for s in spans)

        def label(n, padding=width):
            return prefix + str(n).zfill(padding)

        outside = namers[prefix] | {c for _, hi, c in touches[prefix] if hi is not None and hi > last}
        if outside:
            emit("not_computed", prefix, None, None,
                 f"Bates series {prefix}: gaps outside {label(first)} to {label(last)} not computed",
                 outside, "label may name series or page count extends outside span")
        right, right_ids, right_widths = first - 1, set(), set()
        for lo, group in groupby(spans, key=lambda s: s[0]):
            group = list(group)
            left_ids, left_widths = {s[4] for s in group}, {s[2] for s in group}
            if right_ids and lo > right + 1:
                causes = {c for a, b, c in touches[prefix] if a < lo and (last if b is None else b) > right}
                reasons = (["page count touches gap"] if causes else [])
                if namers[prefix]:
                    reasons.append("label may name series")
                if len(left_widths | right_widths) != 1:
                    reasons.append("bracketing digit widths differ")
                if incomplete:
                    reasons.append("unlabeled or incomplete document")
                ids = right_ids | left_ids | causes | incomplete | namers[prefix]
                widths = left_widths | right_widths
                gap_width = next(iter(widths)) if len(widths) == 1 else width
                text = label(right + 1, gap_width) + (f" to {label(lo - 1, gap_width)}" if lo > right + 2 else "")
                reason = "; ".join(reasons)
                emit("possible" if reasons else "missing", prefix, right + 1, lo - 1,
                     ("Possible missing Bates " if reasons else "Missing Bates ") + text +
                     (f" ({reason})" if reason else ""), ids, reason)
            for _, hi, _, end_width, cite in group:
                if hi > right:
                    right, right_ids, right_widths = hi, {cite}, {end_width}
                elif hi == right:
                    right_ids.add(cite)
                    right_widths.add(end_width)
        emit("computed", prefix, None, None, f"Bates series {prefix}: computed",
             [s[4] for s in spans] + sorted(namers[prefix]))
    return tuple(facts)
