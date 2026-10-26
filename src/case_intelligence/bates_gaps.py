"""Pure Bates gap facts. See docs/BATES_GAPS.md for the conservative boundary."""
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


def _related(label):
    folded = unicodedata.normalize("NFKC", label).casefold().strip()
    skeleton = re.sub(r"[\W_]", "|", folded)
    match = re.match(r"([a-z]+)\|?(\d+)", skeleton)
    return (match[1], int(match[2])) if match else None


def _unreadable(label):
    return not label.isascii() or not label.isprintable()


def compute_bates_gaps(docs: Iterable[BatesDocument]) -> tuple[GapFact, ...]:
    """Return maximal gaps with source/version citations; never truncate facts."""
    docs = tuple(docs)
    facts, members, related, blocked = [], defaultdict(list), defaultdict(list), defaultdict(list)
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
        else:
            for p in (a, b):
                if p:
                    members[p[0]].append((p[1], p[1], p[2], p[2], cite))
        if a and b and (a[0] != b[0] or a[1] > b[1]):
            reason = "different prefixes" if a[0] != b[0] else "reversed range"
            for prefix in {a[0], b[0]}:
                blocked[prefix].append((cite, reason))
    folds = defaultdict(list)
    for prefix in members:
        folds[prefix.casefold()].append(prefix)
    for start, end, d, cite in rows:
        a, b = _related(start), _related(end or start)
        targets = {p for r in (a, b) if r for p in folds[r[0]]}
        if not targets or not _plain(start) or (end is not None and not _plain(end)):
            emit("not_computed", "", None, None, f"Bates labels not read: {start}" +
                 (f" to {end}" if end else ""), [cite], "non-plain label" if targets else "no plain series")
        for prefix in targets:
            numbers = [r[1] for r in (a, b) if r and r[0] == prefix.casefold()]
            lo, hi = min(numbers), max(numbers)
            if a and b and (a[0] != b[0] or a[1] > b[1]):
                blocked[prefix].append((cite, "different prefixes" if a[0] != b[0] else "reversed range"))
            if end is None and _plain(start) and d.page_count != 1:
                hi = lo + d.page_count - 1 if d.page_count and d.page_count > 0 else None
            plain_a, plain_b = _plain(start), _plain(end or start)
            uncertain = not (plain_a and plain_b and plain_a[0] == plain_b[0] == prefix and hi is not None and
                             lo == plain_a[1] and hi == plain_b[1])
            related[prefix].append((lo, hi, cite, uncertain))
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

        touches = [(lo, last if hi is None else hi, c) for lo, hi, c, uncertain in related[prefix] if uncertain]
        outside = [c for lo, hi, c, _ in related[prefix] if lo < first or (hi is not None and hi > last)]
        if outside:
            emit("not_computed", prefix, None, None,
                 f"Bates series {prefix}: gaps outside {label(first)} to {label(last)} not computed",
                 outside, "related label outside span")
        right, right_ids, right_widths = first - 1, set(), set()
        for lo, group in groupby(spans, key=lambda s: s[0]):
            group = list(group)
            left_ids, left_widths = {s[4] for s in group}, {s[2] for s in group}
            if right_ids and lo > right + 1:
                causes = {c for a, b, c in touches if a < lo and b > right}
                reasons = (["related label or page count touches gap"] if causes else [])
                if len(left_widths | right_widths) != 1:
                    reasons.append("bracketing digit widths differ")
                if incomplete:
                    reasons.append("unlabeled or incomplete document")
                ids = right_ids | left_ids | causes | incomplete
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
             [s[4] for s in spans] + [c for _, _, c, _ in related[prefix]])
    return tuple(facts)
