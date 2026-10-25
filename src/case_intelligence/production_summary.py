"""Deterministic summary of a caller-authorized production snapshot; no writes."""
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date
from itertools import combinations
from typing import Literal
import html
import re
import unicodedata
from urllib.parse import quote, urlencode

from .contracts import SourceLocator
from .pilot_uploads import PilotDocument
from .unit_stream import UnitRecordLimit


@dataclass(frozen=True)
class ProductionSource:
    document: PilotDocument
    action_token: str
    locators: tuple[SourceLocator, ...] = ()


@dataclass(frozen=True)
class SummaryLink:
    document_id: str
    version_id: str
    bates: tuple[str, ...]
    href: str
    location: str = ''
    excerpt: str = ''


@dataclass(frozen=True)
class SummaryFact:
    kind: str
    text: str
    links: tuple[SummaryLink, ...]
    count: int | None = None


def _md(value):
    value = ''.join(f'[U+{ord(char):04X}]' if unicodedata.category(char) in {'Cc', 'Cf', 'Mn'} else char
                    for char in value)
    escaped = re.sub(r'([\\`*_\[\]~#])', r'\\\1', html.escape(' '.join(value.split()), quote=False))
    return re.sub(r'(?i)\b(?:https?|ftp):(?=//)|\bwww\.',
                  lambda match: match[0][:-1] + '\\' + match[0][-1], escaped)


@dataclass(frozen=True)
class ProductionSummary:
    name: str
    facts: tuple[SummaryFact, ...]

    def to_markdown(self) -> str:
        lines = ['# Production summary: ' + _md(self.name), '',
                 'Counts cover supplied documents; dates are mentioned ISO dates. '
                 'Gaps are intervals between recorded Bates labels in computed series. '
                 'Review series status and coverage notices. '
                 'An empty gap list does not establish completeness.']
        overview = {'documents', 'pages', 'type', 'date_range', 'undated'}
        people = {'person', 'organization'}
        for title, kinds in (('Overview', overview), ('People and organizations', people),
                             ('Gaps and coverage', {f.kind for f in self.facts} - overview - people)):
            lines += ['', '## ' + title, '']
            selected = [fact for fact in self.facts if fact.kind in kinds]
            if title == 'Gaps and coverage':
                selected.sort(key=lambda fact: fact.kind != 'bates_series')
            for fact in selected:
                links = []
                for link in fact.links:
                    label = f'{link.document_id} (version {link.version_id or "unavailable"}); Bates '
                    label += ', '.join(link.bates) or 'unavailable'
                    if link.location:
                        label += '; ' + link.location
                    rendered = f'[{_md(label)}]({link.href})'
                    links.append(rendered + (' “' + _md(link.excerpt) + '”' if link.excerpt else ''))
                lines.append('- ' + _md(fact.text))
                if links:
                    lines.append('  Sources: ' + '; '.join(links))
            if not selected:
                lines.append('No entries recorded in the supplied inventory.')
        return '\n'.join(lines) + '\n'


_BATES_FACT_LIMIT = 100_000


@dataclass(frozen=True)
class ParsedBates:
    prefix: str
    lower: int
    upper: int
    width: int


@dataclass(frozen=True)
class UnresolvedBates:
    reason: str
    blocks: frozenset[str] | Literal['ALL']


@dataclass(frozen=True)
class BatesSeries:
    prefix: str
    intervals: tuple[tuple[int, int, int, SummaryLink], ...]
    reasons: tuple[str, ...]
    links: tuple[SummaryLink, ...]
    start_only: tuple[SummaryLink, ...]

    @property
    def state(self) -> Literal['computed', 'not_computed']:
        return 'not_computed' if self.reasons else 'computed'


def _normalize_bates(start, end):
    if any(unicodedata.category(char) in {'Cc', 'Cf', 'Mn'} for char in start + end):
        return UnresolvedBates('control, format or combining character', 'ALL')
    start, end = (unicodedata.normalize('NFKC', value).strip() for value in (start, end))
    if any(char.isalpha() and not unicodedata.name(char, '').startswith('LATIN ')
           for char in start + end):
        return UnresolvedBates('non-Latin or mixed-script prefix', 'ALL')
    first, last = (re.fullmatch(r'(.*?)([0-9]+)', value) for value in (start, end))
    blocks = frozenset(match[1] for match in (first, last) if match)
    if not first or not last:
        return UnresolvedBates('nonnumeric label', blocks)
    if first[1] != last[1]:
        return UnresolvedBates('cross-prefix range', blocks)
    if first[1][-1:].isdigit() or any(len(match[2]) > 18 for match in (first, last)):
        return UnresolvedBates('numeric suffix or prefix cannot be ordered', blocks)
    if int(first[2]) > int(last[2]):
        return UnresolvedBates('reversed range', blocks)
    width = max(len(match[2]) if match[2].startswith('0') else 1 for match in (first, last))
    return ParsedBates(first[1], int(first[2]), int(last[2]), width)


def _unique_links(links):
    return tuple({link.document_id: link for link in links}.values())


def _bates_facts(ranges, *, unlabeled=(), start_only=()):
    groups, unresolved, unordered, facts = defaultdict(list), [], defaultdict(list), []

    def emit(kind, text, links, count=None):
        if len(facts) >= _BATES_FACT_LIMIT:
            raise OverflowError('Production exceeds the complete Bates finding limit')
        facts.append(SummaryFact(kind, text, tuple(links), count))

    for start, end, link in ranges:
        parsed = _normalize_bates(start, end)
        if isinstance(parsed, UnresolvedBates):
            unresolved.append((parsed, start, end, link))
            normalized = tuple(unicodedata.normalize('NFKC', value).strip() for value in (start, end))
            unordered[normalized].append(link)
        else:
            groups[parsed.prefix].append((parsed.lower, parsed.upper, parsed.width, link))
    blocked, global_causes = defaultdict(list), []
    for parsed, _, _, link in unresolved:
        if parsed.blocks == 'ALL':
            global_causes.append((parsed.reason, link))
        else:
            for prefix in parsed.blocks:
                blocked[prefix].append((parsed.reason, link))
    if global_causes:
        reasons = '; '.join(sorted({reason for reason, _ in global_causes}))
        emit('bates_series', f'Bates series ALL: not computed ({reasons})',
             _unique_links(link for _, link in global_causes))
    series = []
    for prefix in sorted(groups.keys() | blocked.keys()):
        intervals = tuple(groups[prefix])
        causes = global_causes + blocked[prefix]
        local_refs = _unique_links([item[3] for item in intervals] + [link for _, link in blocked[prefix]])
        refs = _unique_links(local_refs + tuple(link for _, link in global_causes))
        starts = tuple(link for link in local_refs if link.document_id in start_only)
        item = BatesSeries(prefix, intervals, tuple(sorted({reason for reason, _ in causes})), refs, starts)
        series.append(item)
        details = list(item.reasons)
        if starts:
            details.append(f'{len(starts)} documents have a start label only')
        text = f'Bates series {prefix}: ' + item.state.replace('_', ' ')
        emit('bates_series', text + (' (' + '; '.join(details) + ')' if details else ''), refs)
    similar = defaultdict(list)
    for item in series:
        similar[''.join(char for char in item.prefix.casefold() if char.isalnum())].append(item)
    for items in similar.values():
        for first, last in combinations(items, 2):
            emit('bates_series', f'Series {first.prefix} and {last.prefix} differ only by case or punctuation',
                 _unique_links(first.links + last.links))
    if unlabeled:
        emit('bates_coverage', f'Documents without a Bates locator: {len(unlabeled)}', unlabeled, len(unlabeled))
    if start_only:
        emit('bates_coverage', f'Documents with a start label only: {len(start_only)}',
             start_only.values(), len(start_only))
    for item in series:
        if item.state != 'computed':
            continue
        prefix, intervals = item.prefix, item.intervals
        width = max(item[2] for item in intervals)
        by_range, shared = defaultdict(dict), defaultdict(dict)
        for start, end, _, link in intervals:
            by_range[start, end][link.document_id] = link
        previous, active = None, []
        for (start, end), refs in sorted(by_range.items()):
            if len(refs) > 1:
                shared[start, end].update(refs)
            active = [(stop, prior) for stop, prior in active if stop >= start]
            for stop, prior in active:
                combined = prior | refs
                if len(combined) > 1:
                    shared[start, min(stop, end)].update(combined)
                if len(facts) + len(shared) > _BATES_FACT_LIMIT:
                    raise OverflowError('Production exceeds the complete Bates finding limit')
            active.append((end, refs))
            link = next((ref for ref in refs.values() if ref.document_id in start_only), next(iter(refs.values())))
            if previous is not None and start > previous[0] + 1:
                lower, upper = previous[0] + 1, start - 1
                label = prefix + str(lower).zfill(width)
                if lower != upper:
                    label += ' to ' + prefix + str(upper).zfill(width)
                if any(ref.document_id in start_only for ref in (previous[1], link)):
                    label += ' (covered documents may hold unrecorded labels)'
                emit('bates_gap', 'Missing Bates: ' + label, (previous[1], link), upper - lower + 1)
            if (previous is None or end > previous[0]
                    or (end == previous[0] and link.document_id in start_only)):
                previous = end, link
        for (start, end), refs in sorted(shared.items()):
            label = prefix + str(start).zfill(width)
            if end != start:
                label += ' to ' + prefix + str(end).zfill(width)
            emit('bates_overlap', 'Overlapping Bates labels: ' + label, refs.values(), len(refs))
    for parsed, start, end, link in unresolved:
        emit('bates_unordered', f'Bates order unresolved ({parsed.reason}): {start} to {end}', (link,))
    for (start, end), refs in sorted(unordered.items()):
        if len({link.document_id for link in refs}) > 1:
            emit('bates_overlap', f'Duplicate Bates labels: {start} to {end}', refs)
    return facts


def build_summary(name: str, matter_slug: str, sources: Iterable[ProductionSource], *, entities=()) -> ProductionSummary:
    """Use the complete selected PilotDocument inventory, including failed files.

    Caller holds the authorized matter's source guard throughout. Optional entity
    pairs are (entity, mentions) from EntityService.detail, including availability.
    Locators must describe this document version. No membership or origin reads
    happen here; see docs/PRODUCTION_SUMMARY.md for the integration contract.
    """
    base = '/matters/' + quote(matter_slug, safe='')
    documents, links, dates, references, ranges, notices = {}, {}, defaultdict(list), [], [], []
    incomplete, unlabeled, start_only = set(), [], {}
    units_seen = chars_seen = labels_seen = 0
    for source in sources:
        doc = source.document
        if doc.document_id in documents:
            raise ValueError('Duplicate document identity in production')
        if len(documents) >= 10_000:
            raise OverflowError('Production exceeds the complete document read limit')
        documents[doc.document_id] = doc
        labels = [(loc.bates_start, loc.bates_end or loc.bates_start)
                  for loc in source.locators if loc.bates_start]
        labels_seen += len(labels)
        if labels_seen > 100_000:
            raise OverflowError('Production exceeds the complete Bates metadata read limit')
        found_dates, found_refs, cited_bates, pages = [], [], [], set()
        reasons = []
        if doc.state != 'ready':
            reasons.append('Source state: ' + doc.state + ('; ' + doc.message if doc.message else ''))
        elif doc.message:
            reasons.append('Extraction notice: ' + doc.message)
        try:
            for ordinal, unit in enumerate(doc.iter_parsed_units(max_record_chars=2_000_000), 1):
                units_seen += 1
                chars_seen += len(unit.text)
                if units_seen > 20_000 or chars_seen > 20_000_000:
                    raise OverflowError('Production exceeds the complete extracted text read limit')
                if unit.text.strip():
                    pages.add(unit.number)
                for line in unit.text.splitlines():
                    if match := re.fullmatch(r'\s*Bates:\s*(\S+)(?: to (\S+))?\s*', line):
                        cited_bates.append((match[1] + (' to ' + match[2] if match[2] else ''), ordinal, unit.location))
                        continue
                    if match := re.fullmatch(r'\s*Reference:\s*(\S.*?)\s*', line):
                        found_refs.append((match[1], ordinal, unit.location))
                        continue
                    for value in sorted(set(re.findall(r'(?<![\w/\\-])[0-9]{4}-[0-9]{2}-[0-9]{2}(?![\w/\\-])', line))):
                        try:
                            date.fromisoformat(value)
                        except ValueError:
                            continue
                        found_dates.append((value, ordinal, unit.location))
        except UnitRecordLimit as exc:
            raise OverflowError('Production exceeds the derived record read limit') from exc
        except (OSError, ValueError, TypeError, RuntimeError):
            incomplete.add(doc.document_id)
            found_dates, found_refs, cited_bates = [], [], []
            reasons.append('Derived text unavailable; facts incomplete; page coverage unresolved')
        if doc.state == 'ready' and not pages and doc.document_id not in incomplete:
            reasons.append('No extracted text available')
        labels = sorted(set(labels))
        link = SummaryLink(doc.document_id, doc.version_id,
                           tuple(start if start == end else start + ' to ' + end for start, end in labels),
                           base + '/sources/' + quote(source.action_token, safe=''))
        links[doc.document_id] = link
        if not labels:
            unlabeled.append(link)
        if doc.page_count > 1 and any(loc.bates_start and not loc.bates_end for loc in source.locators):
            start_only[doc.document_id] = link
        notices.extend(SummaryFact('readability', reason, (link,)) for reason in reasons)
        if doc.media_type == 'application/pdf':
            if doc.page_count <= 0:
                notices.append(SummaryFact('page_coverage', 'PDF page count unavailable', (link,)))
            elif doc.state == 'ready' and doc.document_id not in incomplete:
                missing = doc.page_count - len({page for page in pages if 1 <= page <= doc.page_count})
                if missing:
                    notices.append(SummaryFact('readability', f'PDF pages without extracted text: {missing}', (link,), missing))
        for value, ordinal, location in found_dates:
            dates[value].append(replace(link, href=link.href + '?' + urlencode({'unit': ordinal}), location=location))
        for value, ordinal, location in found_refs:
            references.append((value, replace(link, href=link.href + '?' + urlencode({'unit': ordinal}), location=location)))
        for value, ordinal, location in cited_bates:
            notices.append(SummaryFact('bates_reference', 'Cited Bates: ' + value,
                (replace(link, href=link.href + '?' + urlencode({'unit': ordinal}), location=location),)))
        ranges.extend((start, end, link) for start, end in labels)
    facts = [SummaryFact('documents', f'Documents: {len(documents)}', tuple(links.values()), len(documents))]
    qualifier = ' (incomplete; excludes unavailable derived text)' if incomplete else ''
    pdfs = [doc for doc in documents.values() if doc.media_type == 'application/pdf' and doc.page_count > 0
            and doc.document_id not in incomplete]
    count = sum(doc.page_count for doc in pdfs)
    facts.append(SummaryFact('pages', f'Recorded PDF pages{qualifier}: {count}', tuple(links[d.document_id] for d in pdfs), count))
    types = defaultdict(list)
    for doc in documents.values():
        types[doc.media_type].append(links[doc.document_id])
    facts.extend(SummaryFact('type', f'{kind} documents: {len(refs)}', tuple(refs), len(refs)) for kind, refs in sorted(types.items()))
    if dates:
        first, last = min(dates), max(dates)
        facts.append(SummaryFact('date_range', f'Mentioned ISO date range{qualifier}: {first} to {last}',
                                tuple(link for day in sorted(dates) for link in dates[day])))
    dated = {link.document_id for refs in dates.values() for link in refs}
    undated = tuple(link for key, link in links.items() if key not in dated and key not in incomplete)
    facts.append(SummaryFact('undated', f'Documents without a valid mentioned ISO date{qualifier}: {len(undated)}', undated, len(undated)))
    facts.extend(notices)
    names = {name.casefold() for doc in documents.values()
             for path in (doc.display_name, doc.relative_path)
             for name in (path, path.rsplit('/', 1)[-1]) if name}
    for value, link in references:
        if value.casefold() not in names:
            facts.append(SummaryFact('absent_reference', 'Referenced file absent from this production: ' + value, (link,)))
    facts.extend(_bates_facts(ranges, unlabeled=unlabeled, start_only=start_only))
    mention_count = 0
    for entity_index, (entity, mentions) in enumerate(entities, 1):
        if entity_index > 10_000:
            raise OverflowError('Production exceeds the complete entity read limit')
        if entity['entity_type'] not in {'person', 'organization'} or entity['status'] == 'dismissed':
            continue
        supported, unavailable = [], []
        for mention in mentions:
            mention_count += 1
            if mention_count > 100_000:
                raise OverflowError('Production exceeds the complete entity mention read limit')
            key = mention['document_id']
            if key not in documents or mention.get('review_status') == 'dismissed':
                continue
            if not mention['available'] or mention['source_version_id'] != documents[key].version_id:
                unavailable.append(links[key])
                continue
            supported.append(replace(links[key], href=base + '?' + urlencode({'support': mention['support_token']}) + '#support-pane',
                                     location=mention['location'], excerpt=mention['excerpt']))
        if supported:
            kind = entity['entity_type']
            facts.append(SummaryFact(kind, f'{kind}: {entity["display_name"]} ({entity["status"]}); recorded mentions: {len(supported)}',
                                     tuple(supported), len(supported)))
        if unavailable:
            facts.append(SummaryFact('entity_unavailable', f'{entity["display_name"]}: unavailable recorded mentions: {len(unavailable)}',
                                     tuple(unavailable), len(unavailable)))
    return ProductionSummary(name, tuple(facts))
