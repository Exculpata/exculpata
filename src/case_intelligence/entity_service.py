"""Manual entity workflows with explicit source and workspace authority."""

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
from dataclasses import asdict
import json

from .workspace_store import NOTEBOOK_STATUSES, WorkspaceProblem

ENTITY_TYPES = ('person', 'place', 'thing', 'organization', 'identifier', 'date')
ENTITY_STATUSES = NOTEBOOK_STATUSES
REFERENCE_FIELDS = ('document_id', 'source_version_id', 'source_name', 'location',
                    'unit_number', 'chunk_id', 'excerpt_digest', 'excerpt', 'support_token')


class EntityService:
    def __init__(self, repository, *, source_guard, resolve_support, load_note, load_references, validate_references,
                 current_sources=None):
        self.repository = repository
        self.source_guard = source_guard
        self.resolve_support = resolve_support
        self.load_note = load_note
        self.load_references = load_references
        self.current_sources = current_sources
        self.validate_references = validate_references

    @staticmethod
    def fields(*, display_name, entity_type='person', status='needs_review', aliases=''):
        name = display_name.strip()
        labels = [value.strip() for value in aliases.splitlines() if value.strip()]
        if not name or len(name) > 160 or any(ord(c) < 32 for c in name):
            raise WorkspaceProblem('Enter a display name of 1–160 characters.')
        if entity_type not in ENTITY_TYPES or status not in ENTITY_STATUSES:
            raise WorkspaceProblem('Choose an available entity type and review status.')
        if len(labels) > 30 or any(len(value) > 160 or any(ord(c) < 32 for c in value) for value in labels):
            raise WorkspaceProblem('Use at most 30 aliases, each on its own line with at most 160 characters.')
        return dict(display_name=name, entity_type=entity_type, status=status, aliases=list(dict.fromkeys(labels)))

    def list(self, matter_id, actor_id, *, query='', page=1):
        if len(query) > 200 or not 1 <= page <= 100_000:
            raise WorkspaceProblem('Choose a valid search or page.')
        with self.repository.transaction(matter_id, actor_id) as repo:
            return repo.list(matter_id, query, page)

    def detail(self, matter_id, actor_id, entity_id):
        with self.source_guard(), self.repository.transaction(matter_id, actor_id) as repo:
            return self._detail(repo, matter_id, actor_id, entity_id)

    def _detail(self, repo, matter_id, actor_id, entity_id):
        entity = repo.get(matter_id, entity_id)
        mentions = repo.mentions(matter_id, entity_id)
        available = self.validate_references(mentions)
        for index, mention in enumerate(mentions):
            mention['available'] = index in available
            mention['extraction_detail'] = json.loads(mention.get('date_json') or 'null')
        note = None
        if entity['notebook_item_id']:
            try:
                note = self.load_note(matter_id, actor_id, entity['notebook_item_id'])
            except KeyError:
                pass
        return entity, mentions, repo.history(matter_id, entity_id), note

    def export(self, matter_id, actor_id, entity_id):
        with self.source_guard(), self.repository.transaction(matter_id, actor_id) as repo:
            entity, mentions, history, _ = self._detail(repo, matter_id, actor_id, entity_id)
            return dict(format=f"{LEGACY_STEM}-entity-v1", entity=entity, mentions=mentions, history=history,
                reconciliations=repo.reconciliations(matter_id, entity_id))

    def create(self, matter_id, actor_id, *, support='', **values):
        fields = self.fields(**values)
        with self.source_guard(), self.repository.transaction(matter_id, actor_id) as repo:
            reference = self.resolve_support(support) if support else None
            entity = repo.create(matter_id, actor_id, fields)
            added = [repo.add_mention(matter_id, actor_id, entity['entity_id'], reference, 'manual')] if reference else []
            repo.record_history(matter_id, actor_id, entity['entity_id'], 'created', added_mentions=added)
            return entity

    def update(self, matter_id, actor_id, entity_id, *, expected_revision, **values):
        fields = self.fields(**values)
        with self.repository.transaction(matter_id, actor_id) as repo:
            repo.check_revision(matter_id, entity_id, expected_revision)
            repo.save(matter_id, actor_id, entity_id, fields)
            repo.record_history(matter_id, actor_id, entity_id, 'edited')
            return repo.get(matter_id, entity_id)

    def decide(self, matter_id, actor_id, targets, *, status):
        """Record one review decision on each listed identity, all or nothing.

        targets is a sequence of (entity_id, expected_revision). Nothing else
        about an identity changes; identities are never merged.
        """
        if status not in ('confirmed', 'dismissed', 'needs_review'):
            raise WorkspaceProblem('Choose confirm, not relevant, or needs review.')
        if not 1 <= len(targets) <= 200 or len({entity_id for entity_id, _ in targets}) != len(targets):
            raise WorkspaceProblem('Choose between 1 and 200 distinct suggestions.')
        from .entity_repository import EntityEditConflict
        with self.repository.transaction(matter_id, actor_id) as repo:
            for entity_id, expected_revision in targets:
                current = repo.check_revision(matter_id, entity_id, expected_revision)
                if current['origin'] != 'extraction' or current['status'] != 'suggested':
                    # Only pending automatic suggestions; the whole batch rolls back.
                    raise EntityEditConflict('That suggestion changed since this page loaded.')
                repo.save(matter_id, actor_id, entity_id, dict(current, status=status))
                repo.record_history(matter_id, actor_id, entity_id, 'edited')
        return len(targets)

    def inbox(self, matter_id, actor_id, *, kind='', page=1, rows=True):
        if not rows:
            # Counts only (while searching): no source data, so no source guard to wait on.
            with self.repository.transaction(matter_id, actor_id) as repo:
                return repo.suggestion_inbox(matter_id, kind, page, rows=False)
        with self.source_guard():
            with self.repository.transaction(matter_id, actor_id) as repo:
                items, total, kinds = repo.suggestion_inbox(matter_id, kind, page, rows=rows)
            # Link only passages whose source is still present, ready and at the
            # same version. This reads catalog metadata only, never derived text,
            # so a page citing many large sources stays cheap; the identity page
            # and opening the passage itself still validate the exact text.
            mentions = [item['first_mention'] for item in items if item['first_mention']]
            check = self.current_sources or self.validate_references
            available = check(mentions)
            for index, mention in enumerate(mentions):
                mention['available'] = index in available
            return items, total, kinds

    def date_draft(self, matter_id, actor_id, *, page=1):
        """Passages stating automatically found dates, for a read-only draft timeline."""
        with self.source_guard():
            with self.repository.transaction(matter_id, actor_id) as repo:
                items, total, ordered_total = repo.date_draft(matter_id, page)
            # As in the inbox: catalog metadata only; opening the passage still
            # validates the exact cited text.
            check = self.current_sources or self.validate_references
            available = check(items)
            for index, item in enumerate(items):
                item['available'] = index in available
            return items, total, ordered_total

    def attach(self, matter_id, actor_id, entity_id, *, expected_revision, support):
        with self.source_guard(), self.repository.transaction(matter_id, actor_id) as repo:
            current = repo.check_revision(matter_id, entity_id, expected_revision)
            reference = self.resolve_support(support)
            if repo.has_mention(matter_id, entity_id, support):
                return current
            added = repo.add_mention(matter_id, actor_id, entity_id, reference, 'manual')
            repo.save(matter_id, actor_id, entity_id, current)
            repo.record_history(matter_id, actor_id, entity_id, 'mention attached', added_mentions=[added])
            return repo.get(matter_id, entity_id)

    def remove_mention(self, matter_id, actor_id, entity_id, *, expected_revision, mention_id):
        with self.repository.transaction(matter_id, actor_id) as repo:
            current = repo.check_revision(matter_id, entity_id, expected_revision)
            removed = repo.remove_mention(matter_id, entity_id, mention_id)
            repo.save(matter_id, actor_id, entity_id, current)
            repo.record_history(matter_id, actor_id, entity_id, 'mention removed', removed_mentions=[removed])

    def review_mention(self, matter_id, actor_id, entity_id, *, expected_revision, mention_id, status):
        if status not in ENTITY_STATUSES:
            raise WorkspaceProblem('Choose an available mention review status.')
        with self.repository.transaction(matter_id, actor_id) as repo:
            current = repo.check_revision(matter_id, entity_id, expected_revision)
            repo.review_mention(matter_id, entity_id, mention_id, status)
            repo.save(matter_id, actor_id, entity_id, current)
            repo.record_history(matter_id, actor_id, entity_id, 'mention review: ' + mention_id + ' ' + status)

    def delete(self, matter_id, actor_id, entity_id, *, expected_revision):
        with self.repository.transaction(matter_id, actor_id) as repo:
            repo.check_revision(matter_id, entity_id, expected_revision)
            repo.delete(matter_id, entity_id)

    def import_note(self, matter_id, actor_id, item_id):
        with self.source_guard(), self.repository.transaction(matter_id, actor_id) as repo:
            existing = repo.imported(matter_id, item_id)
            if existing:
                return existing
            note = self.load_note(matter_id, actor_id, item_id)
            if note.item_type not in ENTITY_TYPES:
                raise WorkspaceProblem('Only supported person, place, or thing notes can be imported.')
            references = self.load_references(matter_id, actor_id, item_id)
            if not references:
                raise WorkspaceProblem('This note has no original source support. Attach a supported passage to a manually created entity instead.')
            prepared = [{key: getattr(reference, key) for key in REFERENCE_FIELDS} for reference in references]
            if self.validate_references(prepared) != frozenset(range(len(prepared))):
                raise WorkspaceProblem('A note passage changed or is unavailable. Review its original support before importing.')
            validated = {reference['support_token']: reference for reference in prepared}
            fields = self.fields(display_name=note.title, entity_type=note.item_type, status=note.status)
            entity = repo.create(matter_id, actor_id, fields, notebook=asdict(note))
            added = [repo.add_mention(matter_id, actor_id, entity['entity_id'], reference, 'notebook')
                     for reference in validated.values()]
            repo.record_history(matter_id, actor_id, entity['entity_id'], 'notebook imported', added_mentions=added)
            return entity

    def reconciliation_detail(self, matter_id, actor_id, entity_id, *, page=1):
        if not 1 <= page <= 100000:
            raise WorkspaceProblem('Choose a valid candidate page.')
        with self.repository.transaction(matter_id, actor_id) as repo:
            entity, pool = repo.candidate_page(matter_id, entity_id, page)
            history = repo.reconciliations(matter_id, entity_id)
        # Score only a bounded copied page, after releasing the writer lock.
        return repo.score_candidates(entity, pool[:50]), history, len(pool) > 50

    def reconcile(self, matter_id, actor_id, entity_id, *, expected_revision,
                  target_id, target_revision, action, mention_ids=()):
        if action not in ('merge', 'split', 'alias', 'reject') or entity_id == target_id:
            raise WorkspaceProblem('Choose two distinct identities and a reconciliation action.')
        with self.repository.transaction(matter_id, actor_id) as repo:
            source = repo.check_revision(matter_id, entity_id, expected_revision)
            target = repo.check_revision(matter_id, target_id, target_revision)
            if action == 'merge':
                moved_ids = [row['mention_id'] for row in repo.mentions(matter_id, entity_id)]
            elif action == 'split':
                moved_ids = list(dict.fromkeys(mention_ids))
            else:
                moved_ids = []
            if action == 'split' and not moved_ids:
                raise WorkspaceProblem('Select at least one mention to split into the other identity.')
            correction_fields = ('entity_id', 'entity_type', 'display_name', 'status', 'aliases', 'revision')
            before = dict(source={key: source[key] for key in correction_fields},
                target={key: target[key] for key in correction_fields}, mention_ids=moved_ids)
            moved = repo.move_mentions(matter_id, entity_id, target_id, moved_ids)
            destination = dict(target)
            if action in ('merge', 'alias'):
                destination['aliases'] = list(dict.fromkeys([*target['aliases'], source['display_name']]))
                if len(destination['aliases']) > 30:
                    raise WorkspaceProblem('The target already has 30 aliases. Review its labels before linking another.')
            repo.save(matter_id, actor_id, entity_id, source)
            repo.save(matter_id, actor_id, target_id, destination)
            repo.record_history(matter_id, actor_id, entity_id, action + ' reviewed', removed_mentions=moved)
            added = [dict(row, entity_id=target_id) for row in moved]
            repo.record_history(matter_id, actor_id, target_id, action + ' reviewed', added_mentions=added)
            return repo.save_reconciliation(matter_id, actor_id, action, before,
                dict(source_revision=source['revision'] + 1, target_revision=target['revision'] + 1))

    def undo(self, matter_id, actor_id, operation_id):
        from .entity_repository import EntityEditConflict
        with self.repository.transaction(matter_id, actor_id) as repo:
            operation = repo.get_reconciliation(matter_id, operation_id)
            if operation['undone']:
                raise EntityEditConflict('This correction has already been undone.')
            before, after = json.loads(operation['before_json']), json.loads(operation['after_json'])
            source, target = before['source'], before['target']
            repo.check_revision(matter_id, source['entity_id'], after['source_revision'])
            repo.check_revision(matter_id, target['entity_id'], after['target_revision'])
            moved = repo.move_mentions(matter_id, target['entity_id'], source['entity_id'], before['mention_ids'])
            repo.save(matter_id, actor_id, source['entity_id'], source)
            repo.save(matter_id, actor_id, target['entity_id'], target)
            repo.record_history(matter_id, actor_id, source['entity_id'], 'reconciliation undone',
                added_mentions=[dict(row, entity_id=source['entity_id']) for row in moved])
            repo.record_history(matter_id, actor_id, target['entity_id'], 'reconciliation undone', removed_mentions=moved)
            repo.undo_reconciliation(matter_id, operation_id)


def current_source_indexes(references, load_document):
    """Positions whose source is present, ready and still at the cited version.

    Catalog metadata only: no derived text is read.
    """
    available = set()
    for index, reference in enumerate(references):
        try:
            document = load_document(reference['document_id'])
        except (KeyError, OSError, RuntimeError):
            continue
        if document.state == 'ready' and document.version_id == reference['source_version_id']:
            available.add(index)
    return frozenset(available)


def current_reference_indexes(references, *, load_document, candidate_for, support_tokens, read_units=None):
    """Validate only referenced documents, reading each once under the source guard.

    Results identify input positions, not tokens: duplicate tokens with differing
    metadata must each pass the exact original-reference comparison. read_units
    (document, ordinals) -> (ordinal, unit) pairs, when given, reads only the
    referenced units through a bounded index instead of parsing whole files. An
    original that cannot be read (missing, unsafe, damaged or malformed derived text) is
    reported as unavailable rather than failing the page.
    """
    grouped = {}
    for index, reference in enumerate(references):
        grouped.setdefault(reference['document_id'], []).append((index, reference))
    available = set()
    for document_id, expected in grouped.items():
        wanted = {}
        for index, reference in expected:
            try:
                ordinal = int(reference['chunk_id'].removeprefix('chunk-'))
            except (TypeError, ValueError, AttributeError):
                continue
            if ordinal >= 1:
                wanted.setdefault(ordinal, []).append((index, reference))
        if not wanted:
            continue
        try:
            document = load_document(document_id)
            if document.state != 'ready':
                continue
            if read_units is not None:
                units = dict(read_units(document, sorted(wanted)))
            else:
                parsed = document.parsed_units()
                units = {ordinal: parsed[ordinal - 1] for ordinal in wanted if ordinal <= len(parsed)}
            for ordinal, references_at in wanted.items():
                unit = units.get(ordinal)
                if unit is None:
                    continue
                candidate = candidate_for(document, unit, ordinal)
                tokens = support_tokens(candidate)
                current = dict(document_id=candidate.document_id, source_version_id=candidate.source_version_id,
                    source_name=candidate.source_name, location=candidate.citation, unit_number=unit.number,
                    chunk_id=candidate.chunk_id, excerpt_digest=candidate.excerpt_digest,
                    excerpt=candidate.text[:6000])
                for index, reference in references_at:
                    current['support_token'] = reference['support_token']
                    if reference['support_token'] in tokens and all(
                            current[key] == reference[key] for key in REFERENCE_FIELDS):
                        available.add(index)
        except (KeyError, OSError, RuntimeError, TypeError, AttributeError, ValueError):
            # Includes malformed unit records (valid JSON of the wrong shape).
            continue
    return frozenset(available)
