"""Parse explicit Bates labels and sweep intervals without expanding page ranges."""
from collections import defaultdict
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class BatesNumber:
    prefix: str
    number: int
    width: int


def parse_bates(value: str) -> BatesNumber | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    match = re.fullmatch(r'([A-Za-z]+(?:[_-][A-Za-z]+)*)-?(\d{1,12})', value.strip())
    if not match:
        return None
    return BatesNumber(match[1].rstrip('-').upper(), int(match[2]), len(match[2]))


def bates_gaps(records):
    """Records contain bates_start/end and link; invalid ranges stay visible."""
    groups, invalid = defaultdict(list), []
    for record in records:
        start = parse_bates(record.get('bates_start', ''))
        end = parse_bates(record.get('bates_end') or record.get('bates_start', ''))
        if not start or not end or start.prefix != end.prefix or start.number > end.number:
            invalid.append(record)
            continue
        groups[start.prefix].append((start.number, end.number, max(start.width, end.width), record['link']))
    missing, duplicates = [], []
    for prefix, ranges in sorted(groups.items()):
        width = max(row[2] for row in ranges)
        events = defaultdict(list)
        for index, (start, end, _, link) in enumerate(ranges):
            events[start].append((index, link, True))
            events[end + 1].append((index, link, False))
        active, previous, previous_links = {}, None, []
        for position, changes in sorted(events.items()):
            if previous is not None and position > previous:
                item = dict(prefix=prefix, start=str(previous).zfill(width), end=str(position - 1).zfill(width))
                if not active:
                    missing.append(dict(item, links=sorted(set(previous_links + [row[1] for row in changes if row[2]]))))
                elif len(active) > 1:
                    duplicates.append(dict(item, occurrences=len(active), links=sorted(set(active.values()))))
            previous_links = list(active.values()) or previous_links
            for index, link, adding in changes:
                if adding:
                    active[index] = link
                else:
                    active.pop(index)
            previous = position
    return dict(bates_missing=missing, bates_duplicates=duplicates, bates_invalid=invalid)
