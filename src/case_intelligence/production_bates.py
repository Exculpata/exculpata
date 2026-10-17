"""Parse explicit Bates labels and sweep intervals without expanding page ranges."""
from collections import defaultdict, OrderedDict
from dataclasses import dataclass
import re
from itertools import islice

MAX_BATES_ROWS = 100
MAX_BATES_LINKS = 20


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


def bates_gaps(records, *, row_limit=MAX_BATES_ROWS, link_limit=MAX_BATES_LINKS, caps=None):
    """Records contain bates_start/end and link; invalid ranges stay visible."""
    if row_limit < 1 or link_limit < 1:
        raise ValueError('Bates output limits must be positive.')
    groups, invalid = defaultdict(list), []
    totals = dict(bates_missing=0, bates_duplicates=0, bates_invalid=0)
    omitted_links = 0
    shown_links = 0
    seen = set()
    for record in records:
        start = parse_bates(record.get('bates_start', ''))
        end = parse_bates(record.get('bates_end') or record.get('bates_start', ''))
        if not start or not end or start.prefix != end.prefix or start.number > end.number:
            totals['bates_invalid'] += 1
            if len(invalid) < row_limit:
                invalid.append(record)
            continue
        identity = record.get('identity')
        if identity is not None:
            key = (identity, start.prefix, start.number, end.number)
            if key in seen:
                continue
            seen.add(key)
        groups[start.prefix].append((start.number, end.number, max(start.width, end.width), record['link']))
    missing, duplicates = [], []
    for prefix, ranges in sorted(groups.items()):
        width = max(row[2] for row in ranges)
        events = defaultdict(list)
        for index, (start, end, _, link) in enumerate(ranges):
            events[start].append((index, link, True))
            events[end + 1].append((index, link, False))
        active, previous, previous_links, previous_total = OrderedDict(), None, [], 0
        for position, changes in sorted(events.items()):
            if previous is not None and position > previous:
                item = dict(prefix=prefix, start=str(previous).zfill(width), end=str(position - 1).zfill(width))
                if not active:
                    totals['bates_missing'] += 1
                    if len(missing) < row_limit:
                        # Both boundary samples are bounded before copying or sorting.
                        following = [row[1] for row in islice((r for r in changes if r[2]), link_limit)]
                        links = sorted(set(previous_links + following))[:link_limit]
                        omitted = max(0, previous_total + sum(row[2] for row in changes) - len(links))
                        value = dict(item, links=links)
                        if omitted:
                            value['links_omitted'] = omitted
                        omitted_links += omitted
                        shown_links += len(links)
                        missing.append(value)
                elif len(active) > 1:
                    totals['bates_duplicates'] += 1
                    if len(duplicates) < row_limit:
                        links = sorted(set(islice(active.values(), link_limit)))
                        omitted = len(active) - len(links)
                        omitted_links += omitted
                        shown_links += len(links)
                        value = dict(item, occurrences=len(active), links=links)
                        if omitted:
                            value['links_omitted'] = omitted
                        duplicates.append(value)
            if active:
                previous_links = list(islice(active.values(), link_limit))
                previous_total = len(active)
            for index, link, adding in changes:
                if adding:
                    active[index] = link
                else:
                    active.pop(index)
            previous = position
    result = dict(bates_missing=missing, bates_duplicates=duplicates, bates_invalid=invalid)
    if caps is not None:
        caps.update({key: dict(total=total, shown=len(result[key]), omitted=total-len(result[key]))
                     for key, total in totals.items()})
        caps['bates_support_references'] = dict(total=shown_links+omitted_links,
            shown=shown_links, omitted=omitted_links)
    return result
