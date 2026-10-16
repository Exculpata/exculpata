"""Bounded, deterministic production accounting with current original support."""
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import PurePosixPath
import re
from urllib.parse import urlencode

from . import production_accounting as accounting
from .intake_receipts import IntakeReceipts
from .production_bates import bates_gaps
from .workspace_store import WorkspaceProblem

FORMAT = 'exculpata-production-summary-v1'
NOTICE = 'Summary of what was received, generated from the files for attorney review.'
MAX_DOCUMENTS = 10_000
MAX_ROWS = 100_000
MAX_CHARACTERS = 20_000_000
MAX_UNITS = 20_000
MAX_DISPLAY = 100
_DATE = re.compile(r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)')
_REFERENCE = re.compile(r'\b(?:see|refer to|referenced in|attached as)\s+((?:Exhibit|Attachment|Record)\s+(?:[A-Za-z]|\d{1,8}))\b|\b((?:Exhibit|Attachment)\s+(?:[A-Z]|\d{1,8}))\b')
_ATTACHMENT = re.compile(r'(?im)\b(?:attachment|attached file)\s*:\s*([^\n;]{1,240}\.[a-z0-9]{1,8})(?:[ \t]+\([^\n)]{1,100}\))?[ \t]*$')
_BATES = re.compile(r'\bBates\s*:\s*([A-Za-z][A-Za-z_-]*\d{1,12})(?:\s*(?:to|–|—|-)\s*([A-Za-z][A-Za-z_-]*\d{1,12}))?', re.I)


@dataclass(frozen=True)
class ProductionSummary:
    format: str
    generated_at: str
    scope: str
    totals: dict
    by_type: list
    date_range: dict
    people: list
    organizations: list
    gaps: dict
    capped: dict
    notice: str
    sources: list
    limits: list

    def to_dict(self):
        return asdict(self)


def detected_type(media_type):
    if media_type == 'application/pdf':
        return 'pdf'
    if media_type == 'message/rfc822':
        return 'email'
    if media_type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
        return 'docx'
    if media_type in ('text/csv', 'text/tab-separated-values', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'):
        return 'table'
    if media_type.startswith(('audio/', 'video/', 'image/')):
        return 'media'
    return 'text' if media_type.startswith('text/') else 'other'


def _bound(count):
    if count > MAX_ROWS:
        raise WorkspaceProblem('Production summary read limit reached; no partial summary was returned.')


def _identifier(value):
    return re.sub(r'[^a-z0-9]', '', value.casefold())


class ProductionSummaryService:
    def __init__(self, bench, matter, actor_id):
        self.bench, self.matter, self.actor_id = bench, matter, actor_id

    def summarize(self, matter_id, *, scope='whole_matter', generated_at=None, inventory=()):
        if matter_id != self.matter.matter_id:
            raise KeyError(matter_id)
        bench, matter, actor = self.bench, self.matter, self.actor_id
        workspace = bench.workspace
        # Resolve the cache before taking either source or workspace locks.
        store = bench.source_store(matter)
        entities = bench.entity_service(matter, store=store)
        with entities.source_guard(), workspace._lock:
            workspace.membership(matter_id, actor)
            if workspace.get_matter_by_id(matter_id).slug != matter.slug:
                raise KeyError(matter.slug)
            revision = workspace.connection.execute('PRAGMA data_version').fetchone()[0]
            result = self._build(store, entities, scope, generated_at, inventory)
            workspace.membership(matter_id, actor)
            if workspace.connection.execute('PRAGMA data_version').fetchone()[0] != revision:
                raise WorkspaceProblem('Sources changed while summarizing. Try again.')
            return result

    def _build(self, store, entities, scope, generated_at, inventory):
        bench, matter, actor = self.bench, self.matter, self.actor_id
        workspace = bench.workspace
        matter_id, base = matter.matter_id, '/matters/' + matter.slug
        receipts = IntakeReceipts(workspace)
        receipt, selected = None, None
        if scope != 'whole_matter':
            receipt = receipts.get(matter_id, actor, scope)
            items = []
            for offset in range(0, receipt['recorded_count'], 100):
                items.extend(receipts.items(matter_id, actor, scope, limit=100, offset=offset))
            if len(items) != receipt['recorded_count']:
                raise WorkspaceProblem('Receipt changed while summarizing. Try again.')
            selected = {row['catalog_document_id'] for row in items if row['catalog_document_id']}
        sources = {}
        documents = {}
        for row in accounting._catalog(workspace, matter_id, bounded=_bound):
            if selected is not None and row['document_id'] not in selected:
                continue
            if len(sources) >= MAX_DOCUMENTS:
                raise WorkspaceProblem('Production exceeds the document read limit; no partial summary was returned.')
            document = store.get(row['document_id'])
            if document.version_id != row['version_id']:
                raise WorkspaceProblem('A source version changed. Try again.')
            documents[document.document_id] = document
            sources[document.document_id] = dict(version_id=row['version_id'], kind=row['kind'],
                folder=row['relative_path'].split('/', 1)[0] if '/' in row['relative_path'] else '',
                link=accounting._link(base, row['display_name'], '/sources/' + row['action_token']),
                tone=row['tone'], state=row['source_state'],
                reason=document.message if row['tone'] != 'ready' and document.message else row['state_label'],
                name=row['relative_path'] or row['display_name'])
        # The shared job read must preserve whole-matter filtering semantics.
        accounting._jobs(workspace, matter_id, sources, bounded=_bound)
        coverage = []
        document_dates, rejected_dates = accounting.document_dates_for(sources, inventory, bounded=_bound)
        unread = accounting._unread(base, matter_id, actor, workspace, receipts, sources,
            workspace.matter_readiness(matter_id), MAX_ROWS, coverage, bounded=_bound,
            receipt_id=scope if receipt else None)
        if unread.omitted:
            raise WorkspaceProblem('Readability accounting exceeds the read limit; no partial summary was returned.')
        url = base + '/production-summary?' + urlencode({'scope': scope})
        rows, dates, references, bates, failures = [], [], [], [], []
        characters = units = 0
        def charge(amount=0):
            nonlocal characters
            characters += amount
            if characters > MAX_CHARACTERS or units > MAX_UNITS:
                raise WorkspaceProblem('Production exceeds the text read limit; no partial summary was returned.')
        for document_id, source in sources.items():
            document = documents[document_id]
            kind = detected_type(document.media_type)
            # TXT page_count counts lines; email/table units are not physical pages.
            pages = document.page_count if kind == 'pdf' else 0
            row = dict(source_id=document_id, name=source['name'], type=kind,
                pages=pages, bytes=document.size, link=source['link'].href, dates=[])
            rows.append(row)
            try:
                for ordinal, unit in enumerate(document.iter_parsed_units(read_check=charge, max_record_chars=1_000_000), 1):
                    units += 1
                    charge(len(unit.text))
                    citation = bench._discovery_reference(matter, document, unit, ordinal)
                    citation['link'] = base + '?' + urlencode({'support': citation['support_token']}) + '#support-pane'
                    for match in _DATE.finditer(unit.text):
                        try:
                            day = date.fromisoformat(match.group()).isoformat()
                        except ValueError:
                            continue
                        row['dates'].append(day)
                        dates.append(dict(value=day, link=citation['link'], source_id=document_id, basis='text'))
                        _bound(len(dates))
                    for pattern in (_REFERENCE, _ATTACHMENT):
                        for match in pattern.finditer(unit.text):
                            reference = next(value for value in match.groups() if value is not None).strip()
                            references.append(dict(reference_text=reference, citation=citation))
                            _bound(len(references))
                    for match in _BATES.finditer(unit.text):
                        bates.append(dict(bates_start=match[1], bates_end=match[2], link=citation['link']))
                        _bound(len(bates))
                    # Extracted source adapters may supply validated locator metadata.
                    for page in getattr(getattr(document, 'locator_metadata', None), 'pages', ()) if ordinal == 1 else ():
                        if page.bates:
                            bates.append(dict(bates_start=page.bates, link=citation['link']))
            except WorkspaceProblem:
                raise
            except (OSError, RuntimeError, ValueError, TypeError):
                failures.append(dict(source_id=document_id, name=source['name'], reason='Extracted text could not be read.', link=source['link'].href, count=1))
            row['dates'] = sorted(set(row['dates']))
        # The briefing and summary use the same provenance/version validation.
        row_by_id = {row['source_id']: row for row in rows}
        for document_id, day in document_dates:
            row = row_by_id[document_id]
            row['dates'].append(day.isoformat())
            dates.append(dict(value=day.isoformat(), link=row['link'], source_id=document_id, basis='document_date'))
        existing = {_identifier(PurePosixPath(row['name']).stem) for row in rows}
        absent, seen = [], set()
        for ref in references:
            identifier = _identifier(PurePosixPath(ref['reference_text']).stem)
            key = (ref['reference_text'].casefold(), ref['citation']['support_token'])
            if identifier not in existing and key not in seen:
                absent.append(dict(ref, reason='referenced, not located in this production'))
                seen.add(key)
        gaps = bates_gaps(bates)
        gaps['referenced_absent'] = absent
        gaps['unreadable'] = [dict(source_id=line.value, name=sources.get(line.value, {}).get('name', line.text),
            reason=line.text, count=line.count, link=line.links[0].href) for line in unread.lines if line.kind != 'empty']
        known_failed = {row['source_id'] for row in gaps['unreadable']}
        gaps['unreadable'].extend(row for row in failures if row['source_id'] not in known_failed)
        people, organizations, excluded_mentions = self._entities(entities, sources, base)
        caps = {}
        for key, values in [('people', people), ('organizations', organizations), *gaps.items()]:
            caps[key] = dict(total=len(values), shown=min(len(values), MAX_DISPLAY), omitted=max(0, len(values) - MAX_DISPLAY))
        date_links = sorted({d['link'] for d in dates})
        caps['date_links'] = dict(total=len(date_links), shown=min(len(date_links), MAX_DISPLAY), omitted=max(0, len(date_links) - MAX_DISPLAY))
        by_type = []
        for kind in ('pdf', 'email', 'docx', 'text', 'table', 'media', 'other'):
            group = [row for row in rows if row['type'] == kind]
            by_type.append(dict(type=kind, documents=len(group), pages=sum(row['pages'] for row in group), link=url + '&type=' + kind + '#summary-sources'))
        limits = ['Counts describe current source versions; repeated receipts do not multiply the same source.',
                  'Page totals count recorded PDF pages only. Text lines, email sections, table rows and media segments are not pages.',
                  'Date ranges combine valid ISO calendar dates stated in extracted text and available semantic document dates; ambiguous dates are not ordered.',
                  'People and organizations count current, non-dismissed recorded mentions. Extraction may not recognize every name.',
                  'Bates checks use explicit Bates labels and available locator metadata. References match exact source filename stems; unmatched references need human review.',
                  f'Read limits: {MAX_DOCUMENTS} documents, {MAX_UNITS} units, {MAX_CHARACTERS} charged characters. Lists display at most {MAX_DISPLAY} rows; caps are reported.',
                  f'{rejected_dates} invalid, stale, conflicting or non-document date records were excluded.',
                  f'{excluded_mentions} retained mentions lacked current source support and were excluded.']
        if receipt:
            limits.append('Receipt scope includes only current source versions bound to this receipt; all non-searchable selection outcomes remain in Gaps.')
        earliest = min((d['value'] for d in dates), default=None)
        latest = max((d['value'] for d in dates), default=None)
        return ProductionSummary(FORMAT, generated_at or datetime.now(timezone.utc).isoformat(), scope,
            dict(documents=len(rows), pages=sum(row['pages'] for row in rows), bytes=sum(row['bytes'] for row in rows), link=url + '#summary-sources'),
            by_type, dict(earliest=earliest, latest=latest, earliest_link=url + '&day=' + (earliest or '') + '#summary-sources',
                latest_link=url + '&day=' + (latest or '') + '#summary-sources',
                undated_count=sum(not row['dates'] for row in rows), link=url + '&undated=1#summary-sources', links=date_links[:MAX_DISPLAY]),
            people[:MAX_DISPLAY], organizations[:MAX_DISPLAY], {key: value[:MAX_DISPLAY] for key, value in gaps.items()}, caps, NOTICE, rows, limits)

    def _entities(self, service, sources, base):
        grouped, excluded = {}, 0
        with service.repository.reading(self.matter.matter_id, self.actor_id) as repo:
            cursor = repo.connection.execute("SELECT m.*,e.display_name,e.entity_type FROM workbench_entity_mention m JOIN workbench_entity e ON e.entity_id=m.entity_id AND e.matter_id=m.matter_id WHERE m.matter_id=? AND m.review_status<>'dismissed' AND e.status<>'dismissed' AND e.entity_type IN ('person','organization') ORDER BY e.entity_id,m.mention_id", (self.matter.matter_id,))
            count = 0
            while batch := cursor.fetchmany(100):
                count += len(batch)
                _bound(count)
                mentions = [dict(row) for row in batch if row['document_id'] in sources]
                available = service.current_sources(mentions)
                for index, mention in enumerate(mentions):
                    if index not in available:
                        excluded += 1
                        continue
                    key = mention['entity_id']
                    link = base + '?' + urlencode({'support': mention['support_token']}) + '#support-pane'
                    item = grouped.setdefault(key, dict(entity_id=key, name=mention['display_name'], type=mention['entity_type'], mentions=0, link=link, links=[]))
                    item['mentions'] += 1
                    if link not in item['links']:
                        item['links'].append(link)
        ranked = sorted(grouped.values(), key=lambda row: (-row['mentions'], row['name'], row['entity_id']))
        return [row for row in ranked if row['type'] == 'person'], [row for row in ranked if row['type'] == 'organization'], excluded
