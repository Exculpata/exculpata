"""Promote one draft with every distinct date-basis account atomically."""
from contextlib import contextmanager

from .assertion_service import AssertionService


class _PromotionTransaction:
    """Let existing service operations join our already-authorized unit of work.

    The adapter is local to one promotion, never installed on a shared service.
    The outer repository transaction owns authorization, commit and rollback.
    """
    def __init__(self, repository, matter_id, actor_id):
        self.repository = repository
        self.identity = (matter_id, actor_id)

    @contextmanager
    def transaction(self, matter_id, actor_id):
        if (matter_id, actor_id) != self.identity:
            raise KeyError(matter_id)
        yield self.repository


def promote_entry(service, matter_id, actor_id, entry, *, entity_id, revision, role):
    with service.entities.source_guard(), service.repository.transaction(matter_id, actor_id) as repo:
        citations = {}
        for citation in entry['citations']:
            reference = service.entities.resolve_support(citation['support_token'])
            if (reference['source_version_id'] != citation['source_version_id'] or
                    reference['excerpt_digest'] != citation['excerpt_digest']):
                raise KeyError(entry['entry_id'])
            citations.setdefault(citation['support_token'], citation)
        primary, *anchors = citations.values()
        writer = AssertionService(_PromotionTransaction(repo, matter_id, actor_id), service.entities)
        record = writer.create(matter_id, actor_id, support=primary['support_token'],
            attributed_to=primary['document_title'][:200],
            roles=[dict(entity_id=entity_id, expected_revision=revision, role=role)], record_type='event',
            title=' '.join(entry['description'].split())[:160], statement=entry['description'],
            raw_date=entry['date_label'][:200], date_uncertainty=entry['uncertainty'] +
                (' Possible date conflict; compare the draft sources.' if entry['conflict_group'] else ''),
            sort_date=entry['sort_key'][:10], status='needs_review')
        for citation in anchors:
            record = writer.attach(matter_id, actor_id, record['assertion_id'],
                expected_revision=record['revision'], support=citation['support_token'],
                attributed_to=citation['document_title'][:200])
        return record
