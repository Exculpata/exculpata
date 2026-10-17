"""Bounded, source-validated passages for attorney review; no legal assessment."""
from dataclasses import asdict, dataclass, replace
from importlib import resources
import json
import re

from .answer_evidence import validated_window_text
from .generation import EvidenceItem, GenerationRejected, GenerationUnavailable
from .matter_analysis import _signature
from .review_wording import prohibited_phrases as shared_prohibited_phrases
from .workspace_store import WorkspaceProblem

NOTICE = ('Passages that may be worth attorney review for exculpatory or impeachment value. '
          'The tool does not decide whether any material is exculpatory, material, or subject to disclosure.')
FORMAT = 'exculpata-review-candidates-v1'
MAX_UNITS = 2500
MAX_UNIT_CHARS = 6000
MAX_CANDIDATES = 150
# TODO #271 / PR #283: activate only after the dependency has merged and its
# same-person attribution contract has passed the release review.
PRIOR_STATEMENT_DIFFERENCE_ENABLED = False
RULES = json.loads(resources.files('case_intelligence').joinpath('data/review_candidate_rules.json').read_text())
CATEGORIES = {r['key']: r['label'] for r in RULES}
_EXTRA_PROHIBITED = ('is Brady', 'is Giglio', 'Brady material', 'Brady violation',
    'must be disclosed', 'required to disclose', 'withheld', 'suppressed',
    'exculpatory evidence', 'proves', 'establishes', 'guilty', 'innocent', 'lied')


def prohibited_phrases(text):
    """Check authored wording; verbatim source passages are separately attributed."""
    text = text.replace(NOTICE, '')
    return shared_prohibited_phrases(text) + [p for p in _EXTRA_PROHIBITED
        if re.search(r'\b' + re.escape(p) + r'\b', text, re.I)]


@dataclass(frozen=True)
class ReviewCandidate:
    signature: str
    category: str
    reason: str
    citation: dict
    source_order: int
    related_citation: dict | None = None
    additional_reason: str = ''
    status: str = 'open'
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    revision: int = 0
    kind: str = 'candidate_for_review'


def _spans(text):
    start = 0
    for separator in re.finditer(r'(?<=[.!?])\s+(?=[A-Z])|\n\s*\n', text):
        if separator.start() > start:
            yield start, separator.start()
        start = separator.end()
    if start < len(text):
        yield start, len(text)


def _matches(rule, text):
    if rule['context'] and not any(re.search(p, text, re.I) for p in rule['context']):
        return None
    # Exclude idiomatic senses locally so a real benefit elsewhere is retained.
    excluded = [m.span() for p in rule['exclude'] for m in re.finditer(p, text, re.I)]
    for pattern in rule['patterns']:
        for match in re.finditer(pattern, text, re.I | re.S):
            if any(a <= match.start() < b for a,b in excluded):
                continue
            if rule.get('negation_before'):
                # Negation inside the trigger (e.g. "did not match") describes
                # a mismatch. Earlier negation in its clause can reverse that
                # meaning regardless of intervening modifiers: abstain.
                prefix = re.split(rule['clause_boundary'], text[:match.start()], flags=re.I)[-1]
                if re.search(rule['negation_before'], prefix, re.I):
                    continue
            return match
    return None


def detect(units):
    """Keep negation and hedges verbatim; the reason only describes a mention."""
    found = {}
    category_counts = {key: 0 for key in CATEGORIES}
    for order, unit in enumerate(units[:MAX_UNITS]):
        for start,end in _spans(unit.text[:MAX_UNIT_CHARS]):
            excerpt = unit.text[start:end]
            for rule in RULES:
                if category_counts[rule['key']] >= MAX_CANDIDATES + 1:
                    continue
                match = _matches(rule, excerpt)
                if not match:
                    continue
                citation = dict(document_id=unit.document_id, source_version_id=unit.source_version_id,
                    chunk_id=unit.chunk_id, source_name=unit.source_name, location=unit.citation,
                    unit_number=unit.page_number, excerpt_digest=unit.excerpt_digest, excerpt=excerpt,
                    highlight=match.group(), highlight_start=match.start(), window=dict(start=start, end=end,
                        original_chars=len(unit.text), omitted_chars=len(unit.text)-(end-start)))
                signature = _signature('review-candidates-v1', json.dumps(rule, sort_keys=True), unit.document_id,
                    unit.source_version_id, unit.chunk_id, unit.excerpt_digest, str(start), excerpt)
                if signature not in found:
                    category_counts[rule['key']] += 1
                found[signature] = ReviewCandidate(signature, rule['key'], rule['reason'], citation, order)
    return sorted(found.values(), key=lambda c: (list(CATEGORIES).index(c.category), c.source_order,
                                                c.citation['window']['start'], c.signature))


def validate(candidates, resolve):
    accepted, dropped = [], 0
    for candidate in candidates:
        citations = []
        try:
            for citation in (candidate.citation, candidate.related_citation):
                if citation is None and citations:
                    continue
                current = resolve(citation)
                for key in ('document_id','source_version_id','chunk_id','excerpt_digest','location','source_name','unit_number'):
                    if not citation[key] or current[key] != citation[key]:
                        raise ValueError('Citation identity changed')
                excerpt = validated_window_text(current['excerpt'], citation['window'])
                highlight_start = citation.get('highlight_start', excerpt.find(citation['highlight']))
                if (excerpt != citation['excerpt'] or not citation['highlight']
                        or type(highlight_start) is not int or highlight_start < 0
                        or excerpt[highlight_start:highlight_start+len(citation['highlight'])] != citation['highlight']):
                    raise ValueError('Citation window changed')
                citations.append(dict(citation, support_token=current['support_token']))
        except (KeyError, TypeError, ValueError, WorkspaceProblem):
            dropped += 1
            continue
        accepted.append(replace(candidate, citation=citations[0], related_citation=citations[1] if len(citations)>1 else None))
    return accepted, dropped


def from_comparisons(payload, *, enabled=False):
    """Consume #271's typed payload; cross-person comparisons cannot enter here."""
    if not enabled or payload.get('format') != 'exculpata-inconsistency-candidates-v1':
        return []
    rule = next(r for r in RULES if r['key']=='prior_statement_difference')
    result=[]
    for order,row in enumerate(payload.get('candidates', [])[:MAX_CANDIDATES]):
        if row.get('kind') != 'possible_inconsistency' or row.get('scope') != 'same_witness' or len(row.get('speaker_ids', [])) != 1:
            continue
        if not row.get('left') or not row.get('right') or not row.get('signature'):
            continue
        result.append(ReviewCandidate(_signature(FORMAT, json.dumps(rule, sort_keys=True), row['signature'], row['scope'], json.dumps(row['speaker_ids'])),
            rule['key'], rule['reason'], row['left'], order, related_citation=row['right']))
    return result


def add_verified_reason(candidate, generation):
    """Optional local-model annotation; never creates, deletes or recategorizes a row.

    Only one cited, source-close reason that survives the existing verifier and
    neutral-wording check is eligible. The deterministic reason remains present.
    """
    if generation is None or not generation.available:
        return candidate
    c = candidate.citation
    try:
        answer = generation.answer('Describe only the passage wording relevant to this review category: '
            + CATEGORIES[candidate.category] + '. Preserve negation and uncertainty. Cite the passage.',
            [EvidenceItem('S1', c['source_name'], c['location'], c['excerpt'], document_id=c['document_id'])])
    except (GenerationRejected, GenerationUnavailable, ValueError):
        return candidate
    if not answer.answerable or len(answer.claims) != 1:
        return candidate
    claim = answer.claims[0]
    if claim.evidence_ids != ('S1',) or len(claim.text) > 500 or prohibited_phrases(claim.text):
        return candidate
    # Keep the annotation source-close, including hedges and negation, rather
    # than trusting a model's interpretation of legal significance.
    if claim.text.strip() != c['excerpt'].strip():
        return candidate
    return replace(candidate, additional_reason=claim.text)
