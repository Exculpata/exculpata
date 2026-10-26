"""Naive red-first stub for 378a; replaced by the next implementation commit."""
from collections import defaultdict
from dataclasses import dataclass
import re
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


def compute_bates_gaps(docs: Iterable[BatesDocument]) -> tuple[GapFact, ...]:
    facts, members = [], defaultdict(list)

    def emit(kind, series, lower, upper, text, ids):
        if len(facts) >= 100000:
            raise OverflowError("Bates fact limit exceeded (100000)")
        facts.append(GapFact(kind, series, lower, upper, text, tuple(sorted(set(ids))), ""))

    for doc in docs:
        cite = (doc.document_id, doc.version_id)
        for start, end in doc.labels:
            a, b = (re.fullmatch(r"(.*?)([0-9]+)$", label) for label in (start, end or start))
            if a and b and a[1] == b[1]:
                lo, hi = sorted((int(a[2]), int(b[2])))
                members[a[1]].append((lo, hi, max(len(a[2]), len(b[2])), cite))
            else:
                for prefix in sorted({p[1] for p in (a, b) if p} or {""}):
                    emit("not_computed", prefix, None, None, f"Bates labels not read: {start}", [cite])
                if a and not b:
                    members[a[1]].append((int(a[2]), int(a[2]), len(a[2]), cite))
    for prefix, spans in sorted(members.items()):
        spans.sort()
        width = max(s[2] for s in spans)
        right, ids = spans[0][0] - 1, set()
        for lo, hi, _, cite in spans:
            if ids and lo > right + 1:
                label = prefix + str(right + 1).zfill(width)
                if lo > right + 2:
                    label += " to " + prefix + str(lo - 1).zfill(width)
                emit("missing", prefix, right + 1, lo - 1, "Missing Bates " + label, ids | {cite})
            if hi > right:
                right, ids = hi, {cite}
            elif hi == right:
                ids.add(cite)
        emit("computed", prefix, None, None, f"Bates series {prefix}: computed", [s[3] for s in spans])
    return tuple(facts)
