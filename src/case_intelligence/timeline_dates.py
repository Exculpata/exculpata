"""Conservative calendar mentions; no locale, century, or timezone guessing."""
from dataclasses import dataclass, replace
from datetime import date, timedelta
import re

MONTHS = 'January February March April May June July August September October November December'.split()
MONTH = '(?:' + '|'.join(m[:3] + '(?:' + m[3:] + ')?' for m in MONTHS) + ')'
CALENDAR = re.compile(r'\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/(?:\d{4}|\d{2})|' + MONTH + r'\s+\d{1,2}(?:\s*[–—-]\s*\d{1,2})?(?:,?\s+\d{4})?|\d{1,2}\s+' + MONTH + r'(?:\s+\d{4})?)\b', re.I)
TIME = re.compile(r'\b(?:between\s+\d{1,2}(?::\d{2})?\s+and\s+\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?|\d{1,2}:\d{2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?|\d{1,2}\s*[ap]\.?m\.?)', re.I)
MONTH_YEAR = re.compile(r'\b' + MONTH + r'\s+\d{4}\b', re.I)
APPROX_YEAR = re.compile(r'\b(?:about|around|early|late)\s+(\d{4})\b', re.I)
RELATIVE = re.compile(r'\b(?:the\s+)?(?:next\s+(?:morning|day)|previous\s+day|following\s+day|undated|date unknown|no exact time|unknown time|does not establish when)\b', re.I)
APPROX = re.compile(r'\b(?:about|around|near|early|late|approximately)\s*$', re.I)

@dataclass(frozen=True)
class DateMention:
    text: str
    start: int
    end: int
    kind: str
    start_date: str | None
    end_date: str | None
    precision: str


def _calendar(raw):
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
            return date.fromisoformat(raw).isoformat(), None
        if '/' in raw:
            month, day, year = raw.split('/')
            if len(year) != 4:
                return None, None
            return date(int(year), int(month), int(day)).isoformat(), None
        words = re.findall(r'[A-Za-z]+|\d+', raw)
        name = next(w for w in words if w.isalpha())
        month = next(i for i, m in enumerate(MONTHS, 1) if m.lower().startswith(name.lower()))
        numbers = [int(w) for w in words if w.isdigit()]
        if len(str(numbers[-1])) != 4:
            return None, None
        start = date(numbers[-1], month, numbers[0])
        end = date(numbers[-1], month, numbers[1]) if len(numbers) == 3 else None
        if end and end < start:
            raise ValueError('Reversed range')
        return start.isoformat(), end.isoformat() if end else None
    except (ValueError, StopIteration):
        return False, None


def _time(raw):
    meridiem = re.search(r'([ap])\.?m', raw, re.I)
    clock = re.search(r'\d{1,2}(?::\d{2})?(?::\d{2})?', raw).group()
    parts = list(map(int, clock.split(':')))
    hour, minute = parts[0], parts[1] if len(parts) > 1 else 0
    if minute > 59 or (len(parts) > 2 and parts[2] > 59) or hour > (12 if meridiem else 23) or (meridiem and hour == 0):
        return None
    if meridiem:
        hour = hour % 12 + (12 if meridiem[1].lower() == 'p' else 0)
    return f'{hour:02}:{minute:02}', 'minute' if ':' in clock else 'hour'


def _extract_mentions(text):
    """Offsets always address the original text. Unanchored values stay unordered."""
    result = []
    for match in CALENDAR.finditer(text):
        start, end = _calendar(match.group())
        if start is False:
            continue
        prefix = APPROX.search(text[max(0, match.start()-24):match.start()])
        begin = max(0, match.start()-24) + prefix.start() if prefix else match.start()
        kind = 'approximate' if prefix else 'range' if re.search('[–—-]', match.group()) and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', match.group()) else 'exact'
        result.append(DateMention(text[begin:match.end()], begin, match.end(), kind, start, end, 'day'))
    for match in MONTH_YEAR.finditer(text):
        if any(m.start <= match.start() < m.end for m in result):
            continue
        name, year = match.group().split()
        month = next(i for i, m in enumerate(MONTHS, 1) if m.lower().startswith(name.lower()))
        try:
            start = date(int(year), month, 1).isoformat()
        except ValueError:
            continue
        prefix = APPROX.search(text[max(0, match.start()-24):match.start()])
        begin = max(0, match.start()-24) + prefix.start() if prefix else match.start()
        result.append(DateMention(text[begin:match.end()], begin, match.end(), 'approximate', start, None, 'month'))
    for match in APPROX_YEAR.finditer(text):
        try:
            start = date(int(match[1]), 1, 1).isoformat()
        except ValueError:
            continue
        result.append(DateMention(match.group(), match.start(), match.end(), 'approximate', start, None, 'year'))
    for match in TIME.finditer(text):
        if any(m.start <= match.start() < m.end for m in result):
            continue
        parsed = _time(match.group())
        if not parsed:
            continue
        clock, precision = parsed
        end = None
        if match.group().lower().startswith('between'):
            end_value = _time(match.group().split('and', 1)[-1])
            if not end_value or end_value[0] < clock:
                continue
            end = 'T' + end_value[0]
        prefix = APPROX.search(text[max(0, match.start()-24):match.start()])
        begin = max(0, match.start()-24) + prefix.start() if prefix else match.start()
        result.append(DateMention(text[begin:match.end()], begin, match.end(), 'range' if end else 'approximate' if prefix else 'exact', 'T'+clock, end, precision))
    for match in RELATIVE.finditer(text):
        result.append(DateMention(match.group(), match.start(), match.end(), 'unresolved_relative', None, None, 'day'))
    result.sort(key=lambda m: m.start)
    return result


def resolve_mention(mention, anchor):
    if not anchor:
        return mention
    if mention.start_date and mention.start_date.startswith('T'):
        return replace(mention, start_date=anchor+mention.start_date, end_date=anchor+mention.end_date if mention.end_date else None)
    if mention.kind == 'unresolved_relative' and re.search(r'next|following|previous', mention.text, re.I):
        day = date.fromisoformat(anchor) + timedelta(days=-1 if 'previous' in mention.text.lower() else 1)
        return replace(mention, kind='approximate', start_date=day.isoformat())
    return mention


def extract_dates(text):
    """Extract mentions from one document, resolving only an unambiguous anchor."""
    mentions = _extract_mentions(text)
    anchors = {m.start_date for m in mentions if m.kind == 'exact' and m.precision == 'day' and m.start_date}
    anchor = next(iter(anchors)) if len(anchors) == 1 else None
    return [resolve_mention(m, anchor) for m in mentions]
