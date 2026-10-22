"""Manual events and assertions: attributed original accounts, independent review."""

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
from datetime import date
import json
import re
import unicodedata

from .assertion_repository import MAX_RECORDS, MAX_ROLES, MAX_STORAGE_BYTES
from .workspace_store import WorkspaceProblem

ASSERTION_TYPES = ('assertion', 'event')
ASSERTION_STATUSES = ('needs_review', 'confirmed', 'disputed', 'dismissed')
ROLE_TYPES = ('subject', 'participant', 'speaker', 'witness', 'location', 'object', 'organization')
ACCOUNT_STANCES = ('supporting', 'competing')


class AssertionService:
    def __init__(self, repository, entity_service):
        self.repository = repository
        self.entities = entity_service

    @staticmethod
    def _text(value, label, maximum, *, required=False, multiline=False, preserve=False):
        if not isinstance(value, str):
            raise WorkspaceProblem('Enter valid ' + label + '.')
        if multiline:
            value = value.replace('\r\n', '\n')
        cleaned = value if preserve else value.strip()
        if ((required and not cleaned.strip()) or len(cleaned) > maximum or
                any(unicodedata.category(c) == 'Cc' and not (multiline and c == '\n') for c in value)):
            raise WorkspaceProblem('Enter ' + label + ' within ' + str(maximum) + ' characters, without control characters.')
        return cleaned

    @classmethod
    def fields(cls, *, title, statement, record_type='assertion', raw_date='', date_uncertainty='', sort_date='', status='needs_review'):
        if record_type not in ASSERTION_TYPES or status not in ASSERTION_STATUSES:
            raise WorkspaceProblem('Choose an available record type and human review status.')
        title = cls._text(title, 'a title', 160, required=True)
        statement = cls._text(statement, 'a statement', 6000, required=True, multiline=True)
        raw_date = cls._text(raw_date, 'the original date wording', 200, preserve=True)
        date_uncertainty = cls._text(date_uncertainty, 'date uncertainty', 1000)
        if not isinstance(sort_date, str) or (sort_date and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', sort_date)):
            raise WorkspaceProblem('Enter an exact ordering date as YYYY-MM-DD, or leave it blank.')
        if sort_date:
            try:
                if date.fromisoformat(sort_date).isoformat() != sort_date:
                    raise ValueError
            except ValueError:
                raise WorkspaceProblem('Enter a valid calendar ordering date, or leave it blank.') from None
        return dict(title=title, statement=statement, record_type=record_type, raw_date=raw_date,
                    date_uncertainty=date_uncertainty, sort_date=sort_date, status=status)

    @classmethod
    def account_fields(cls, stance, attributed_to):
        if stance not in ACCOUNT_STANCES:
            raise WorkspaceProblem('Choose supporting or competing for this source account.')
        return stance, cls._text(attributed_to, 'source or speaker attribution', 200, required=True)

    @staticmethod
    def _role(repo, matter_id, entity_id, entity_revision, role):
        if role not in ROLE_TYPES:
            raise WorkspaceProblem('Choose an available entity role.')
        if type(entity_revision) is not int or entity_revision < 1:
            raise WorkspaceProblem('Choose the current saved entity revision.')
        return repo.entities.check_revision(matter_id, entity_id, entity_revision)

    def _resolve(self, support):
        if not isinstance(support, str) or not re.fullmatch(r'[0-9a-f]{40}', support):
            raise WorkspaceProblem('Choose a current original passage as support.')
        try:
            return self.entities.resolve_support(support)
        except KeyError:
            raise WorkspaceProblem('The original passage changed or is unavailable. Return to source review and choose current support.') from None

    @staticmethod
    def _finish(repo, matter_id, actor_id, current, action, *, fields=None, **deltas):
        assertion_id = current['assertion_id']
        repo.save(matter_id, actor_id, assertion_id, current if fields is None else fields)
        repo.record_history(matter_id, actor_id, assertion_id, action, **deltas)
        repo.check_limits(matter_id, assertion_id)
        return repo.get(matter_id, assertion_id)

    def list(self, matter_id, actor_id, *, entity_id='', page=1, date_group='all'):
        if type(page) is not int or not 1 <= page <= 100000 or date_group not in ('all', 'dated', 'undated'):
            raise WorkspaceProblem('Choose a valid chronology page or date group.')
        with self.repository.transaction(matter_id, actor_id) as repo:
            return repo.list(matter_id, entity_id, page, date_group)

    def _detail(self, repo, matter_id, assertion_id):
        record = repo.get(matter_id, assertion_id)
        accounts = repo.accounts(matter_id, assertion_id)
        available = self.entities.validate_references(accounts)
        for index, account in enumerate(accounts):
            account['available'] = index in available
        return dict(record=record, roles=repo.roles(matter_id, assertion_id), accounts=accounts,
                    history=repo.history(matter_id, assertion_id))

    def detail(self, matter_id, actor_id, assertion_id):
        with self.entities.source_guard(), self.repository.transaction(matter_id, actor_id) as repo:
            return self._detail(repo, matter_id, assertion_id)

    def export(self, matter_id, actor_id, assertion_id):
        with self.entities.source_guard(), self.repository.transaction(matter_id, actor_id) as repo:
            return dict(format=f"{LEGACY_STEM}-assertion-v1", **self._detail(repo, matter_id, assertion_id))

    def chronology_export(self, matter_id, actor_id, *, entity_id=''):
        with self.entities.source_guard(), self.repository.transaction(matter_id, actor_id) as repo:
            if repo.list(matter_id, entity_id)[1] > MAX_RECORDS:
                raise WorkspaceProblem('This chronology exceeds the export record limit. No partial export was produced.')
            records, size = [], 0
            for exported in repo.export_records(matter_id, entity_id=entity_id):
                exported.pop('source_support')
                # Include a bounded allowance for availability booleans and the
                # enclosing document before collecting the next serialized row.
                size += len(json.dumps(exported, ensure_ascii=False).encode('utf-8')) + 32 * len(exported['accounts']) + 256
                if size > MAX_STORAGE_BYTES:
                    raise WorkspaceProblem('This chronology exceeds the export payload limit. No partial export was produced.')
                records.append(exported)
            accounts = [account for record in records for account in record['accounts']]
            available = self.entities.validate_references(accounts)
            for index, account in enumerate(accounts):
                account['available'] = index in available
            result = dict(format=f"{LEGACY_STEM}-chronology-v1", records=records,
                ordering='Explicit reviewer-entered calendar dates first; records without ordering dates follow separately. Raw date wording is not inferred.')
            if len(json.dumps(result, indent=2).encode('utf-8')) > MAX_STORAGE_BYTES:
                raise WorkspaceProblem('This chronology exceeds the export payload limit. No partial export was produced.')
            return result

    def create(self, matter_id, actor_id, *, support, attributed_to, roles=(), **values):
        fields = self.fields(**values)
        _, attribution = self.account_fields('supporting', attributed_to)
        if not isinstance(roles, (list, tuple)) or not 1 <= len(roles) <= MAX_ROLES:
            raise WorkspaceProblem('Choose at least one explicit entity role, within the role limit.')
        with self.entities.source_guard(), self.repository.transaction(matter_id, actor_id) as repo:
            reference = self._resolve(support)
            prepared = []
            for entry in roles:
                if not isinstance(entry, dict) or not {'entity_id', 'expected_revision', 'role'} <= entry.keys():
                    raise WorkspaceProblem('Choose an identity, saved revision, and role for every entity.')
                prepared.append((self._role(repo, matter_id, entry['entity_id'], entry['expected_revision'], entry['role']), entry['role']))
            record = repo.create(matter_id, actor_id, fields)
            assertion_id = record['assertion_id']
            added_roles = [repo.add_role(matter_id, actor_id, assertion_id, entity, role) for entity, role in prepared]
            account = repo.add_account(matter_id, actor_id, assertion_id, reference, 'supporting', attribution)
            repo.record_history(matter_id, actor_id, assertion_id, 'created', added_roles=added_roles, added_accounts=[account])
            repo.check_limits(matter_id, assertion_id)
            return record

    def update(self, matter_id, actor_id, assertion_id, *, expected_revision, **values):
        fields = self.fields(**values)
        with self.repository.transaction(matter_id, actor_id) as repo:
            current = repo.check_revision(matter_id, assertion_id, expected_revision)
            return self._finish(repo, matter_id, actor_id, current, 'review or statement corrected', fields=fields)

    def attach(self, matter_id, actor_id, assertion_id, *, expected_revision, support, stance='supporting', attributed_to):
        stance, attribution = self.account_fields(stance, attributed_to)
        with self.entities.source_guard(), self.repository.transaction(matter_id, actor_id) as repo:
            current = repo.check_revision(matter_id, assertion_id, expected_revision)
            reference = self._resolve(support)
            duplicate = repo.duplicate_account(matter_id, assertion_id, support, attribution)
            if duplicate:
                if duplicate['stance'] != stance:
                    raise WorkspaceProblem('This attributed passage is already attached. Revise its existing account to change the stance.')
                return current
            added = repo.add_account(matter_id, actor_id, assertion_id, reference, stance, attribution)
            return self._finish(repo, matter_id, actor_id, current, 'account attached', added_accounts=[added])

    @staticmethod
    def _keep_support(repo, matter_id, assertion_id, removed):
        if removed['stance'] == 'supporting' and sum(row['stance'] == 'supporting' for row in repo.accounts(matter_id, assertion_id)) < 2:
            raise WorkspaceProblem('Keep at least one supporting original account. Add replacement support first, or remove the entire record.')

    def revise_account(self, matter_id, actor_id, assertion_id, *, expected_revision, account_id, stance, attributed_to):
        stance, attribution = self.account_fields(stance, attributed_to)
        with self.repository.transaction(matter_id, actor_id) as repo:
            current = repo.check_revision(matter_id, assertion_id, expected_revision)
            account = repo.account(matter_id, assertion_id, account_id)
            if account['stance'] != stance:
                self._keep_support(repo, matter_id, assertion_id, account)
            if repo.duplicate_account(matter_id, assertion_id, account['support_token'], attribution, excluding=account_id):
                raise WorkspaceProblem('That attributed passage is already attached. Correct or remove the duplicate account instead.')
            correction = dict(account_id=account_id, before=dict(stance=account['stance'], attributed_to=account['attributed_to']),
                              after=dict(stance=stance, attributed_to=attribution))
            repo.revise_account(matter_id, assertion_id, account_id, stance, attribution)
            return self._finish(repo, matter_id, actor_id, current, 'account corrected', corrected_accounts=[correction])

    def remove_account(self, matter_id, actor_id, assertion_id, *, expected_revision, account_id):
        with self.repository.transaction(matter_id, actor_id) as repo:
            current = repo.check_revision(matter_id, assertion_id, expected_revision)
            account = repo.account(matter_id, assertion_id, account_id)
            self._keep_support(repo, matter_id, assertion_id, account)
            removed = repo.remove_account(matter_id, assertion_id, account_id)
            return self._finish(repo, matter_id, actor_id, current, 'account removed', removed_accounts=[removed])

    def add_role(self, matter_id, actor_id, assertion_id, *, expected_revision, entity_id, entity_revision, role):
        with self.repository.transaction(matter_id, actor_id) as repo:
            current = repo.check_revision(matter_id, assertion_id, expected_revision)
            entity = self._role(repo, matter_id, entity_id, entity_revision, role)
            added = repo.add_role(matter_id, actor_id, assertion_id, entity, role)
            return self._finish(repo, matter_id, actor_id, current, 'role attached', added_roles=[added])

    def remove_role(self, matter_id, actor_id, assertion_id, *, expected_revision, role_id):
        with self.repository.transaction(matter_id, actor_id) as repo:
            current = repo.check_revision(matter_id, assertion_id, expected_revision)
            if len(repo.roles(matter_id, assertion_id, current=False)) <= 1:
                raise WorkspaceProblem('Keep at least one explicit entity role. Add a replacement role first, or remove the entire record.')
            removed = repo.remove_role(matter_id, assertion_id, role_id)
            return self._finish(repo, matter_id, actor_id, current, 'role removed', removed_roles=[removed])

    def revise_role(self, matter_id, actor_id, assertion_id, *, expected_revision, role_id, entity_id, entity_revision, role):
        with self.repository.transaction(matter_id, actor_id) as repo:
            current = repo.check_revision(matter_id, assertion_id, expected_revision)
            before = repo.role(matter_id, assertion_id, role_id)
            entity = self._role(repo, matter_id, entity_id, entity_revision, role)
            after = repo.revise_role(matter_id, assertion_id, role_id, entity, role)
            return self._finish(repo, matter_id, actor_id, current, 'role corrected',
                corrected_roles=[dict(role_id=role_id, before=before, after=after)])

    def delete(self, matter_id, actor_id, assertion_id, *, expected_revision):
        with self.repository.transaction(matter_id, actor_id) as repo:
            repo.check_revision(matter_id, assertion_id, expected_revision)
            repo.delete(matter_id, assertion_id)
