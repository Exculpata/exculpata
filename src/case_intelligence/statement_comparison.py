"""Bounded deterministic statement comparisons; original text remains the evidence."""
from dataclasses import asdict, dataclass, replace
from itertools import combinations
import re

from .answer_evidence import validated_window_text
from .matter_analysis import _NEGATION, _WORD, _STOP, _signature
from .review_wording import NOTE_TEMPLATES
from .workspace_store import WorkspaceProblem

MAX_STATEMENTS = 1200
MAX_PAIRS = 30000
MAX_CANDIDATES = 150
_FILLER = _STOP | _NEGATION | set('at said stated told recalled remembered repeated reported was were did had has have before after about around account follow up synthetic demonstration record people places events person object date first saw see seeing observed described taking leaving returning could distinguish'.split())
_NUMBERS = dict(zip('zero one two three four five six seven eight nine ten'.split(), map(str, range(11))))
_TIME = re.compile(r'\b(\d{1,2}):(\d{2})(?:\s*([ap])\.?m\.?)?', re.I)
_DATE = re.compile(r'\b\d{4}-\d{2}-\d{2}\b')
_LOCATION = re.compile(r'\b(?:at|inside|outside|near|to)\s+(?:the\s+)?([A-Z][\w-]+(?:\s+[A-Z][\w-]+){0,3}|(?:north|south|east|west)\s+(?:entrance|gate|passage))\b')
_COUNT = re.compile(r'\b(\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten)\s+(cases?|parcels?|boxes|people|vehicles?|cars?|bags?)\b', re.I)
_DESCRIPTION = re.compile(r'\b(red|blue|green|teal|black|white|yellow|sealed|empty|open|closed|damaged|intact)\s+(?:(?:red|blue|green|teal|black|white|yellow)\s+)?(case|parcel|box|car|bag)\b', re.I)


@dataclass(frozen=True)
class Statement:
    statement_id: str
    speaker_entity_id: str | None
    attribution: str
    source_id: str
    locator: str
    text: str
    event_terms: tuple[str, ...]
    citation: dict
    speaker_name: str = ''


@dataclass(frozen=True)
class InconsistencyCandidate:
    signature: str
    scope: str
    difference_type: str
    note: str
    left: dict
    right: dict
    speaker_ids: tuple[str, ...]
    status: str = 'open'
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    revision: int = 0
    kind: str = 'possible_inconsistency'


def temporal_slots(text):
    """Minimal adapter; TODO #270: adopt its date parser once merged and compatible."""
    values = []
    for match in _TIME.finditer(text):
        hour, minute, period = match.groups()
        hour, minute = int(hour), int(minute)
        if minute > 59 or hour > 23 or (period and not 1 <= hour <= 12):
            continue
        if period:
            hour = hour % 12 + (12 if period.lower() == 'p' else 0)
        values.append(('time', f'{hour:02}:{minute:02}', match.group()))
    for match in _DATE.finditer(text):
        from datetime import date
        try:
            date.fromisoformat(match.group())
        except ValueError:
            continue
        values.append(('date', match.group(), match.group()))
    return values


def _sentence_spans(text):
    # Preserve exact original whitespace and offsets, including wrapped lines.
    # A sentence terminator must precede an uppercase word, so p.m. stays intact.
    start = 0
    for separator in re.finditer(r'(?<=[.!?])\s+(?=[A-Z])|\n\s*\n', text):
        yield start, separator.start()
        start = separator.end()
    yield start, len(text)


def attribute_statements(units, entities):
    """Attribute explicit speakers or interview subjects, never a filename guess.

    A synthetic document subject gets a source-declared ID when no curated entity
    exists. Ambiguous aliases abstain instead of merging different identities.
    """
    names = {}
    for entity in entities:
        if entity.get('entity_type', 'person') != 'person':
            continue
        for name in [entity['display_name'], *entity.get('aliases', [])]:
            names.setdefault(name.casefold(), set()).add(entity['entity_id'])
    units = tuple(units)
    document_subjects = {}
    for unit in units:
        subject = re.search(r'^Person:\s*([^\n]+)', unit.text, re.M)
        if subject and re.search(r'account|interview|statement', unit.source_name + '\n' + unit.text[:100], re.I):
            document_subjects.setdefault(unit.document_id, set()).add(subject.group(1).strip())
    result = []
    for unit in units:
        subjects = document_subjects.get(unit.document_id, set())
        document_name = next(iter(subjects)) if len(subjects) == 1 else ''
        for start, end in _sentence_spans(unit.text[:6000]):
            text = unit.text[start:end]
            # Metadata dates and document IDs cannot become event differences.
            if len(text) < 15 or re.search(r'^(?:Record date|Person|ID|Matter reference):', text, re.M):
                continue
            quoted = re.search(r'\b([A-Z][a-zA-Z-]+(?:\s+[A-Z][a-zA-Z-]+){0,2})\s+(?:said|stated|told|recalled|remembered|reported)\b', text)
            name = quoted.group(1) if quoted else document_name
            ids = names.get(name.casefold(), set())
            speaker = (next(iter(ids)) if len(ids) == 1 else
                       'declared-' + _signature(name.casefold())[:24] if name and not ids else None)
            attribution = ('quoted' if quoted else 'document') if speaker else 'none'
            terms = tuple(sorted(set(_WORD.findall(text.casefold())) - _FILLER - set(_WORD.findall(name.casefold()))))
            citation = dict(document_id=unit.document_id, source_version_id=unit.source_version_id,
                chunk_id=unit.chunk_id, source_name=unit.source_name, location=unit.citation,
                unit_number=unit.page_number, excerpt_digest=unit.excerpt_digest, excerpt=text,
                window=dict(start=start, end=end, original_chars=len(unit.text), omitted_chars=len(unit.text) - (end-start)),
                record_date=(re.search(r'^Record date:\s*([^\n]+)', unit.text, re.M).group(1)
                             if re.search(r'^Record date:\s*([^\n]+)', unit.text, re.M) else 'Date not stated'))
            result.append(Statement(_signature(unit.document_id, unit.source_version_id, unit.excerpt_digest, unit.chunk_id, str(start), text),
                speaker, attribution, unit.document_id, unit.citation, text, terms, citation, name))
            if len(result) >= MAX_STATEMENTS:
                return result
    return result


def _slots(statement):
    text = statement.text
    slots = {}
    for kind, value, span in temporal_slots(text):
        slots.setdefault(('time_date', kind), []).append((value, span))
    for match in _LOCATION.finditer(text):
        slots.setdefault(('location', 'place'), []).append((match[1].casefold(), match[1]))
    for match in _COUNT.finditer(text):
        noun = match[2].lower().rstrip('s')
        slots.setdefault(('quantity_count', noun), []).append((_NUMBERS.get(match[1].lower(), match[1]), match.group()))
    for match in _DESCRIPTION.finditer(text):
        group = 'condition' if match[1].lower() in ('sealed', 'empty', 'open', 'closed', 'damaged', 'intact') else 'color'
        slots.setdefault(('object_description', match[2].lower() + ':' + group), []).append((match[1].lower(), match.group()))
    for match in re.finditer(r'\b([A-Z][a-zA-Z-]+(?:\s+[A-Z][a-zA-Z-]+){0,2})\s+(?:was|is|were)\s+(not\s+)?(present|absent|there)\b', text):
        value = 'absent' if match[2] or match[3] == 'absent' else 'present'
        slots.setdefault(('people_present', match[1].casefold()), []).append((value, match.group()))
    return slots


def compare(statements):
    """Pair aligned clauses, with explicit bounded limits and stable signatures."""
    found = {}
    statement_slots = {statement.statement_id: _slots(statement) for statement in statements}
    document_terms = {}
    for statement in statements:
        document_terms.setdefault(statement.source_id, set())
        if statement_slots[statement.statement_id]:
            document_terms[statement.source_id].update(statement.event_terms)
    for index, (left, right) in enumerate(combinations(sorted(statements, key=lambda s: s.statement_id), 2)):
        if index >= MAX_PAIRS or len(found) >= MAX_CANDIDATES:
            break
        if left.source_id == right.source_id or not left.speaker_entity_id or not right.speaker_entity_id:
            continue
        shared = set(left.event_terms) & set(right.event_terms)
        ls, rs = statement_slots[left.statement_id], statement_slots[right.statement_id]
        object_context = (left.speaker_entity_id == right.speaker_entity_id
            and any(key[0] == 'object_description' for key in ls.keys() & rs.keys())
            and len(document_terms[left.source_id] & document_terms[right.source_id]) >= 3)
        if len(shared) < 3 and not object_context:
            continue
        differences = []
        for key in sorted(ls.keys() & rs.keys()):
            a, b = ls[key], rs[key]
            # Multiple alternatives and overlap are not treated as disagreement.
            if len(a) == len(b) == 1 and a[0][0] != b[0][0]:
                differences.append((key[0], a[0][1], b[0][1]))
        lw, rw = set(_WORD.findall(left.text.casefold())), set(_WORD.findall(right.text.casefold()))
        if bool(lw & _NEGATION) != bool(rw & _NEGATION):
            base_l, base_r = lw - _FILLER, rw - _FILLER
            if len(base_l & base_r) / max(len(base_l | base_r), 1) >= .75:
                kind = 'people_present' if re.search(r'\b(?:present|absent|there)\b', left.text + ' ' + right.text, re.I) else 'observed_action'
                if not any(difference[0] == kind for difference in differences):
                    differences.append((kind, left.text, right.text))
        for kind, a, b in differences:
            if len(found) >= MAX_CANDIDATES:
                break
            signature = _signature('possible_inconsistency', kind, left.statement_id, right.statement_id, a, b)
            found[signature] = InconsistencyCandidate(signature,
                'same_witness' if left.speaker_entity_id == right.speaker_entity_id else 'cross_witness',
                kind, NOTE_TEMPLATES[kind].format(left=a, right=b),
                dict(left.citation, highlight=a), dict(right.citation, highlight=b),
                tuple(sorted({left.speaker_entity_id, right.speaker_entity_id})))
    return list(found.values())


def validate_candidates(candidates, resolve):
    """Recheck full original identity before validating each exact excerpt window."""
    accepted, dropped = [], 0
    for candidate in candidates:
        references = []
        try:
            for citation in (candidate.left, candidate.right):
                if not citation or not citation.get('highlight'):
                    raise WorkspaceProblem('Missing original passage.')
                current = resolve(citation)
                if any(current[key] != citation[key] for key in ('document_id', 'source_version_id', 'chunk_id', 'excerpt_digest', 'location', 'source_name', 'unit_number')):
                    raise WorkspaceProblem('Original passage changed.')
                excerpt = validated_window_text(current['excerpt'], citation['window'])
                if excerpt != citation['excerpt'] or citation['highlight'] not in excerpt:
                    raise WorkspaceProblem('Original passage changed.')
                references.append(dict(citation, support_token=current['support_token']))
        except (KeyError, TypeError, ValueError, WorkspaceProblem):
            dropped += 1
            continue
        accepted.append(replace(candidate, left=references[0], right=references[1]))
    return accepted, dropped


def candidate_payload(candidate):
    return asdict(candidate)
