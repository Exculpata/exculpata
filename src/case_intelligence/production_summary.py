"""Deterministic summary of a caller-authorized production snapshot; no writes."""
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date
import html
import re
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
    return re.sub(r'([\\`*_\[\]])', r'\\\1', html.escape(' '.join(value.split()), quote=False))


@dataclass(frozen=True)
class ProductionSummary:
    name: str
    facts: tuple[SummaryFact, ...]

    def to_markdown(self) -> str:
        lines = ['# Production summary: ' + _md(self.name), '',
                 'Counts cover supplied documents; dates are mentioned ISO dates. '
                 'Gaps describe recorded coverage, not proof of completeness.']
        overview = {'documents', 'pages', 'type', 'date_range', 'undated'}
        people = {'person', 'organization'}
        for title, kinds in (('Overview', overview), ('People and organizations', people),
                             ('Gaps and coverage', {f.kind for f in self.facts} - overview - people)):
            lines += ['', '## ' + title, '']
            selected = [fact for fact in self.facts if fact.kind in kinds]
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


def _bates_facts(ranges):
    groups, facts = defaultdict(list), []
    for start, end, link in ranges:
        first, last = re.fullmatch(r'(.*?)([0-9]{1,18})', start), re.fullmatch(r'(.*?)([0-9]{1,18})', end)
        if (not first or not last or first[1] != last[1] or first[1][-1:].isdigit()
                or int(first[2]) > int(last[2])):
            facts.append(SummaryFact('bates_unordered', f'Bates order unresolved: {start} to {end}', (link,)))
            continue
        groups[first[1]].append((int(first[2]), int(last[2]), len(first[2]), link))
    for prefix, intervals in sorted(groups.items()):
        previous = None
        for start, end, width, link in sorted(intervals, key=lambda item: (item[0], item[1], item[3].document_id)):
            if previous is not None and start > previous[0] + 1:
                lower, upper = previous[0] + 1, start - 1
                label = prefix + str(lower).zfill(previous[1])
                if lower != upper:
                    label += ' to ' + prefix + str(upper).zfill(previous[1])
                facts.append(SummaryFact('bates_gap', 'Missing Bates: ' + label,
                                         (previous[2], link), upper - lower + 1))
            if previous is None or end > previous[0]:
                previous = end, width, link
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
    units_seen = chars_seen = 0
    for source in sources:
        doc = source.document
        if doc.document_id in documents:
            raise ValueError('Duplicate document identity in production')
        if len(documents) >= 10_000:
            raise OverflowError('Production exceeds the complete document read limit')
        if len(ranges) + len(source.locators) > 100_000:
            raise OverflowError('Production exceeds the complete Bates metadata read limit')
        documents[doc.document_id] = doc
        labels = [(loc.bates_start, loc.bates_end or loc.bates_start)
                  for loc in source.locators if loc.bates_start]
        found_dates, found_refs, pages = [], [], set()
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
                for value in sorted(set(re.findall(r'(?<!\w)[0-9]{4}-[0-9]{2}-[0-9]{2}(?!\w)', unit.text))):
                    try:
                        date.fromisoformat(value)
                    except ValueError:
                        continue
                    found_dates.append((value, ordinal, unit.location))
                for line in unit.text.splitlines():
                    if match := re.fullmatch(r'\s*Bates:\s*(\S+)(?: to (\S+))?\s*', line):
                        labels.append((match[1], match[2] or match[1]))
                    if match := re.fullmatch(r'\s*Reference:\s*(\S.*?)\s*', line):
                        found_refs.append((match[1], ordinal, unit.location))
        except UnitRecordLimit as exc:
            raise OverflowError('Production exceeds the derived record read limit') from exc
        except (OSError, ValueError, TypeError, RuntimeError):
            reasons.append('Derived text unavailable')
        if doc.state == 'ready' and not pages:
            reasons.append('No extracted text available')
        labels = sorted(set(labels))
        link = SummaryLink(doc.document_id, doc.version_id,
                           tuple(start if start == end else start + ' to ' + end for start, end in labels),
                           base + '/sources/' + quote(source.action_token, safe=''))
        links[doc.document_id] = link
        notices.extend(SummaryFact('readability', reason, (link,)) for reason in reasons)
        if doc.media_type == 'application/pdf':
            if doc.page_count <= 0:
                notices.append(SummaryFact('page_coverage', 'PDF page count unavailable', (link,)))
            elif doc.state == 'ready':
                missing = doc.page_count - len({page for page in pages if 1 <= page <= doc.page_count})
                if missing:
                    notices.append(SummaryFact('readability', f'PDF pages without extracted text: {missing}', (link,), missing))
        for value, ordinal, location in found_dates:
            dates[value].append(replace(link, href=link.href + '?' + urlencode({'unit': ordinal}), location=location))
        for value, ordinal, location in found_refs:
            references.append((value, replace(link, href=link.href + '?' + urlencode({'unit': ordinal}), location=location)))
        ranges.extend((start, end, link) for start, end in labels)
    facts = [SummaryFact('documents', f'Documents: {len(documents)}', tuple(links.values()), len(documents))]
    pdfs = [doc for doc in documents.values() if doc.media_type == 'application/pdf' and doc.page_count > 0]
    count = sum(doc.page_count for doc in pdfs)
    facts.append(SummaryFact('pages', f'Recorded PDF pages: {count}', tuple(links[d.document_id] for d in pdfs), count))
    types = defaultdict(list)
    for doc in documents.values():
        types[doc.media_type].append(links[doc.document_id])
    facts.extend(SummaryFact('type', f'{kind} documents: {len(refs)}', tuple(refs), len(refs)) for kind, refs in sorted(types.items()))
    if dates:
        first, last = min(dates), max(dates)
        facts.append(SummaryFact('date_range', f'Mentioned ISO date range: {first} to {last}',
                                tuple(link for day in sorted(dates) for link in dates[day])))
    dated = {link.document_id for refs in dates.values() for link in refs}
    undated = tuple(link for key, link in links.items() if key not in dated)
    facts.append(SummaryFact('undated', f'Documents without a valid mentioned ISO date: {len(undated)}', undated, len(undated)))
    facts.extend(notices)
    names = {name.casefold() for doc in documents.values()
             for path in (doc.display_name, doc.relative_path)
             for name in (path, path.rsplit('/', 1)[-1]) if name}
    for value, link in references:
        if value.casefold() not in names:
            facts.append(SummaryFact('absent_reference', 'Referenced file absent from this production: ' + value, (link,)))
    facts.extend(_bates_facts(ranges))
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
