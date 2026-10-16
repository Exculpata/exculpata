"""Current-source comparison snapshots inside existing matter authority guards."""
from contextlib import contextmanager
from dataclasses import asdict

from .candidate_review import CandidateReviewRepository
from .matter_analysis import MAX_ANALYSIS_UNITS, possible_inconsistencies
from .statement_comparison import MAX_STATEMENTS, MAX_PAIRS, MAX_CANDIDATES


class StatementComparisonService:
    def __init__(self, bench, matter):
        self.bench, self.matter = bench, matter
        # Resolve cache before either guard; never re-enter it from a transaction.
        self.store = bench.source_store(matter)
        self.entities = bench.entity_service(matter, store=self.store)
        self.reviews = CandidateReviewRepository(self.entities.repository)

    @contextmanager
    def snapshot(self, actor, *, writing=False):
        matter_id = self.matter.matter_id
        repository = self.entities.repository
        transaction = repository.transaction if writing else repository.reading
        with self.store.mutation_guard(), transaction(matter_id, actor):
            units = []
            capped = False
            for document in self.store.ready_documents():
                for ordinal, unit in enumerate(document.parsed_units(), 1):
                    if len(units) == MAX_ANALYSIS_UNITS:
                        capped = True
                        break
                    units.append(self.bench._candidate(self.matter, document, unit, ordinal))
                if capped:
                    break
            entities = [repository.record(row) for row in repository.connection.execute(
                'SELECT * FROM workbench_entity WHERE matter_id=? ORDER BY entity_id LIMIT 5000', (matter_id,))]
            originals = {(u.document_id, u.chunk_id): u for u in units}

            def resolve(citation):
                unit = originals[(citation['document_id'], citation['chunk_id'])]
                return dict(document_id=unit.document_id, source_version_id=unit.source_version_id,
                    chunk_id=unit.chunk_id, excerpt_digest=unit.excerpt_digest, location=unit.citation,
                    source_name=unit.source_name, unit_number=unit.page_number,
                    excerpt=unit.text, support_token=self.bench._support_token(unit))

            statements, candidates, dropped = possible_inconsistencies(units, entities, resolve)
            candidates = [self.reviews.apply(matter_id, candidate) for candidate in candidates]
            capped = (capped or any(len(unit.text) > 6000 for unit in units) or len(statements) >= MAX_STATEMENTS or
                      len(statements) * (len(statements)-1) // 2 > MAX_PAIRS or len(candidates) >= MAX_CANDIDATES)
            speakers = {s.speaker_entity_id: s.speaker_name for s in statements if s.speaker_entity_id}
            yield dict(candidates=candidates, dropped=dropped, capped=capped, unit_count=len(units), speakers=speakers)

    def payload(self, actor, *, witness='', difference_type='', status='active'):
        with self.snapshot(actor) as snapshot:
            rows = [asdict(c) for c in snapshot['candidates']
                    if (not witness or witness in c.speaker_ids)
                    and (not difference_type or c.difference_type == difference_type)
                    and (status == 'all' or (c.status != 'dismissed' if status == 'active' else c.status == status))]
            return dict(format='exculpata-inconsistency-candidates-v1', candidates=rows,
                        dropped=snapshot['dropped'], capped=snapshot['capped'],
                        unit_count=snapshot['unit_count'], speakers=snapshot['speakers'])

    def review(self, actor, signature, action, revision):
        with self.snapshot(actor, writing=True) as snapshot:
            candidate = next((c for c in snapshot['candidates'] if c.signature == signature), None)
            if candidate is None:
                raise KeyError(signature)
            return self.reviews.save(self.matter.matter_id, actor, candidate, action, revision)
