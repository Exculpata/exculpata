"""Committed source snapshots, subscriptions and bounded registry reads."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import uuid4

MAX_READ = 500


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    document_id: str
    version_id: str
    display_name: str
    relative_path: str
    media_type: str
    size: int
    state: str
    message: str
    digest: str
    page_count: int
    origin: str
    has_text: bool


def snapshot(payload):
    return SourceSnapshot(
        payload['document_id'], payload['version_id'], payload['display_name'],
        payload.get('relative_path') or payload['display_name'], payload['media_type'],
        int(payload['size']), payload['state'], payload.get('message', ''),
        payload.get('digest', ''), int(payload.get('page_count', 0)),
        payload.get('origin', 'upload'),
        payload['state'] == 'ready' and bool(payload.get('units') or payload.get('units_file')))


def token(document):
    return hashlib.sha256(('pilot-action\x00' + document.document_id + '\x00' +
                           document.version_id).encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class SourceChange:
    epoch: str
    revision: int
    upserted: tuple[SourceSnapshot, ...]
    removed_document_ids: tuple[str, ...]


class SourceChanges:
    def __init__(self, store):
        self.store = store
        self.epoch = uuid4().hex
        self.subscribers = {}
        self.failed_revision = -1
        self.by_document = {}
        self.by_token = {}
        for document in store.documents.values():
            self.by_document[document.document_id] = token(document)
            self.by_token[token(document)] = document.document_id

    def subscribe(self, callback):
        with self.store.mutation_guard():
            key = uuid4().hex
            self.subscribers[key] = callback
            return key, self.epoch, self.store.revision

    def unsubscribe(self, key):
        with self.store.mutation_guard():
            self.subscribers.pop(key, None)

    def publish(self, documents, removed):
        copied = tuple(snapshot(self.store._document_payload(document)) for document in documents)
        for document_id in (*removed, *(document.document_id for document in copied)):
            self.by_token.pop(self.by_document.pop(document_id, ''), None)
        for document in copied:
            address = token(document)
            self.by_document[document.document_id] = address
            self.by_token[address] = document.document_id
        change = SourceChange(self.epoch, self.store.revision, copied, tuple(removed))
        for callback in tuple(self.subscribers.values()):
            try:
                callback(change)
            except Exception:
                self.failed_revision = change.revision

    def count(self):
        with self.store.mutation_guard():
            self.store._ensure_active()
            return len(self.by_document)

    def seek(self, after='', *, limit=101, previous=False):
        if type(limit) is not int or not 1 <= limit <= MAX_READ:
            raise ValueError('source read limit must be between 1 and 500')
        comparison, direction = ('<', 'DESC') if previous else ('>', 'ASC')
        with self.store.mutation_guard():
            self.store._ensure_active()
            return tuple(snapshot(json.loads(row[0])) for row in self.store._registry.execute(
                f'SELECT payload FROM source_document WHERE document_id{comparison}? '
                f'ORDER BY document_id {direction} LIMIT ?', (after, limit)))

    def read(self, document_ids):
        if len(document_ids) > MAX_READ:
            raise ValueError('source read accepts at most 500 document ids')
        if not document_ids:
            return ()
        with self.store.mutation_guard():
            self.store._ensure_active()
            marks = ','.join('?' for _ in document_ids)
            return tuple(snapshot(json.loads(row[0])) for row in self.store._registry.execute(
                f'SELECT payload FROM source_document WHERE document_id IN ({marks})', tuple(document_ids)))

    def lookup(self, address):
        with self.store.mutation_guard():
            document_id = self.by_token.get(address)
            found = self.read([document_id]) if document_id else ()
            if not found or token(found[0]) != address:
                raise KeyError(address)
            return found[0]


def registry_documents(store):
    """Keep registry startup decoding beside the bounded registry interface."""
    import sqlite3
    if store._registry is None:
        raise RuntimeError('pilot source registry is unavailable')
    try:
        loaded = []
        for document_id, encoded in store._registry.execute(
                'SELECT document_id,payload FROM source_document ORDER BY ordinal'):
            document = store._decode_document(json.loads(encoded))
            if document.document_id != document_id:
                raise ValueError
            loaded.append(document)
        return loaded
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError('pilot source registry could not be loaded') from exc
