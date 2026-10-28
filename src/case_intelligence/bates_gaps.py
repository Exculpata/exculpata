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


def _digitless_may_name(label, prefix):
    folded = unicodedata.normalize("NFKC", label).casefold()
    return not re.search(r"[0-9]", folded) and prefix.casefold() in re.sub(r"[^a-z]", "", folded)


def _reach(label):
    digits = re.sub(r"[^0-9]", "", unicodedata.normalize("NFKC", label))
    if not digits or len(digits) > 24:
        return None
    return {int(digits[i:j]) for i in range(len(digits))
            for j in range(i + 1, min(len(digits), i + 18) + 1)}


def compute_bates_gaps(docs: Iterable[BatesDocument]) -> tuple[GapFact, ...]:
    """Return gap runs with source/version citations; never truncate facts."""
    docs = tuple(docs)
    facts, members, blocked = [], defaultdict(list), defaultdict(list)
    touches, namers = defaultdict(list), defaultdict(set)
    reaches = defaultdict(list)
    incomplete = {(d.document_id, d.version_id) for d in docs
                  if not d.labels or not d.labels_complete or
                  any(label in {"", "N/A"} for pair in d.labels for label in pair)}
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
            if any(_may_name(label, prefix) or _digitless_may_name(label, prefix)
                   for label in (start, end or start)):
                namers[prefix].add(cite)
                values = [{p[1]} if end is not None and (p := _plain(label)) else _reach(label)
                          for label in (start, end or start)]
                count = d.page_count
                known = count is not None and count > 0
                narrow = re.fullmatch(re.escape(prefix) + r" ?([0-9]+)-([0-9]+)",
                                      unicodedata.normalize("NFKC", start))
                if any(v is None for v in values):
                    reaches[prefix].append((-1, None, cite))
                elif end is not None or (narrow and len(narrow[1]) == len(narrow[2]) ==
                                        max(max(s[2:4]) for s in members[prefix]) and int(narrow[1]) <= int(narrow[2])):
                    numbers = values[0] | values[1] if end is not None else {int(narrow[1]), int(narrow[2])}
                    lower = min(numbers)
                    upper = max(numbers) + count - 1 if known else None
                    reaches[prefix].append((lower, upper, cite))
                else:
                    reaches[prefix].extend((n, n + count - 1 if known else None, cite) for n in values[0])
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

        intervals = [(a, last if b is None else b, c) for a, b, c in reaches[prefix]]
        outside = {c for a, b, c in intervals if a < first or a > last or b > last}
        outside |= {c for _, hi, c in touches[prefix] if hi is not None and hi > last}
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
                if len(left_widths | right_widths) != 1:
                    reasons.append("bracketing digit widths differ")
                if incomplete:
                    reasons.append("unlabeled or incomplete document")
                ids = right_ids | left_ids | causes | incomplete
                widths = left_widths | right_widths
                gap_width = next(iter(widths)) if len(widths) == 1 else width
                covering = [(max(a, right + 1), min(b, lo - 1), c)
                            for a, b, c in intervals if a < lo and b > right]
                cuts = sorted({right + 1, lo} | {n for a, b, _ in covering for n in (a, b + 1)})
                pieces = [(a, b - 1, frozenset(c for x, y, c in covering if x <= a <= y))
                          for a, b in zip(cuts, cuts[1:])]
                for reached, runs in groupby(pieces, key=lambda p: p[2]):
                    runs = list(runs)
                    lower, upper = runs[0][0], runs[-1][1]
                    run_reasons = reasons.copy()
                    if reached:
                        run_reasons.insert(bool(causes), "label may name series")
                    text = label(lower, gap_width) + (f" to {label(upper, gap_width)}" if upper > lower else "")
                    reason = "; ".join(run_reasons)
                    emit("possible" if reason else "missing", prefix, lower, upper,
                         ("Possible missing Bates " if reason else "Missing Bates ") + text +
                         (f" ({reason})" if reason else ""), ids | reached, reason)
            for _, hi, _, end_width, cite in group:
                if hi > right:
                    right, right_ids, right_widths = hi, {cite}, {end_width}
                elif hi == right:
                    right_ids.add(cite)
                    right_widths.add(end_width)
        emit("computed", prefix, None, None, f"Bates series {prefix}: computed",
             [s[4] for s in spans] + sorted(namers[prefix]))
    return tuple(facts)
