"""Matter-scoped candidate snapshots and durable human review decisions."""
from contextlib import contextmanager
from dataclasses import asdict

from .candidate_review import CandidateReviewRepository
from .review_candidates import (CATEGORIES, FORMAT, MAX_CANDIDATES, MAX_UNITS, MAX_UNIT_CHARS,
    NOTICE, PRIOR_STATEMENT_DIFFERENCE_ENABLED, ReviewCandidate, add_verified_reason, detect, validate)


class ReviewCandidateService:
    def __init__(self, bench, matter):
        self.bench, self.matter = bench, matter
        self.store = bench.source_store(matter)
        self.entities = bench.entity_service(matter, store=self.store)
        self.reviews = CandidateReviewRepository(self.entities.repository)

    @contextmanager
    def snapshot(self, actor, *, writing=False):
        repository = self.entities.repository
        transaction = repository.transaction if writing else repository.reading
        with self.store.mutation_guard(), transaction(self.matter.matter_id, actor):
            units, capped = [], False
            for document in self.store.ready_documents():
                for ordinal, unit in enumerate(document.parsed_units(), 1):
                    if len(units) >= MAX_UNITS:
                        capped = True
                        break
                    units.append(self.bench._candidate(self.matter, document, unit, ordinal))
                if capped:
                    break
            originals = {(u.document_id, u.chunk_id): u for u in units}

            def resolve(citation):
                u = originals[(citation['document_id'], citation['chunk_id'])]
                return dict(document_id=u.document_id, source_version_id=u.source_version_id,
                    chunk_id=u.chunk_id, excerpt_digest=u.excerpt_digest, location=u.citation,
                    source_name=u.source_name, unit_number=u.page_number, excerpt=u.text,
                    support_token=self.bench._support_token(u))

            rows, dropped = validate(detect(units), resolve)
            capped = capped or any(len(u.text) > MAX_UNIT_CHARS for u in units) or len(rows) > MAX_CANDIDATES
            rows = [self.reviews.apply(self.matter.matter_id, c, namespace='review_candidates')
                    for c in rows[:MAX_CANDIDATES]]
            yield dict(candidates=rows, dropped=dropped, capped=capped, unit_count=len(units))

    def payload(self, actor, *, category='', status='active', generation=None):
        if generation is not None and generation.available:
            # Generate outside source/workspace locks, then take a fresh authorized
            # snapshot. Changed sources/signatures cannot acquire stale annotations.
            initial = self.payload(actor, category=category, status=status)
            annotated = {row['signature']: add_verified_reason(ReviewCandidate(**row), generation).additional_reason
                         for row in initial['candidates'][:8]}
            current = self.payload(actor, category=category, status=status)
            for row in current['candidates']:
                row['additional_reason'] = annotated.get(row['signature'], '')
            return current
        with self.snapshot(actor) as snapshot:
            rows = [asdict(c) for c in snapshot['candidates']
                    if (not category or c.category == category)
                    and (status == 'all' or (c.status != 'dismissed' if status == 'active' else c.status == status))]
            counts = {key: sum(c['category']==key for c in rows) for key in CATEGORIES}
            return dict(notice=NOTICE, format=FORMAT, candidates=rows, counts=counts,
                dropped=snapshot['dropped'], capped=snapshot['capped'], unit_count=snapshot['unit_count'],
                prior_statement_difference_enabled=PRIOR_STATEMENT_DIFFERENCE_ENABLED)

    def review(self, actor, signature, action, revision):
        with self.snapshot(actor, writing=True) as snapshot:
            candidate = next((c for c in snapshot['candidates'] if c.signature == signature), None)
            if candidate is None:
                raise KeyError(signature)
            return self.reviews.save(self.matter.matter_id, actor, candidate, action, revision, namespace='review_candidates')
