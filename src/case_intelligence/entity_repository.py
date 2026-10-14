"""Entity persistence inside the control store's authorized unit of work.

No independent connection, implicit transaction, or name-based identity lookup.
The service owns the source guard; this repository owns the workspace transaction.
"""
from contextlib import contextmanager
import hashlib
import json
import re
import uuid

from .workspace_store import WorkspaceProblem

# The first history entry of an identity suggested from an answer's cited passage.
ANSWER_HISTORY_ACTION = 'suggested from an answer passage'


class EntityEditConflict(WorkspaceProblem):
    pass


class EntityRepository:
    def __init__(self, *, connection, lock, authorize, now, read_authorize=None):
        self.connection = connection
        self.lock = lock
        self.authorize = authorize
        # Authorization for reading(); it must execute only SELECT statements.
        self.read_authorize = read_authorize or authorize
        self.now = now

    @contextmanager
    def transaction(self, matter_id, actor_id):
        with self.lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            self.authorize(matter_id, actor_id)
            yield self

    @contextmanager
    def reading(self, matter_id, actor_id):
        """A deferred read transaction: no writer reservation is taken."""
        with self.lock, self.connection:
            self.connection.execute('BEGIN')
            self.read_authorize(matter_id, actor_id)
            yield self

    @staticmethod
    def record(row):
        result = dict(row)
        result['aliases'] = json.loads(result.pop('aliases_json'))
        result['notebook_snapshot'] = json.loads(result.pop('notebook_snapshot_json') or 'null')
        return result

    def get(self, matter_id, entity_id):
        row = self.connection.execute(
            'SELECT * FROM workbench_entity WHERE matter_id=? AND entity_id=?',
            (matter_id, entity_id),
        ).fetchone()
        if row is None:
            raise KeyError(entity_id)
        return self.record(row)

    def list(self, matter_id, query='', page=1):
        # Escape wildcard characters: name/alias search never changes identities.
        like = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        where = "matter_id=? AND (display_name LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM json_each(aliases_json) WHERE value LIKE ? ESCAPE '\\'))"
        params = (matter_id, like, like)
        total = self.connection.execute('SELECT COUNT(*) FROM workbench_entity WHERE ' + where, params).fetchone()[0]
        rows = self.connection.execute(
            'SELECT * FROM workbench_entity WHERE ' + where + ' ORDER BY display_name,entity_id LIMIT 50 OFFSET ?',
            (*params, (page - 1) * 50),
        ).fetchall()
        return [self.record(row) for row in rows], total

    def mentions(self, matter_id, entity_id):
        return [dict(row) for row in self.connection.execute(
            'SELECT * FROM workbench_entity_mention WHERE matter_id=? AND entity_id=? ORDER BY created_at,mention_id',
            (matter_id, entity_id),
        )]

    def history(self, matter_id, entity_id):
        return [dict(row) for row in self.connection.execute(
            'SELECT * FROM workbench_entity_history WHERE matter_id=? AND entity_id=? ORDER BY revision DESC',
            (matter_id, entity_id),
        )]

    def check_revision(self, matter_id, entity_id, expected_revision):
        current = self.get(matter_id, entity_id)
        if current['revision'] != expected_revision:
            raise EntityEditConflict('This entity changed since you opened it. Compare the saved version with your unsaved work before saving again.')
        return current

    def imported(self, matter_id, item_id):
        row = self.connection.execute(
            'SELECT entity_id FROM workbench_entity WHERE matter_id=? AND notebook_item_id=?',
            (matter_id, item_id),
        ).fetchone()
        return self.get(matter_id, row[0]) if row else None

    def create(self, matter_id, actor_id, fields, *, notebook=None):
        entity_id = 'entity-' + uuid.uuid4().hex
        now = self.now()
        self.connection.execute(
            'INSERT INTO workbench_entity(entity_id,matter_id,entity_type,display_name,status,aliases_json,origin,'
            'notebook_item_id,notebook_snapshot_json,created_by,created_at,updated_by,updated_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (entity_id, matter_id, fields['entity_type'], fields['display_name'], fields['status'],
             json.dumps(fields['aliases']), 'notebook' if notebook else 'manual',
             notebook['item_id'] if notebook else None, json.dumps(notebook) if notebook else None,
             actor_id, now, actor_id, now),
        )
        return self.get(matter_id, entity_id)

    def save(self, matter_id, actor_id, entity_id, fields):
        self.connection.execute(
            'UPDATE workbench_entity SET entity_type=?,display_name=?,status=?,aliases_json=?,'
            'revision=revision+1,updated_by=?,updated_at=? WHERE matter_id=? AND entity_id=?',
            (fields['entity_type'], fields['display_name'], fields['status'], json.dumps(fields['aliases']),
             actor_id, self.now(), matter_id, entity_id),
        )

    # Every entity type has a filter, so a suggestion a reviewer re-typed (for
    # example to a place) stays counted and reachable.
    INBOX_KINDS = {'people': ('person',), 'organizations': ('organization',), 'places': ('place',),
                   'things': ('thing', 'identifier'), 'dates': ('date',)}

    @staticmethod
    def snippet(excerpt, name, radius=90):
        """A short window around the first occurrence, as (before, match, after)."""
        text = ' '.join(excerpt.split())
        # Case-folding can change length (ß -> ss), so match in the folded text
        # and map positions back to the original characters.
        folded, origin = [], []
        for index, char in enumerate(text):
            for part in char.casefold():
                folded.append(part)
                origin.append(index)
        needle = ' '.join(name.split()).casefold()
        found = ''.join(folded).find(needle) if needle else -1
        if found < 0:
            return (text[:2 * radius] + ('…' if len(text) > 2 * radius else ''), '', '')
        at, end_match = origin[found], origin[found + len(needle) - 1] + 1
        start, end = max(0, at - radius), min(len(text), end_match + radius)
        return (('…' if start else '') + text[start:at], text[at:end_match],
                text[end_match:end] + ('…' if end < len(text) else ''))

    @classmethod
    def snippet_at(cls, excerpt, start, end, radius=90):
        """Like snippet(), around the recorded occurrence at [start, end)."""
        squash = lambda text: re.sub(r'\s+', ' ', text)
        before = excerpt[max(0, start - radius):start]
        after = excerpt[end:end + radius]
        return (('…' if start > radius else '') + squash(before).lstrip(), squash(excerpt[start:end]),
                squash(after).rstrip() + ('…' if end + radius < len(excerpt) else ''))

    @classmethod
    def highlight(cls, mention, name):
        """Set mention['snippet'] around the recorded occurrence when it is still exact,
        else around what was extracted (a reviewer may have renamed it), else the name."""
        start, end, surface = mention.get('start_offset'), mention.get('end_offset'), mention.get('surface_text')
        recorded = surface and isinstance(start, int) and isinstance(end, int) and 0 <= start < end
        if recorded and end > len(mention['excerpt']):
            # The occurrence lies past the retained excerpt prefix: show what was
            # found rather than unrelated opening text or an earlier match.
            mention['snippet'] = ('…', ' '.join(surface.split()), '…')
            mention['beyond_excerpt'] = True
        elif recorded and mention['excerpt'][start:end] == surface:
            mention['snippet'] = cls.snippet_at(mention['excerpt'], start, end)
        else:
            for candidate in (surface or '', name):
                mention['snippet'] = cls.snippet(mention['excerpt'], candidate)
                if mention['snippet'][1]:
                    break
        return mention

    DATE_DRAFT_STATUSES = ('suggested', 'needs_review', 'confirmed', 'disputed')

    def date_draft(self, matter_id, page=1, page_size=25):
        """Each passage stating an automatically found date, for a draft timeline.

        Only passages discovery itself found and recorded as dates are listed,
        whichever date identity now holds them. Only a full calendar date
        naming a real day is placed in order, by calendar day; any other date (day/month order unknown, an
        impossible day) keeps its order unestablished and is never guessed; those
        follow, by source and position. Times and offsets are shown as stated but
        not used for ordering.
        """
        stated = "COALESCE(NULLIF(m.surface_text,''),e.display_name)"
        day = f'substr({stated},1,10)'
        year, month = f'CAST(substr({stated},1,4) AS INTEGER)', f'CAST(substr({stated},6,2) AS INTEGER)'
        # Calendar arithmetic, not date(): SQLite builds differ on impossible days.
        leap = f'({year}%4=0 AND ({year}%100<>0 OR {year}%400=0))'
        month_days = f'CASE WHEN {month}=2 THEN 28+{leap} ELSE 30+({month}+{month}/8)%2 END'
        # A full date, alone or with a stated time in the forms discovery recognises.
        clock, digits = '[T ][0-9][0-9]:[0-9][0-9]', '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        forms = [digits + clock + seconds + zone for seconds in ('', ':[0-9][0-9]')
                 for zone in ('', 'Z', '[-+][0-9][0-9]:[0-9][0-9]')]
        shaped = ' OR '.join([f"{stated} GLOB '{digits}'"] + [f"{stated} GLOB '{form}'" for form in forms])
        ordered = (f"(({shaped}) AND {year} >= 1 AND {month} BETWEEN 1 AND 12 "
                   f'AND CAST(substr({stated},9,2) AS INTEGER) BETWEEN 1 AND {month_days})')
        marks = ','.join('?' * len(self.DATE_DRAFT_STATUSES))
        # One pass over the matter's passages, each joined to its identity by key.
        base = ('FROM workbench_entity_mention m CROSS JOIN workbench_entity e ON e.entity_id=m.entity_id '
                "WHERE m.matter_id=? AND m.origin='extraction' AND json_extract(m.date_json,'$.kind')='date' "
                "AND e.matter_id=m.matter_id AND e.entity_type='date' "
                f'AND e.status IN ({marks}) AND m.review_status IN ({marks})')
        params = (matter_id, *self.DATE_DRAFT_STATUSES, *self.DATE_DRAFT_STATUSES)
        total, ordered_total = self.connection.execute(
            f'SELECT COUNT(*),COALESCE(SUM({ordered}),0) ' + base, params).fetchone()
        rows = self.connection.execute(
            f'SELECT m.*,e.display_name,e.status AS entity_status,{stated} AS stated,{ordered} AS ordered '
            f'{base} ORDER BY ordered DESC,CASE WHEN {ordered} THEN {day} END,m.source_name,m.unit_number,'
            'm.start_offset,m.mention_id LIMIT ? OFFSET ?',
            (*params, page_size, (page - 1) * page_size)).fetchall()
        items = []
        for index, row in enumerate(rows):
            mention = self.highlight(dict(row), row['stated'])
            mention['ordered'] = bool(row['ordered'])
            mention['position'] = (page - 1) * page_size + index + 1
            # Discovery records {"kind": ..., "date": {"raw", "ambiguity", ...}} per occurrence.
            recorded = json.loads(mention.get('date_json') or 'null') or {}
            mention['detail'] = recorded.get('date') if isinstance(recorded.get('date'), dict) else {}
            items.append(mention)
        return items, total, ordered_total

    INBOX_GROUP_LIMIT = 200

    def suggestion_inbox(self, matter_id, kind='', page=1, page_size=25, *, rows=True):
        """Suggested identities grouped by name and type, most-mentioned first.

        Grouping is presentation only: each identity stays separate and is
        decided individually under its own revision.
        """
        types = self.INBOX_KINDS.get(kind, ())
        # Only identities found automatically; manual or notebook ones keep their own provenance.
        where = "e.matter_id=? AND e.status='suggested' AND e.origin='extraction'"
        params = [matter_id]
        if types:
            where += ' AND e.entity_type IN (' + ','.join('?' * len(types)) + ')'
            params.extend(types)
        counts = {row[0]: row[1] for row in self.connection.execute(
            "SELECT entity_type,COUNT(DISTINCT display_name) FROM workbench_entity WHERE matter_id=? "
            "AND status='suggested' AND origin='extraction' GROUP BY entity_type", (matter_id,))}
        kinds = {name: sum(counts.get(value, 0) for value in values) for name, values in self.INBOX_KINDS.items()}
        if not rows:
            return [], 0, kinds
        total = self.connection.execute(
            'SELECT COUNT(*) FROM (SELECT 1 FROM workbench_entity e WHERE ' + where +
            ' GROUP BY e.entity_type,e.display_name)', params).fetchone()[0]
        # One pass over the matter's mentions, each joined to its identity by primary
        # key (CROSS JOIN fixes that order), then grouped; nothing rescans mentions
        # per group. The first passage is chosen in the same pass and read by key.
        groups = self.connection.execute(
            'WITH identities AS MATERIALIZED (SELECT e.entity_type,e.display_name,COUNT(*) AS identity_count '
            'FROM workbench_entity e WHERE ' + where + ' GROUP BY e.entity_type,e.display_name), '
            'found AS MATERIALIZED (SELECT e.entity_type,e.display_name,COUNT(*) AS mention_count,'
            'COUNT(DISTINCT m.document_id) AS source_count,'
            'MIN(m.created_at || char(31) || m.mention_id) AS first_key '
            'FROM workbench_entity_mention m CROSS JOIN workbench_entity e ON e.entity_id=m.entity_id '
            'WHERE m.matter_id=? AND ' + where + ' GROUP BY e.entity_type,e.display_name) '
            'SELECT i.display_name,i.entity_type,i.identity_count,COALESCE(f.mention_count,0) AS mention_count,'
            'COALESCE(f.source_count,0) AS source_count,f.first_key FROM identities i LEFT JOIN found f '
            'ON f.entity_type=i.entity_type AND f.display_name=i.display_name '
            'ORDER BY mention_count DESC,i.display_name,i.entity_type LIMIT ? OFFSET ?',
            (*params, matter_id, *params, page_size, (page - 1) * page_size)).fetchall()
        items = []
        for group in groups:
            members = [dict(row) for row in self.connection.execute(
                "SELECT entity_id,revision FROM workbench_entity WHERE matter_id=? AND status='suggested' "
                "AND origin='extraction' AND display_name=? AND entity_type=? ORDER BY created_at,entity_id LIMIT ?",
                (matter_id, group['display_name'], group['entity_type'], self.INBOX_GROUP_LIMIT))]
            first = self.connection.execute(
                'SELECT * FROM workbench_entity_mention WHERE mention_id=? AND matter_id=?',
                (group['first_key'].split('\x1f', 1)[1], matter_id)).fetchone() if group['first_key'] else None
            mention = dict(first) if first else None
            if mention:
                self.highlight(mention, group['display_name'])
            if mention and mention['entity_id'] not in {row['entity_id'] for row in members}:
                # The row opens the passage's owner, so a bounded decision must include it.
                owner = self.connection.execute(
                    'SELECT entity_id,revision FROM workbench_entity WHERE matter_id=? AND entity_id=?',
                    (matter_id, mention['entity_id'])).fetchone()
                if owner is not None:
                    members = [dict(owner), *members[:self.INBOX_GROUP_LIMIT - 1]]
            # Identities a reviewer suggested from an answer's cited passage keep that
            # provenance; a group mixing them with automatic ones says how many.
            from_answer = self.connection.execute(
                "SELECT COUNT(*) FROM workbench_entity e JOIN workbench_entity_history h "
                "ON h.entity_id=e.entity_id AND h.revision=1 WHERE e.matter_id=? AND e.status='suggested' "
                "AND e.origin='extraction' AND e.display_name=? AND e.entity_type=? AND h.action=?",
                (matter_id, group['display_name'], group['entity_type'], ANSWER_HISTORY_ACTION)).fetchone()[0]
            group = {key: group[key] for key in group.keys() if key != 'first_key'}
            items.append(dict(group, members=members, first_mention=mention, from_answer=from_answer,
                              # Open the identity that owns the passage shown.
                              entity_id=mention['entity_id'] if mention else members[0]['entity_id'] if members else '',
                              targets=','.join(f"{row['entity_id']}:{row['revision']}" for row in members)))
        return items, total, kinds

    def has_mention(self, matter_id, entity_id, support_token):
        return self.connection.execute(
            'SELECT 1 FROM workbench_entity_mention WHERE matter_id=? AND entity_id=? AND support_token=?',
            (matter_id, entity_id, support_token),
        ).fetchone() is not None

    def add_mention(self, matter_id, actor_id, entity_id, reference, origin):
        names = ('document_id', 'source_version_id', 'source_name', 'location', 'unit_number',
                 'chunk_id', 'excerpt_digest', 'excerpt', 'support_token')
        mention_id = 'mention-' + uuid.uuid4().hex
        self.connection.execute(
            'INSERT INTO workbench_entity_mention(mention_id,matter_id,entity_id,' + ','.join(names)
            + ',origin,created_by,created_at) VALUES (' + ','.join('?' for _ in range(15)) + ')',
            (mention_id, matter_id, entity_id, *(reference[name] for name in names),
             origin, actor_id, self.now()),
        )

        return dict(self.connection.execute(
            'SELECT * FROM workbench_entity_mention WHERE mention_id=?', (mention_id,)).fetchone())

    def remove_mention(self, matter_id, entity_id, mention_id):
        removed = self.connection.execute(
            'SELECT * FROM workbench_entity_mention WHERE matter_id=? AND entity_id=? AND mention_id=?',
            (matter_id, entity_id, mention_id),
        ).fetchone()
        if removed is None:
            raise KeyError(mention_id)
        cursor = self.connection.execute(
            'DELETE FROM workbench_entity_mention WHERE matter_id=? AND entity_id=? AND mention_id=?',
            (matter_id, entity_id, mention_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(mention_id)
        return dict(removed)

    def record_history(self, matter_id, actor_id, entity_id, action, *, added_mentions=(), removed_mentions=()):
        current = self.get(matter_id, entity_id)
        # Store each mention only at addition/removal, never in every later edit.
        # Old full snapshots remain readable; new records describe explicit deltas.
        snapshot = dict(current, history_format=2, added_mentions=list(added_mentions),
                        removed_mentions=list(removed_mentions))
        if action not in ('created', 'notebook imported'):
            snapshot.pop('notebook_snapshot', None)
        self.connection.execute(
            'INSERT INTO workbench_entity_history VALUES (?,?,?,?,?,?,?)',
            (entity_id, matter_id, current['revision'], action, json.dumps(snapshot), actor_id, self.now()),
        )
        self.connection.execute('UPDATE workbench_matter SET updated_at=? WHERE matter_id=?', (self.now(), matter_id))

    def delete(self, matter_id, entity_id):
        self.connection.execute("DELETE FROM workbench_entity_reconciliation WHERE matter_id=? AND "
            "(json_extract(before_json,'$.source.entity_id')=? OR json_extract(before_json,'$.target.entity_id')=?)",
            (matter_id, entity_id, entity_id))
        self.connection.execute('DELETE FROM workbench_entity WHERE matter_id=? AND entity_id=?', (matter_id, entity_id))
        self.connection.execute('UPDATE workbench_matter SET updated_at=? WHERE matter_id=?', (self.now(), matter_id))

    def export_records(self, matter_id):
        for row in self.connection.execute(
                'SELECT entity_id FROM workbench_entity WHERE matter_id=? ORDER BY entity_id', (matter_id,)):
            entity_id = row[0]
            yield dict(format='recordbench-entity-v1', entity=self.get(matter_id, entity_id),
                       mentions=self.mentions(matter_id, entity_id), history=self.history(matter_id, entity_id),
                       reconciliations=self.reconciliations(matter_id, entity_id),
                       source_support='Retained original-source support; availability is not revalidated in this bundle.')

    def discovery_runs(self, matter_id, page=1):
        return [dict(row) for row in self.connection.execute(
            'SELECT r.run_id,r.state,r.created_at FROM workbench_review_run r '
            'JOIN workbench_text_review t ON t.run_id=r.run_id WHERE r.matter_id=? ORDER BY r.created_at DESC,r.run_id LIMIT 21 OFFSET ?', (matter_id, (page - 1) * 20))]

    def require_discovery_run(self, matter_id, run_id):
        if not self.connection.execute('SELECT 1 FROM workbench_review_run r JOIN workbench_text_review t '
                'ON t.run_id=r.run_id WHERE r.matter_id=? AND r.run_id=?', (matter_id, run_id)).fetchone():
            raise KeyError(run_id)

    def discovery_pending(self, matter_id, run_id, version, limit, retry):
        return [dict(row) for row in self.connection.execute(
            "SELECT ? AS matter_id,u.run_id,u.document_id,s.source_version_id,u.unit_ordinal,u.unit_digest,"
            "? AS extractor_version,COALESCE(d.state,'pending') AS state,COALESCE(d.note,'') AS note "
            "FROM workbench_text_review_source s JOIN workbench_text_review_unit u "
            "ON u.run_id=s.run_id AND u.document_id=s.document_id "
            "LEFT JOIN workbench_entity_discovery_unit d ON d.matter_id=? AND d.run_id=u.run_id "
            "AND d.document_id=u.document_id AND d.unit_ordinal=u.unit_ordinal AND d.extractor_version=? "
            "WHERE s.run_id=? AND s.inventory_sealed=1 AND "
            "((? AND d.state='failed') OR (NOT ? AND (d.state IS NULL OR d.state='pending'))) "
            "ORDER BY u.document_id,u.unit_ordinal LIMIT ?",
            (matter_id, version, matter_id, version, run_id, retry, retry, limit))]

    def seed_discovery_unit(self, unit):
        self.connection.execute("INSERT OR IGNORE INTO workbench_entity_discovery_unit "
            "(matter_id,run_id,document_id,source_version_id,unit_ordinal,unit_digest,extractor_version,state) "
            "VALUES (?,?,?,?,?,?,?,'pending')",
            tuple(unit[key] for key in ('matter_id','run_id','document_id','source_version_id',
                                       'unit_ordinal','unit_digest','extractor_version')))

    def discovery_storage_bytes(self, matter_id):
        # A logical payload budget, with per-row allowance. Include human records
        # and retained history so repeated extractor versions cannot reset it.
        total = 0
        for table in ('workbench_entity', 'workbench_entity_mention', 'workbench_entity_history',
                      'workbench_entity_discovery_seen', 'workbench_entity_discovery_unit',
                      'workbench_entity_reconciliation'):
            columns = [row[1] for row in self.connection.execute(f'PRAGMA table_info({table})')]
            sizes = '+'.join(f'COALESCE(length(CAST("{column}" AS BLOB)),0)' for column in columns)
            total += self.connection.execute(f'SELECT COALESCE(SUM(256+{sizes}),0) FROM {table} WHERE matter_id=?',
                (matter_id,)).fetchone()[0]
        return total

    def has_discovery_receipt(self, matter_id, key):
        return self.connection.execute('SELECT 1 FROM workbench_entity_discovery_seen WHERE matter_id=? AND occurrence_key=?',
            (matter_id, key)).fetchone() is not None

    def discovery_source_current(self, matter_id, unit):
        return self.connection.execute(
            "SELECT 1 FROM workbench_text_review_source s JOIN workbench_source_catalog c "
            "ON c.document_id=s.document_id AND c.matter_id=? WHERE s.run_id=? AND s.document_id=? "
            "AND s.state!='invalidated' AND s.inventory_sealed=1 AND c.source_state='ready' "
            "AND c.version_id=s.source_version_id AND c.content_basis_digest=s.source_basis_digest",
            (matter_id, unit['run_id'], unit['document_id'])).fetchone() is not None

    def discovery_claimable(self, matter_id, unit, retry):
        row = self.connection.execute(
            'SELECT state FROM workbench_entity_discovery_unit WHERE matter_id=? AND run_id=? AND document_id=? '
            'AND unit_ordinal=? AND extractor_version=?',
            (matter_id, unit['run_id'], unit['document_id'], unit['unit_ordinal'], unit['extractor_version'])).fetchone()
        return (row is not None and row[0] == 'failed') if retry else (row is None or row[0] == 'pending')

    def discovery_state(self, unit, state, note):
        self.connection.execute('UPDATE workbench_entity_discovery_unit SET state=?,note=? WHERE matter_id=? '
            'AND run_id=? AND document_id=? AND unit_ordinal=? AND extractor_version=?',
            (state, note, unit['matter_id'], unit['run_id'], unit['document_id'], unit['unit_ordinal'], unit['extractor_version']))

    # Automatic discovery: one ledger per current source version and extracted
    # basis, with no review run. "Current" always means the catalog's ready
    # version and content basis; anything else is history.
    # Note on a queued unit that the discovery storage limit rejected; cleared
    # when the unit is later processed.
    AUTO_CAPACITY_NOTE = 'Waiting for discovery storage space.'
    _AUTO_KEY = ('matter_id', 'document_id', 'source_version_id', 'content_basis_digest',
                 'unit_ordinal', 'extractor_version')
    _AUTO_CURRENT = ('a.matter_id=c.matter_id AND a.document_id=c.document_id '
                     'AND a.source_version_id=c.version_id AND a.content_basis_digest=c.content_basis_digest')
    # The automatic coverage ledger is bounded by the sources it describes:
    # one inventory (at most MAX units plus a seal) per current ready source
    # version and extracted basis. Superseded and removed sources' rows are
    # deleted, so it is outside the suggestion byte budget.

    def auto_discovery_documents(self, matter_id, version, limit):
        """Ready sources whose current basis is unsealed or still has pending units."""
        return [dict(row) for row in self.connection.execute(
            "SELECT c.document_id,c.version_id AS source_version_id,c.content_basis_digest,"
            "EXISTS (SELECT 1 FROM workbench_entity_auto_discovery_unit a WHERE " + self._AUTO_CURRENT +
            " AND a.extractor_version=? AND a.unit_ordinal=0) AS sealed "
            "FROM workbench_source_catalog c WHERE c.matter_id=? AND c.source_state='ready' AND ("
            "NOT EXISTS (SELECT 1 FROM workbench_entity_auto_discovery_unit a WHERE " + self._AUTO_CURRENT +
            " AND a.extractor_version=? AND a.unit_ordinal=0) OR EXISTS (SELECT 1 FROM "
            "workbench_entity_auto_discovery_unit a WHERE " + self._AUTO_CURRENT +
            " AND a.extractor_version=? AND a.state='pending')) ORDER BY "
            # Sources with admissible work first; ones only blocked by the storage limit last.
            "(NOT EXISTS (SELECT 1 FROM workbench_entity_auto_discovery_unit a WHERE " + self._AUTO_CURRENT +
            " AND a.extractor_version=? AND a.unit_ordinal=0) OR EXISTS (SELECT 1 FROM "
            "workbench_entity_auto_discovery_unit a WHERE " + self._AUTO_CURRENT +
            " AND a.extractor_version=? AND a.state='pending' AND a.note='')) DESC, "
            # Sources whose waiting work was retried most recently rotate to the back.
            "COALESCE((SELECT a.wait_round FROM workbench_entity_auto_discovery_unit a WHERE " + self._AUTO_CURRENT +
            " AND a.extractor_version=? AND a.unit_ordinal=0), 0), c.document_id LIMIT ?",
            (version, matter_id, version, version, version, version, version, limit))]

    def auto_discovery_pending(self, matter_id, document, version, limit):
        return [dict(row) for row in self.connection.execute(
            'SELECT * FROM workbench_entity_auto_discovery_unit WHERE matter_id=? AND document_id=? '
            "AND source_version_id=? AND content_basis_digest=? AND extractor_version=? AND unit_ordinal>0 "
            "AND state='pending' ORDER BY note<>'', wait_round, unit_ordinal LIMIT ?",
            (matter_id, document['document_id'], document['source_version_id'],
             document['content_basis_digest'], version, limit))]

    def seal_auto_discovery(self, matter_id, document, version, units, state='processed', note=''):
        """Record the unit inventory of one version and basis once; supersede older ones."""
        key = (matter_id, document['document_id'], document['source_version_id'], document['content_basis_digest'])
        if self.connection.execute(
                'SELECT 1 FROM workbench_entity_auto_discovery_unit WHERE matter_id=? AND document_id=? '
                'AND source_version_id=? AND content_basis_digest=? AND extractor_version=? AND unit_ordinal=0',
                (*key, version)).fetchone():
            return False
        self.connection.executemany(
            "INSERT OR IGNORE INTO workbench_entity_auto_discovery_unit (matter_id,document_id,source_version_id,"
            "content_basis_digest,unit_ordinal,unit_digest,extractor_version,state) VALUES (?,?,?,?,?,?,?,'pending')",
            [(*key, ordinal, digest, version) for ordinal, digest in units])
        self.connection.execute(
            "INSERT INTO workbench_entity_auto_discovery_unit (matter_id,document_id,source_version_id,"
            "content_basis_digest,unit_ordinal,unit_digest,extractor_version,state,note) VALUES (?,?,?,?,0,?,?,?,?)",
            (*key, hashlib.sha256(str(len(units)).encode()).hexdigest(), version, state, note))
        # Earlier versions or extracted bases are dropped from the ledger; their
        # saved suggestions, receipts and every human decision stay as they are.
        # A new extractor version replaces the old inventory the same way.
        self.connection.execute(
            "DELETE FROM workbench_entity_auto_discovery_unit WHERE matter_id=? AND document_id=? "
            "AND NOT (source_version_id=? AND content_basis_digest=? AND extractor_version=?)", (*key, version))
        return True

    def prune_auto_discovery(self, matter_id):
        """Drop ledger rows for sources that are no longer in the catalog."""
        return self.connection.execute(
            "DELETE FROM workbench_entity_auto_discovery_unit WHERE matter_id=? AND NOT EXISTS ("
            "SELECT 1 FROM workbench_source_catalog c WHERE c.matter_id=? "
            "AND c.document_id=workbench_entity_auto_discovery_unit.document_id)",
            (matter_id, matter_id)).rowcount

    def auto_discovery_claimable(self, unit):
        row = self.connection.execute(
            'SELECT state FROM workbench_entity_auto_discovery_unit WHERE matter_id=? AND document_id=? '
            'AND source_version_id=? AND content_basis_digest=? AND unit_ordinal=? AND extractor_version=?',
            tuple(unit[key] for key in self._AUTO_KEY)).fetchone()
        return row is not None and row[0] == 'pending'

    def auto_discovery_source_current(self, matter_id, unit):
        return self.connection.execute(
            "SELECT 1 FROM workbench_source_catalog WHERE matter_id=? AND document_id=? "
            "AND source_state='ready' AND version_id=? AND content_basis_digest=?",
            (matter_id, unit['document_id'], unit['source_version_id'],
             unit['content_basis_digest'])).fetchone() is not None

    def wait_auto_discovery(self, unit):
        """Mark a unit as waiting for space and rotate it behind other waiting units."""
        self.connection.execute(
            'UPDATE workbench_entity_auto_discovery_unit SET note=?,wait_round=wait_round+1 WHERE matter_id=? '
            'AND document_id=? AND source_version_id=? AND content_basis_digest=? AND unit_ordinal=? '
            'AND extractor_version=?', (self.AUTO_CAPACITY_NOTE, *(unit[key] for key in self._AUTO_KEY)))

    def rotate_auto_discovery_source(self, matter_id, document, version):
        """Move a source whose step admitted nothing behind other waiting sources."""
        self.connection.execute(
            'UPDATE workbench_entity_auto_discovery_unit SET wait_round=wait_round+1 WHERE matter_id=? '
            'AND document_id=? AND source_version_id=? AND content_basis_digest=? AND extractor_version=? '
            'AND unit_ordinal=0', (matter_id, document['document_id'], document['source_version_id'],
                                   document['content_basis_digest'], version))

    def block_auto_discovery(self, matter_id, version):
        """Mark every queued unit of the matter as waiting for storage space."""
        self.connection.execute(
            "UPDATE workbench_entity_auto_discovery_unit SET note=? WHERE matter_id=? AND extractor_version=? "
            "AND state='pending' AND note=''", (self.AUTO_CAPACITY_NOTE, matter_id, version))

    def auto_discovery_state(self, unit, state, note):
        self.connection.execute(
            'UPDATE workbench_entity_auto_discovery_unit SET state=?,note=? WHERE matter_id=? AND document_id=? '
            'AND source_version_id=? AND content_basis_digest=? AND unit_ordinal=? AND extractor_version=?',
            (state, note, *(unit[key] for key in self._AUTO_KEY)))

    def auto_discovery_progress(self, matter_id, version, *, detail=False):
        """Source-level progress for current source text, by index seeks per source.

        The readiness poll uses this, so it never counts a matter's unit rows;
        detail=True adds unit counts by state for diagnostics and tests.
        """
        current = ("FROM workbench_source_catalog c WHERE c.matter_id=? AND c.source_state='ready' AND ")
        def exists(condition):
            return ("EXISTS (SELECT 1 FROM workbench_entity_auto_discovery_unit a WHERE " + self._AUTO_CURRENT
                    + " AND a.extractor_version=? AND " + condition + ")")
        def count(predicate, repeats):
            return self.connection.execute("SELECT COUNT(*) " + current + predicate,
                                           (matter_id, *([version] * repeats))).fetchone()[0]
        sealed = exists("a.unit_ordinal=0")
        pending_open = exists("a.state='pending' AND a.note=''")
        pending_blocked = exists("a.state='pending' AND a.note<>''")
        unfinished = exists("a.state IN ('pending','failed','invalidated')")
        incomplete = exists("a.state IN ('failed','invalidated')")
        ready = self.connection.execute(
            "SELECT COUNT(*) FROM workbench_source_catalog WHERE matter_id=? AND source_state='ready'",
            (matter_id,)).fetchone()[0]
        complete = count(sealed + " AND NOT " + unfinished, 2)
        attention = count(incomplete, 1)
        unsealed = count("NOT " + sealed, 1)
        open_sources = count(pending_open, 1)
        blocked_sources = count(pending_blocked, 1)
        suggested = self.connection.execute(
            "SELECT COUNT(*) FROM workbench_entity WHERE matter_id=? AND status='suggested'",
            (matter_id,)).fetchone()[0]
        result = dict(unsealed_sources=unsealed, sources_complete=complete, sources_attention=attention,
                      sources_ready=ready, sources_pending=open_sources + blocked_sources, suggested=suggested,
                      # Paused only when the storage limit blocks everything left to do.
                      budget_reached=bool(blocked_sources and not open_sources and not unsealed))
        if detail:
            counts = {row[0]: row[1] for row in self.connection.execute(
                "SELECT a.state,COUNT(*) FROM workbench_entity_auto_discovery_unit a JOIN workbench_source_catalog c "
                "ON " + self._AUTO_CURRENT + " WHERE a.matter_id=? AND a.extractor_version=? AND a.unit_ordinal>0 "
                "AND c.source_state='ready' GROUP BY a.state", (matter_id, version))}
            result.update(pending=counts.get('pending', 0), processed=counts.get('processed', 0),
                          failed=counts.get('failed', 0))
        return result

    def retry_auto_discovery(self, matter_id, version):
        """Queue failed automatic coverage of current sources again.

        Failed or invalidated units of a current source return to pending; a
        source whose text was unavailable loses its failed seal so it is
        inventoried again. Suggestions,
        receipts and human decisions are untouched. Returns sources affected.
        """
        table = 'workbench_entity_auto_discovery_unit'
        current = (f"EXISTS (SELECT 1 FROM workbench_source_catalog c WHERE c.matter_id={table}.matter_id "
                   f"AND c.source_state='ready' AND c.document_id={table}.document_id "
                   f"AND c.version_id={table}.source_version_id "
                   f"AND c.content_basis_digest={table}.content_basis_digest)")
        failed = f"matter_id=? AND extractor_version=? AND state IN ('failed','invalidated') AND {current}"
        sources = self.connection.execute(
            f"SELECT COUNT(DISTINCT document_id) FROM {table} WHERE {failed}", (matter_id, version)).fetchone()[0]
        self.connection.execute(f"DELETE FROM {table} WHERE unit_ordinal=0 AND state='failed' AND {failed}",
                                (matter_id, version))
        self.connection.execute(f"UPDATE {table} SET state='pending',note='' WHERE unit_ordinal>0 AND {failed}",
                                (matter_id, version))
        return sources

    def discovery_coverage(self, matter_id, run_id, version, *, page=1, source_page=1, limit=50):
        self.require_discovery_run(matter_id, run_id)
        criterion_id = self.connection.execute(
            'SELECT criterion_id FROM workbench_review_run WHERE matter_id=? AND run_id=?',
            (matter_id, run_id)).fetchone()[0]
        current = "(s.state!='invalidated' AND c.source_state='ready' AND c.version_id=s.source_version_id AND c.content_basis_digest=s.source_basis_digest)"
        source_from = (' FROM workbench_text_review_source s LEFT JOIN workbench_source_catalog c '
                       'ON c.document_id=s.document_id AND c.matter_id=? WHERE s.run_id=?')
        source_state = f"CASE WHEN s.inventory_sealed=1 AND NOT COALESCE({current},0) THEN 'invalidated' ELSE s.state END"
        source_counts = {row[0]: row[1] for row in self.connection.execute(
            'SELECT ' + source_state + ',COUNT(*)' + source_from + ' GROUP BY 1', (matter_id, run_id))}
        uninventoried = self.connection.execute('SELECT COUNT(*)' + source_from + ' AND s.inventory_sealed=0',
            (matter_id, run_id)).fetchone()[0]
        sources = [dict(row) for row in self.connection.execute(
            'SELECT s.document_id,s.source_name,s.inventory_sealed,s.unit_count,' + source_state + ' AS state'
            + source_from + ' ORDER BY s.document_id LIMIT ? OFFSET ?',
            (matter_id, run_id, limit, (source_page - 1) * 50))] if limit else []
        # Count in SQLite; only materialize the requested page of unit metadata.
        cte = ("WITH coverage AS (SELECT s.run_id,s.document_id,s.source_name,s.source_version_id,"
            "u.unit_ordinal,u.unit_digest,CASE WHEN NOT COALESCE(" + current + ",0) THEN 'invalidated' "
            "ELSE COALESCE(d.state,'pending') END AS state,"
            "CASE WHEN NOT COALESCE(" + current + ",0) THEN 'Frozen source changed or is unavailable.' "
            "ELSE COALESCE(d.note,'Not yet queued for discovery.') END AS note "
            "FROM workbench_text_review_source s JOIN workbench_text_review_unit u "
            "ON u.run_id=s.run_id AND u.document_id=s.document_id "
            "LEFT JOIN workbench_source_catalog c ON c.document_id=s.document_id AND c.matter_id=? "
            "LEFT JOIN workbench_entity_discovery_unit d ON d.matter_id=? AND d.run_id=s.run_id "
            "AND d.document_id=s.document_id AND d.unit_ordinal=u.unit_ordinal AND d.extractor_version=? "
            "WHERE s.run_id=? AND s.inventory_sealed=1) ")
        params = (matter_id, matter_id, version, run_id)
        counts = dict.fromkeys(('pending','processed','failed','invalidated'), 0)
        counts.update({row[0]: row[1] for row in self.connection.execute(
            cte + 'SELECT state,COUNT(*) FROM coverage GROUP BY state', params)})
        units = [dict(row) for row in self.connection.execute(
            cte + 'SELECT * FROM coverage ORDER BY document_id,unit_ordinal LIMIT ? OFFSET ?',
            (*params, limit, (page - 1) * 50))] if limit else []
        return dict(run_id=run_id, criterion_id=criterion_id, extractor_version=version, units=units, sources=sources, counts=counts,
            unit_total=sum(counts.values()), source_total=sum(source_counts.values()),
            source_counts=source_counts, uninventoried=uninventoried, page=page, source_page=source_page)

    def discovery_seen(self, matter_id, key):
        cursor = self.connection.execute('INSERT OR IGNORE INTO workbench_entity_discovery_seen VALUES (?,?)', (matter_id, key))
        return cursor.rowcount == 0

    def mark_extracted(self, matter_id, entity_id, version):
        self.connection.execute("UPDATE workbench_entity SET origin='extraction',extractor_version=? WHERE matter_id=? AND entity_id=?", (version, matter_id, entity_id))

    def annotate_occurrence(self, matter_id, mention_id, key, version, occurrence):
        self.connection.execute('UPDATE workbench_entity_mention SET occurrence_key=?,extractor_version=?,surface_text=?,'
            'start_offset=?,end_offset=?,date_json=?,review_status=? WHERE matter_id=? AND mention_id=?',
            (key, version, occurrence.label, occurrence.start, occurrence.end,
             json.dumps(dict(kind=occurrence.kind, date=occurrence.date)), 'suggested', matter_id, mention_id))
        return dict(self.connection.execute('SELECT * FROM workbench_entity_mention WHERE matter_id=? AND mention_id=?', (matter_id, mention_id)).fetchone())

    def candidate_page(self, matter_id, entity_id, page):
        entity = self.get(matter_id, entity_id)
        rows = self.connection.execute(
            "SELECT e.* FROM workbench_entity e WHERE e.matter_id=? AND e.entity_id!=? "
            "AND NOT EXISTS (SELECT 1 FROM workbench_entity_reconciliation r WHERE r.matter_id=e.matter_id "
            "AND r.action='reject' AND r.undone=0 AND "
            "((json_extract(r.before_json,'$.source.entity_id')=? AND json_extract(r.before_json,'$.target.entity_id')=e.entity_id) "
            "OR (json_extract(r.before_json,'$.target.entity_id')=? AND json_extract(r.before_json,'$.source.entity_id')=e.entity_id))) "
            "ORDER BY e.display_name,e.entity_id LIMIT 51 OFFSET ?",
            (matter_id, entity_id, entity_id, entity_id, (page - 1) * 50))
        return entity, [self.record(row) for row in rows]

    @staticmethod
    def score_candidates(entity, pool):
        from difflib import SequenceMatcher
        labels = {value.casefold() for value in [entity['display_name'], *entity['aliases']]}
        result = []
        for other in pool:
            other_labels = {value.casefold() for value in [other['display_name'], *other['aliases']]}
            # Exact alias overlap needs no Cartesian fuzzy comparison. Fuzzy
            # matching is confined to one display-name pair per identity.
            score = 1.0 if labels & other_labels else SequenceMatcher(
                None, entity['display_name'].casefold(), other['display_name'].casefold()).ratio()
            if score >= .72:
                result.append(dict(other, match_score=score))
        return sorted(result, key=lambda row: (-row['match_score'], row['entity_id']))

    def reconciliations(self, matter_id, entity_id):
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM workbench_entity_reconciliation WHERE matter_id=? AND "
            "(json_extract(before_json,'$.source.entity_id')=? OR json_extract(before_json,'$.target.entity_id')=?) "
            "ORDER BY created_at DESC,operation_id DESC", (matter_id, entity_id, entity_id))]

    def move_mentions(self, matter_id, source_id, target_id, mention_ids):
        mentions = {row['mention_id']: row for row in self.mentions(matter_id, source_id)}
        if not set(mention_ids) <= mentions.keys():
            raise WorkspaceProblem('A selected mention is no longer on this identity.')
        for mention_id in mention_ids:
            self.connection.execute('UPDATE workbench_entity_mention SET entity_id=? WHERE matter_id=? AND entity_id=? AND mention_id=?',
                (target_id, matter_id, source_id, mention_id))
        return [mentions[key] for key in mention_ids]

    def save_reconciliation(self, matter_id, actor_id, action, before, after):
        operation_id = 'reconcile-' + uuid.uuid4().hex
        self.connection.execute('INSERT INTO workbench_entity_reconciliation '
            '(operation_id,matter_id,action,before_json,after_json,actor_id,created_at) VALUES (?,?,?,?,?,?,?)',
            (operation_id, matter_id, action, json.dumps(before), json.dumps(after), actor_id, self.now()))
        return operation_id

    def get_reconciliation(self, matter_id, operation_id):
        row = self.connection.execute('SELECT * FROM workbench_entity_reconciliation WHERE matter_id=? AND operation_id=?',
            (matter_id, operation_id)).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return dict(row)

    def undo_reconciliation(self, matter_id, operation_id):
        self.connection.execute('UPDATE workbench_entity_reconciliation SET undone=1 WHERE matter_id=? AND operation_id=?',
            (matter_id, operation_id))

    def discovery_export(self, matter_id):
        return dict(format='recordbench-entity-discovery-v1',
            source_support='Retained frozen coverage; current source availability is not revalidated in this bundle.',
            sources=[dict(row) for row in self.connection.execute(
                'SELECT s.* FROM workbench_text_review_source s JOIN workbench_review_run r ON r.run_id=s.run_id WHERE r.matter_id=?', (matter_id,))],
            coverage=[dict(row) for row in self.connection.execute('SELECT * FROM workbench_entity_discovery_unit WHERE matter_id=?', (matter_id,))],
            # One compact row per source inventory and outcome, not per unit,
            # so coverage stays small regardless of source size.
            automatic_coverage=[dict(row) for row in self.connection.execute(
                "SELECT document_id,source_version_id,content_basis_digest,extractor_version,"
                "MAX(CASE WHEN unit_ordinal=0 THEN state END) AS inventory_state,"
                "SUM(unit_ordinal>0) AS units,"
                "SUM(unit_ordinal>0 AND state='processed') AS processed,"
                "SUM(unit_ordinal>0 AND state='pending') AS pending,"
                "SUM(unit_ordinal>0 AND state='failed') AS failed,"
                "SUM(unit_ordinal>0 AND state='invalidated') AS invalidated "
                'FROM workbench_entity_auto_discovery_unit WHERE matter_id=? '
                'GROUP BY document_id,source_version_id,content_basis_digest,extractor_version '
                'ORDER BY document_id,source_version_id,content_basis_digest,extractor_version', (matter_id,))],
            occurrence_tombstones=[row[0] for row in self.connection.execute('SELECT occurrence_key FROM workbench_entity_discovery_seen WHERE matter_id=?', (matter_id,))],
            reconciliations=[dict(row) for row in self.connection.execute('SELECT * FROM workbench_entity_reconciliation WHERE matter_id=?', (matter_id,))])

    def review_mention(self, matter_id, entity_id, mention_id, status):
        cursor = self.connection.execute('UPDATE workbench_entity_mention SET review_status=? WHERE matter_id=? AND entity_id=? AND mention_id=?',
            (status, matter_id, entity_id, mention_id))
        if cursor.rowcount != 1:
            raise KeyError(mention_id)
