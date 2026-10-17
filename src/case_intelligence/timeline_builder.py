"""Bounded, source-grounded draft timelines without a model or persisted state."""
from dataclasses import asdict, dataclass
import hashlib
import json
import re
from collections import defaultdict

from .answer_evidence import validated_window_text
from .timeline_dates import RELATIVE, _extract_mentions, mention_span, resolve_in_context
from .workspace_store import WorkspaceProblem

NOTICE = 'Draft timeline generated from the records for attorney review. Entries are not findings.'

@dataclass(frozen=True)
class TimelineLimits:
    units: int = 2500
    entries: int = 150
    unit_chars: int = 6000

@dataclass(frozen=True)
class TimelineResult:
    format: str
    generated_at: str | None
    source_count: int
    unit_count: int
    capped: bool
    entries: list
    conflicts: list
    undated: list
    notice: str
    rejected_count: int

    def payload(self):
        return asdict(self)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]


def _span(text, mention):
    # Sentence boundaries exclude decimal/time abbreviations and retain wrapped lines.
    return mention_span(text, mention)


def _citation(unit, start, end, token):
    window = dict(start=start, end=end, original_chars=len(unit.text), omitted_chars=len(unit.text)-(end-start))
    excerpt = validated_window_text(unit.text, window)
    locator = {'page': unit.page_number, 'character_start': start, 'character_end': end}
    if unit.line_start is not None:
        locator['timestamp_start_ms' if unit.evidence_kind == 'transcript' else 'line_start'] = unit.line_start
    return dict(source_id=unit.document_id, document_title=unit.source_name, locator=locator,
                location=unit.citation, excerpt=excerpt, source_version_id=unit.source_version_id,
                excerpt_digest=unit.excerpt_digest, support_token=token(unit), window=window)


def build_timeline(units, *, limits=TimelineLimits(), support_token=lambda unit: ''):
    if not (1 <= limits.units <= 2500 and 1 <= limits.entries <= 150 and 1 <= limits.unit_chars <= 6000):
        raise ValueError('Timeline limits must remain within the bounded scan ceilings')
    selected = sorted(units, key=lambda u: (u.document_id, u.page_number, u.chunk_id))[:limits.units]
    if len({u.matter_id for u in selected}) > 1:
        raise ValueError('A draft timeline belongs to one matter')
    capped = len(units) > len(selected)
    anchors = defaultdict(list)
    scanned = []
    def parsed_documents():
        # Parse a whole document for its anchors, then stop before opening the
        # next document once the output budget is exhausted.
        from itertools import groupby
        for document_id, group in groupby(selected, key=lambda u: u.document_id):
            parsed = [(u, _extract_mentions(u.text[:limits.unit_chars])) for u in group]
            scanned.extend(u for u, _ in parsed)
            for unit, mentions in parsed:
                for mention in mentions:
                    if mention.start_date and re.fullmatch(r'\d{4}-\d{2}-\d{2}', mention.start_date) and mention.kind == 'exact':
                        anchors[document_id].append((unit, mention))
            yield from parsed
    capped |= any(len(u.text) > limits.unit_chars for u in selected)
    entries, undated, signatures = [], [], defaultdict(list)
    rejected = 0
    for unit, mentions in parsed_documents():
        for mention in mentions:
            start, end = _span(unit.text[:limits.unit_chars], mention)
            try:
                citation = _citation(unit, start, end, support_token)
                if mention.text not in citation['excerpt']:
                    raise WorkspaceProblem('Date outside its citation')
            except WorkspaceProblem:
                rejected += 1
                continue
            # Prefer one explicit date in the same sentence, else one unambiguous
            # document date. Never borrow another document's date or choose among alternatives.
            local = [(u, m) for u, m in anchors[unit.document_id] if u.chunk_id == unit.chunk_id and start <= m.start and m.end <= end]
            choices = local or anchors[unit.document_id]
            dates = {m.start_date for _, m in choices}
            anchor = choices[0] if len(dates) == 1 else None
            # A recognized "previous day" is resolvable; unrelated retrospective
            # wording still prevents borrowing the document date.
            retrospective = RELATIVE.sub('', citation['excerpt'])
            if not local and re.search(r'\b(?:earlier|previous|prior|recalled|remembered)\b', retrospective, re.I):
                anchor = None
            context = [m for m in mentions if start <= m.start and m.end <= end]
            resolved = resolve_in_context(mention, anchor[1].start_date if anchor else None, context)
            citations = [citation]
            uncertainty = mention.kind if mention.kind != 'exact' else ''
            if resolved != mention:
                uncertainty = (uncertainty + '; ' if uncertainty else '') + 'Resolved against an explicit date in this document; verify the event date and clock basis.'
                au, am = anchor
                ast, aend = _span(au.text[:limits.unit_chars], am)
                try:
                    ac = _citation(au, ast, aend, support_token)
                except WorkspaceProblem:
                    rejected += 1
                    continue
                if ac != citation:
                    citations.append(ac)
            ordered = bool(resolved.start_date and not resolved.start_date.startswith('T'))
            if not ordered:
                uncertainty = (uncertainty + '; ' if uncertainty else '') + 'Calendar date unresolved; no century or missing year inferred.'
            entry = dict(entry_id=_hash([unit.document_id, unit.source_version_id, unit.chunk_id, mention.start, mention.end, citations, resolved.start_date, resolved.end_date]),
                         sort_key=resolved.start_date if ordered else '', date_label=mention.text,
                         precision=mention.precision, uncertainty=uncertainty,
                         description=citation['excerpt'].strip(), citations=citations, conflict_group=None,
                         end_date=resolved.end_date)
            # Compare literal event wording after removing date/time mentions. Keep
            # negations and participants: shared locations alone never establish identity.
            sentence = citation['excerpt']
            for other in reversed(_extract_mentions(sentence)):
                sentence = sentence[:other.start] + ' ' + sentence[other.end:]
            signature = ' '.join(re.findall(r'[a-z0-9]+', sentence.lower()))
            if len(signature.split()) >= 5 and not re.search(r'(?im)^(?:Record date|Date|ID|Matter reference):', sentence):
                signatures[signature + ' | precision=' + mention.precision].append(entry)
            (entries if ordered else undated).append(entry)
            if len(entries) + len(undated) >= limits.entries:
                capped |= unit != selected[-1] or mention != mentions[-1]
                break
        if len(entries) + len(undated) >= limits.entries:
            break
    conflicts = []
    for signature, group in sorted(signatures.items()):
        if len({e['citations'][0]['source_id'] for e in group}) < 2 or len({(e['sort_key'], e['end_date']) for e in group if e['sort_key']}) < 2:
            continue
        identifier = _hash(signature)
        for entry in group:
            entry['conflict_group'] = identifier
        conflicts.append(dict(group=identifier, entries=sorted(group, key=lambda e: (e['sort_key'], e['entry_id']))))
    entries.sort(key=lambda e: (e['sort_key'], e['entry_id']))
    undated.sort(key=lambda e: e['entry_id'])
    # Pure output deliberately has no wall-clock dependency. HTTP supplies the
    # time of recomputation; the same extracted inputs serialize byte-identically.
    return TimelineResult('exculpata-timeline-draft-v1', None, len({u.document_id for u in scanned}), len(scanned),
                          capped, entries, conflicts, undated, NOTICE, rejected)
