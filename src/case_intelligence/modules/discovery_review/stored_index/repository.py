"""Portable queries and explicitly budgeted transactions on the borrowed database."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import sqlite3

from case_intelligence.modules.discovery_review import load_files, reader
from case_intelligence.modules.discovery_review.stored_index import keys
from case_intelligence.modules.discovery_review.stored_index.models import CapacityPlan

BATCH = 100


def packed(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), default=sorted)


def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()


def text_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Transaction:
    def __init__(self, repository, budget, capacity):
        self.repository, self.budget, self.capacity = repository, budget, capacity
        self.active = True
        self.encoded_bytes = 0

    def read(self, statement, args=(), *, maximum=1, constituents=1):
        if not self.active or not 1 <= maximum <= 32:
            raise RuntimeError('use an active bounded index transaction')
        rows = self.repository.rows(statement, args)
        if len(rows) > maximum:
            raise RuntimeError('index read exceeded its declared row bound')
        self.budget.charge(len(rows) * constituents, credit=not rows)
        return rows

    def write(self, statement, args=()):
        if not self.active:
            raise RuntimeError('use an active bounded index transaction')
        estimate = sum(len(value) if isinstance(value, bytes) else len(str(value).encode()) for value in args)
        self.encoded_bytes += estimate * 4 + 4096
        if self.encoded_bytes > self.capacity:
            raise CapacityPlan(self.encoded_bytes)
        cursor = self.repository.db.connection.execute(self.repository.sql(statement), args)
        affected = cursor.rowcount
        if affected not in (0, 1):
            raise RuntimeError('index mutations require a single primary key')
        self.budget.charge(affected)
        return affected


class Repository:
    def __init__(self, db):
        self.db = db
        self.sqlite = isinstance(db.connection, sqlite3.Connection)

    def sql(self, statement):
        return statement if self.sqlite else statement.replace('?', '%s')

    @contextmanager
    def transaction(self, budget, capacity):
        with self.db.lock:
            tx = Transaction(self, budget, capacity)
            try:
                if self.sqlite:
                    with self.db.connection:
                        self.db.connection.execute('BEGIN IMMEDIATE')
                        yield tx
                else:
                    with self.db.connection.transaction():
                        yield tx
            finally:
                tx.active = False

    def rows(self, statement, args=()):
        with self.db.lock:
            return tuple(tuple(row) for row in self.db.connection.execute(self.sql(statement), args))

    def state(self, matter_id, tx=None):
        select = tx.read if tx else self.rows
        found = select('SELECT epoch,revision,generation,definition_digest,key_version,total,indexed,pending,errors,phase,summaries,checkpoint '
                       'FROM workbench_discovery_index_state WHERE matter_id=?', (matter_id,))
        return dict(zip(('epoch', 'revision', 'generation', 'definition_digest', 'key_version', 'total',
                         'indexed', 'pending', 'errors', 'phase', 'summaries', 'checkpoint'), found[0])) if found else None

    def save(self, tx, matter_id, cursor, summaries, exists):
        target, counts = cursor['target'], cursor['counts']
        values = (target[0], target[1], cursor['generation'], target[4], keys.VERSION, counts['total'],
                  counts['indexed'], counts['total'] - counts['indexed'] - counts['errors'], counts['errors'],
                  cursor['phase'], packed(summaries), packed(cursor))
        if exists:
            tx.write('UPDATE workbench_discovery_index_state SET epoch=?,revision=?,generation=?,definition_digest=?,key_version=?,total=?,'
                     'indexed=?,pending=?,errors=?,phase=?,summaries=?,checkpoint=? WHERE matter_id=?', (*values, matter_id))
        else:
            tx.write('INSERT INTO workbench_discovery_index_state VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (matter_id, *values))

    def inventory(self, tx, matter_id, document, generation):
        path = load_files.path_key(document.relative_path)
        name = path.rsplit('/', 1)[-1]
        stem = name.rsplit('.', 1)[0]
        source = asdict(document)
        existing = tx.read('SELECT document_id FROM workbench_discovery_index_item WHERE matter_id=? AND document_id=?',
                           (matter_id, document.document_id))
        values = (document.version_id, reader.action_token(document.document_id, document.version_id), digest(source), packed(source),
                  path, name, stem, text_hash(path), text_hash(name), text_hash(stem), generation)
        if existing:
            tx.write('UPDATE workbench_discovery_index_item SET version_id=?,token=?,fingerprint=?,source_payload=?,path_key=?,name_key=?,stem_key=?,'
                     'path_hash=?,name_hash=?,stem_hash=?,seen_generation=? WHERE matter_id=? AND document_id=?',
                     (*values, matter_id, document.document_id))
        else:
            tx.write('INSERT INTO workbench_discovery_index_item '
                     '(matter_id,document_id,version_id,token,fingerprint,source_payload,path_key,name_key,stem_key,path_hash,name_hash,stem_hash,seen_generation) '
                     'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (matter_id, document.document_id, *values))

    def document(self, tx, matter_id, document_id):
        found = tx.read('SELECT source_payload,payload,cache_payload,projection_fingerprint,readiness FROM workbench_discovery_index_item '
                        'WHERE matter_id=? AND document_id=?', (matter_id, document_id))
        return found[0] if found else None

    def document_read(self, matter_id, document_id):
        found = self.rows('SELECT source_payload,payload,cache_payload,projection_fingerprint,readiness FROM workbench_discovery_index_item '
                          'WHERE matter_id=? AND document_id=?', (matter_id, document_id))
        return found[0] if found else None

    def documents(self, tx, matter_id, after, generation):
        return tx.read('SELECT document_id FROM workbench_discovery_index_item WHERE matter_id=? AND seen_generation=? '
                       'AND document_id>? ORDER BY document_id LIMIT 1', (matter_id, generation, after))

    def cache(self, tx, matter_id, document_id, cache, *, is_loadfile=None):
        if is_loadfile is None:
            tx.write('UPDATE workbench_discovery_index_item SET cache_payload=? WHERE matter_id=? AND document_id=?',
                     (packed(cache), matter_id, document_id))
        else:
            tx.write('UPDATE workbench_discovery_index_item SET cache_payload=?,is_loadfile=? WHERE matter_id=? AND document_id=?',
                     (packed(cache), int(is_loadfile), matter_id, document_id))

    def clear_values(self, tx, matter_id, document_id, after):
        field, direction = after
        rows = tx.read('SELECT field_key,direction FROM workbench_discovery_index_value WHERE matter_id=? AND document_id=? '
                       'AND (field_key>? OR (field_key=? AND direction>?)) ORDER BY field_key,direction LIMIT 8',
                       (matter_id, document_id, field, field, direction), maximum=8)
        for key, side in rows:
            tx.write('DELETE FROM workbench_discovery_index_value WHERE matter_id=? AND document_id=? AND field_key=? AND direction=?',
                     (matter_id, document_id, key, side))
        return rows

    def clear_load_rows(self, tx, matter_id, source_id, after):
        rows = tx.read('SELECT line FROM workbench_discovery_index_load_row WHERE matter_id=? AND source_id=? AND line>? ORDER BY line LIMIT 8',
                       (matter_id, source_id, after), maximum=8)
        for line, in rows:
            tx.write('DELETE FROM workbench_discovery_index_load_row WHERE matter_id=? AND source_id=? AND line=?', (matter_id, source_id, line))
        return rows

    def load_rows(self, tx, matter_id, source_id, after, limit=8):
        return tx.read('SELECT line,path_value,bates_value,values_json,attached_document,basis FROM workbench_discovery_index_load_row '
                       'WHERE matter_id=? AND source_id=? AND line>? ORDER BY line LIMIT ?', (matter_id, source_id, after, limit), maximum=limit)

    def put_load_row(self, tx, matter_id, source, parser, row):
        order = keys.encode((reader.natural_key(source.relative_path), source.document_id))
        path = load_files.path_key(row.values.get(parser['path_header'], ''))
        bates = row.values.get(parser['bates_header'], '').strip().casefold()
        tx.write('INSERT INTO workbench_discovery_index_load_row '
                 '(matter_id,source_id,line,source_order,order_prefix,path_value,bates_value,path_name_hash,bates_hash,values_json) '
                 'VALUES (?,?,?,?,?,?,?,?,?,?)', (matter_id, source.document_id, row.line, order, order[:keys.PREFIX_BYTES], path, bates,
                                                 text_hash(path.rsplit('/', 1)[-1]), text_hash(bates), packed(row.values)))

    def candidates(self, tx, matter_id, generation, kind, value, after):
        if kind not in ('path', 'name', 'stem'):
            raise ValueError('unsupported dependency key')
        return tx.read(f'SELECT document_id,path_key FROM workbench_discovery_index_item WHERE matter_id=? AND seen_generation=? '
                       f'AND {kind}_hash=? AND {kind}_key=? AND is_loadfile=0 AND document_id>? ORDER BY document_id LIMIT 8',
                       (matter_id, generation, text_hash(value), value, after), maximum=8)

    def attach_row(self, tx, matter_id, source_id, line, target, basis):
        tx.write('UPDATE workbench_discovery_index_load_row SET attached_document=?,basis=? WHERE matter_id=? AND source_id=? AND line=?',
                 (target, basis, matter_id, source_id, line))

    def attachment(self, tx, matter_id, document_id):
        prefix = tx.read('SELECT order_prefix FROM workbench_discovery_index_load_row WHERE matter_id=? AND attached_document=? '
                         'ORDER BY order_prefix LIMIT 1', (matter_id, document_id))
        if not prefix:
            return None
        found = tx.read('SELECT r.source_id,r.line,r.basis,r.values_json,i.source_payload FROM workbench_discovery_index_load_row r '
                        'JOIN workbench_discovery_index_item i ON i.matter_id=r.matter_id AND i.document_id=r.source_id '
                        'WHERE r.matter_id=? AND r.attached_document=? AND r.order_prefix=? ORDER BY r.source_order,r.line LIMIT 1',
                        (matter_id, document_id, bytes(prefix[0][0])), constituents=2)
        return found[0] if found else None

    def value(self, tx, matter_id, item, chosen, descending):
        order = keys.order_key(chosen, item, descending)
        tx.write('INSERT INTO workbench_discovery_index_value VALUES (?,?,?,?,?,?,?,?)',
                 (matter_id, item.document_id, chosen.key, int(descending), order[:keys.PREFIX_BYTES], order,
                  item.value(chosen.key).casefold(), int(item.has(chosen.key))))

    def publish(self, tx, matter_id, item, generation, fingerprint, cache):
        tx.write('UPDATE workbench_discovery_index_item SET payload=?,cache_payload=?,projection_fingerprint=?,readiness=?,ready_generation=? '
                 'WHERE matter_id=? AND document_id=?', (packed(asdict(item)), packed(cache), fingerprint, item.metadata_state,
                                                        generation, matter_id, item.document_id))
    def read_items(self, matter_id, document_ids):
        if len(document_ids) > 500:
            raise ValueError('item read accepts at most 500 document ids')
        if not document_ids:
            return {}
        marks = ','.join('?' for _ in document_ids)
        return {row[0]: row[1:] for row in self.rows(
            f'SELECT document_id,payload,fingerprint,readiness,ready_generation FROM workbench_discovery_index_item '
            f'WHERE matter_id=? AND document_id IN ({marks})', (matter_id, *document_ids))}

    def page(self, matter_id, field, descending=False, *, boundary=b'', previous=False, limit=101):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('page limit must be between 1 and 500')
        comparison, direction = ('<', 'DESC') if previous else ('>', 'ASC')
        args = [matter_id, field, int(descending)]
        restriction = ''
        if boundary:
            restriction = f' AND leading_key{comparison}=? AND sort_key{comparison}?'
            args.extend((boundary[:keys.PREFIX_BYTES], boundary))
        prefixes = self.rows('SELECT leading_key FROM workbench_discovery_index_value '
                             'WHERE matter_id=? AND field_key=? AND direction=?' + restriction +
                             f' ORDER BY leading_key {direction} LIMIT ?', (*args, limit))
        if not prefixes:
            return ()
        edge = bytes(prefixes[-1][0])
        restriction += ' AND leading_key>=?' if previous else ' AND leading_key<=?'
        found = self.rows('SELECT sort_key,document_id FROM workbench_discovery_index_value '
                          'WHERE matter_id=? AND field_key=? AND direction=?' + restriction +
                          f' ORDER BY sort_key {direction} LIMIT ?', (*args, edge, limit))
        return tuple((bytes(key), document) for key, document in found)
