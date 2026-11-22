"""Durable, matter-scoped inventory of a confirmed browser selection.

Selection metadata never authorizes a transfer. Existing upload admission remains
responsible for capacity, file validation, malware checks and processing.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import PurePosixPath

from .loose_file_preflight import PREFLIGHT_STATES, evaluate_loose_file_preflight
from .source_load_files import DAT_INTAKE_ROLES, FieldProfile
from .workspace_store import WorkspaceProblem, WorkspaceStore

MAX_SELECTION_ITEMS = 10_000
MAX_METADATA_BATCH_ITEMS = 2_000
MAX_METADATA_BATCH_BYTES = 6 * 1024 * 1024
# Receipts, reserved selection rows, and charged metadata bytes. Reservations
# count unfinished selections too; actual row text is charged before insertion.
RECEIPT_LIMITS = {
    'matter': (1_000, 100_000, 32 * 1024 * 1024),
    'actor': (2_000, 200_000, 64 * 1024 * 1024),
    'workspace': (10_000, 1_000_000, 128 * 1024 * 1024),
}
_RECEIPT = re.compile(r'intake-[0-9a-f]{32}')
_KEY = re.compile(r'[0-9a-f]{32}')
_FINGERPRINT = re.compile(r'[0-9a-f]{64}')

# A browser selection can exclude a metadata-valid file because the whole
# selection exceeded capacity or repeated a path. Preserve that observation
# separately; never accept arbitrary client explanation text as verified output.
_REVIEWED_REASONS = {
    'valid': 'Not included in the reviewed upload plan. Reselect the file to review it again.',
    'needs_attention': 'The file needed attention during selection review and was not included. Reselect it to review the current checks.',
    'unsupported': 'The file was marked unsupported during selection review and was not included.',
    'duplicate_candidate': 'This relative path repeated another selected file and was not included.',
    'over_limit': 'This file was outside the reviewed capacity-limited upload plan. Free space or choose another matter before trying again.',
    'failed': 'Selection review could not validate this file. Reselect it and try again.',
}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def _integer(value: object, minimum: int, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


def _profile_text(value, maximum):
    return (isinstance(value, str) and 0 < len(value) <= maximum and bool(value.strip())
            and not any(unicodedata.category(char) in {'Cc', 'Cf', 'Cs'} for char in value))


def _dat_mapping(data):
    if (not isinstance(data, dict) or set(data) != {'producer', 'sha256', 'fields'}
            or not _profile_text(data['producer'], 160)
            or not isinstance(data['sha256'], str) or not _FINGERPRINT.fullmatch(data['sha256'])
            or not isinstance(data['fields'], dict) or not 1 <= len(data['fields']) <= len(DAT_INTAKE_ROLES)
            or any(not _profile_text(header, 200) or not isinstance(role, str) or role not in DAT_INTAKE_ROLES
                   for header, role in data['fields'].items())):
        raise WorkspaceProblem('Choose a producer and valid DAT fields for this file.')
    try:
        profile = FieldProfile(data['producer'], data['fields'])
    except ValueError as exc:
        raise WorkspaceProblem('Assign each DAT field role only once.') from exc
    return profile.producer, data['sha256'], _json(sorted(profile.fields.items()))


# All joins include matter identity. A received file may fail parsing; byte
# receipt and current source availability intentionally remain distinct axes.
_PROJECTED = '''WITH linked AS (
    SELECT i.*,dp.producer,dp.digest AS inspected_digest,dp.fields_json,
      u.upload_session_id,u.state AS upload_state,u.received_size,
      u.document_id,u.message AS upload_message,
      c.document_id AS catalog_document_id,c.version_id,c.tone,c.source_state,
      ij.state AS ingest_state,mj.state AS media_state
    FROM workbench_intake_item i
    LEFT JOIN workbench_intake_dat_profile dp ON dp.receipt_id=i.receipt_id
      AND dp.ordinal=i.ordinal AND dp.matter_id=i.matter_id
    LEFT JOIN workbench_intake_transfer t ON t.receipt_id=i.receipt_id
      AND t.ordinal=i.ordinal AND t.matter_id=i.matter_id
    LEFT JOIN workbench_upload_item u ON u.upload_item_id=t.upload_item_id AND u.matter_id=i.matter_id
    LEFT JOIN workbench_source_catalog c ON c.document_id=u.document_id AND c.matter_id=i.matter_id AND c.version_id=t.source_version_id
    LEFT JOIN workbench_ingest_job ij ON ij.document_id=u.document_id AND ij.matter_id=i.matter_id
    LEFT JOIN workbench_media_job mj ON mj.document_id=u.document_id AND mj.matter_id=i.matter_id
      AND mj.source_version_id=c.version_id
    WHERE i.matter_id=? AND i.receipt_id=?
), projected AS (
    SELECT *,CASE WHEN selected_for_upload=1 THEN 'included' ELSE 'skipped' END AS selection_state,
      CASE WHEN received_size>=expected_size AND expected_size>0 THEN 'received'
           WHEN received_size>0 THEN 'partial' ELSE 'not_received' END AS transfer_state,
      CASE WHEN selected_for_upload=0 THEN 'not_uploaded'
           WHEN upload_state IN ('failed','cancelled') THEN 'failed'
           WHEN upload_state IS NULL THEN 'not_started'
           WHEN upload_state IN ('pending','uploading','uploaded') THEN 'uploading'
           WHEN catalog_document_id IS NULL THEN 'unavailable'
           WHEN ingest_state IN ('queued','running') OR media_state IN ('queued','running') THEN 'processing'
           WHEN source_state='playback_only' THEN 'playback_only'
           WHEN source_state='needs_review' THEN 'needs_review'
           WHEN ingest_state IN ('failed','cancelled') OR media_state IN ('failed','cancelled') THEN 'failed'
           WHEN tone='ready' THEN 'searchable'
           WHEN tone='attention' THEN 'needs_review' ELSE 'processing' END AS availability
    FROM linked
) '''


class IntakeUploadResumeMismatch(WorkspaceProblem):
    """An old checkpoint cannot be adopted; no receipt binding was changed."""


class IntakeReceipts:
    def __init__(self, workspace: WorkspaceStore):
        self.workspace = workspace
        self.connection = workspace.connection

    def _admit_locked(self, matter_id: str, actor_id: str, *, receipts: int = 0,
                      selected_rows: int = 0, metadata_bytes: int = 0) -> None:
        # Call inside the same BEGIN IMMEDIATE transaction as the write. Other
        # connections cannot consume the remaining budget between these steps.
        for scope, predicate, parameters in (
            ('matter', ' WHERE matter_id=?', (matter_id,)),
            ('actor', ' WHERE actor_id=?', (actor_id,)),
            ('workspace', '', ()),
        ):
            used = self.connection.execute(
                'SELECT count(*),COALESCE(sum(selected_count),0),COALESCE(sum(metadata_bytes),0) '
                'FROM workbench_intake_receipt' + predicate, parameters).fetchone()
            extra = (receipts, selected_rows, metadata_bytes)
            if any(current + added > maximum for current, added, maximum in zip(used, extra, RECEIPT_LIMITS[scope])):
                if scope == 'matter':
                    raise WorkspaceProblem('Selected-file receipt capacity is full for this matter. '
                        'Ask the matter owner to discard receipts without received data, or export your work and use another matter.')
                raise WorkspaceProblem('Selected-file receipt capacity is full for this account or workspace. '
                    'Ask the matter owner to discard receipts without received data, or contact the administrator.')

    def _receipt_locked(self, matter_id: str, actor_id: str, receipt_id: str, *,
                        write: bool = False, administrator_override: bool = False):
        if write:
            self.workspace.membership(matter_id, actor_id)
        else:
            self.workspace._authorize_export_read(matter_id, actor_id,
                administrator_override=administrator_override)
        if not isinstance(receipt_id, str) or not _RECEIPT.fullmatch(receipt_id):
            raise KeyError(receipt_id)
        row = self.connection.execute(
            'SELECT * FROM workbench_intake_receipt WHERE matter_id=? AND receipt_id=?',
            (matter_id, receipt_id),
        ).fetchone()
        if row is None or (write and row['actor_id'] != actor_id):
            raise KeyError(receipt_id)
        return row

    def create(self, matter_id: str, actor_id: str, *, selection_key: str,
               selection_fingerprint: str, selected_count: int,
               eligible_indexes: list[int], collection_name: str) -> dict:
        if (not isinstance(selection_key, str) or not _KEY.fullmatch(selection_key)
            or not isinstance(selection_fingerprint, str) or not _FINGERPRINT.fullmatch(selection_fingerprint)
            or not _integer(selected_count, 1, MAX_SELECTION_ITEMS)
            or not isinstance(eligible_indexes, list) or len(eligible_indexes) > selected_count
            or any(not _integer(i, 0, selected_count - 1) for i in eligible_indexes)
            or eligible_indexes != sorted(set(eligible_indexes))):
            raise WorkspaceProblem('The selected-file receipt request is invalid. Review the selection again.')
        if not isinstance(collection_name, str):
            raise WorkspaceProblem('The collection name is invalid.')
        title = self.workspace._safe_text(collection_name, label='Collection name', maximum=160)
        try:
            title_bytes = title.encode('utf-8')
        except UnicodeEncodeError as exc:
            raise WorkspaceProblem('Collection name contains unsupported characters.') from exc
        indexes = _json(eligible_indexes)
        with self.workspace._lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            self.workspace.membership(matter_id, actor_id)
            previous = self.connection.execute(
                'SELECT * FROM workbench_intake_receipt WHERE matter_id=? AND actor_id=? AND selection_key=?',
                (matter_id, actor_id, selection_key),
            ).fetchone()
            if previous is not None:
                if (previous['selection_fingerprint'], previous['selected_count'], previous['eligible_indexes_json'], previous['collection_name']) != (
                    selection_fingerprint, selected_count, indexes, title):
                    raise WorkspaceProblem('The saved selection changed. Review the files again.')
                receipt_id = previous['receipt_id']
            else:
                charged = 512 + len(title_bytes) + len(indexes.encode('utf-8'))
                self._admit_locked(matter_id, actor_id, receipts=1, selected_rows=selected_count,
                    metadata_bytes=charged)
                receipt_id = 'intake-' + uuid.uuid4().hex
                now = self.workspace._now()
                self.connection.execute(
                    'INSERT INTO workbench_intake_receipt VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (receipt_id, matter_id, actor_id, selection_key, selection_fingerprint, title,
                     selected_count, indexes, 'recording', now, now, charged),
                )
        return self.get(matter_id, actor_id, receipt_id)

    def append(self, matter_id: str, actor_id: str, receipt_id: str, *, start: int,
               files: list[object], reviewed_states: list[str], document_limit: int,
               media_limit: int, malware_scan_mode: str, scanner_ready: bool) -> dict:
        if (not _integer(start, 0, MAX_SELECTION_ITEMS - 1) or not isinstance(files, list)
            or not 1 <= len(files) <= MAX_METADATA_BATCH_ITEMS
            or not isinstance(reviewed_states, list) or len(reviewed_states) != len(files)
            or any(not isinstance(s, str) or s not in PREFLIGHT_STATES for s in reviewed_states)):
            raise WorkspaceProblem('The selected-file receipt batch is invalid. Review the selection again.')
        # Bound direct callers as well as HTTP requests; never stringify nested
        # client values or write part of a malformed metadata batch.
        for file in files:
            if (not isinstance(file, dict) or set(file) - {'name', 'relative_path', 'size', 'media_type', 'dat_profile'}
                or any(file.get(k) is not None and not isinstance(file[k], str)
                       for k in ('name', 'relative_path', 'media_type'))
                or (file.get('size') is not None and not _integer(file['size'], 0, 2**53 - 1))):
                raise WorkspaceProblem('The selection entry is invalid. Review the selection again.')
            if 'dat_profile' in file:
                _dat_mapping(file['dat_profile'])
        try:
            encoded_size = len(json.dumps([files, reviewed_states], ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        except UnicodeEncodeError as exc:
            raise WorkspaceProblem('The selection entry contains invalid text. Review the selection again.') from exc
        if encoded_size > MAX_METADATA_BATCH_BYTES:
            raise WorkspaceProblem('The selected-file receipt batch is too large.')
        evaluated = evaluate_loose_file_preflight(files, document_limit=document_limit,
            media_limit=media_limit, malware_scan_mode=malware_scan_mode, scanner_ready=scanner_ready)
        digests = [hashlib.sha256(_json([f, s]).encode()).hexdigest() for f, s in zip(files, reviewed_states)]
        with self.workspace._lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            receipt = self._receipt_locked(matter_id, actor_id, receipt_id, write=True)
            if start + len(files) > receipt['selected_count']:
                raise WorkspaceProblem('This receipt batch exceeds the confirmed selection.')
            eligible = set(json.loads(receipt['eligible_indexes_json']))
            pending, profiles = [], []
            for offset, (raw, result, digest) in enumerate(zip(files, evaluated['items'], digests)):
                ordinal = start + offset
                existing = self.connection.execute(
                    'SELECT descriptor_digest FROM workbench_intake_item WHERE receipt_id=? AND matter_id=? AND ordinal=?',
                    (receipt_id, matter_id, ordinal),
                ).fetchone()
                if existing is not None:
                    if existing['descriptor_digest'] != digest:
                        raise WorkspaceProblem('A saved selection entry changed. Review the files again.')
                    continue
                if receipt['state'] != 'recording':
                    raise WorkspaceProblem('This selection receipt has already been recorded.')
                included = ordinal in eligible
                reviewed_state = reviewed_states[offset]
                if included and (reviewed_state != 'valid' or result['state'] != 'valid' or not result['eligible']):
                    raise WorkspaceProblem('A selected file is no longer ready to upload. Review the selection again.')
                path = ''
                if result['path_safety_validated']:
                    path = str(PurePosixPath(unicodedata.normalize('NFC', raw.get('relative_path') or raw.get('name'))))
                size = result['size'] if _integer(result['size'], 0, 2**53 - 1) else None
                name = result['display_name'] if path else f'Selected file {ordinal + 1}'
                if 'dat_profile' in raw:
                    if not included or PurePosixPath(path).suffix.lower() != '.dat':
                        raise WorkspaceProblem('A DAT mapping requires a selected DAT file.')
                    profiles.append((receipt_id, matter_id, ordinal, *_dat_mapping(raw['dat_profile'])))
                reason = str(result['message'])
                reviewed_reason = reason
                if not included and (reviewed_state == 'valid' or reviewed_state != result['state']):
                    reviewed_reason = _REVIEWED_REASONS[reviewed_state]
                reviewed_reason = 'Browser-reported selection review: ' + reviewed_reason
                pending.append((receipt_id, matter_id, ordinal, digest, path, name, size,
                     result['expected_type'] or '', result['supplied_type'] or '', result['state'],
                     int(included), reason, reviewed_state, reviewed_reason))
            charged = sum(256 + sum(len(str(value).encode('utf-8')) for value in row if value is not None)
                for row in (*pending, *profiles))
            if pending:
                self._admit_locked(matter_id, actor_id, metadata_bytes=charged)
                self.connection.executemany('INSERT INTO workbench_intake_item VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', pending)
                self.connection.executemany('INSERT INTO workbench_intake_dat_profile VALUES (?,?,?,?,?,?)', profiles)
            self.connection.execute('UPDATE workbench_intake_receipt SET updated_at=?,metadata_bytes=metadata_bytes+? '
                'WHERE receipt_id=? AND matter_id=?', (self.workspace._now(), charged, receipt_id, matter_id))
        return self.get(matter_id, actor_id, receipt_id)

    def _has_received_data_locked(self, matter_id: str, receipt_id: str) -> bool:
        # Check complete bound sessions, including an unexpected row associated
        # with another receipt. Do not cancel work outside this selection.
        row = self.connection.execute('''SELECT
            EXISTS (SELECT 1 FROM workbench_intake_transfer t
                LEFT JOIN workbench_upload_item u ON u.matter_id=t.matter_id AND u.upload_item_id=t.upload_item_id
                WHERE t.matter_id=:matter_id AND t.receipt_id=:receipt_id AND u.upload_item_id IS NULL)
            OR EXISTS (SELECT 1 FROM workbench_upload_item u
                LEFT JOIN workbench_intake_transfer t ON t.matter_id=u.matter_id AND t.upload_item_id=u.upload_item_id
                WHERE u.matter_id=:matter_id AND u.upload_session_id IN (
                    SELECT i.upload_session_id FROM workbench_intake_transfer r
                    JOIN workbench_upload_item i ON i.matter_id=r.matter_id AND i.upload_item_id=r.upload_item_id
                    WHERE r.matter_id=:matter_id AND r.receipt_id=:receipt_id)
                AND (u.received_size<>0 OR u.document_id IS NOT NULL OR u.state IN ('uploaded','queued')
                    OR t.receipt_id IS NULL OR t.receipt_id<>:receipt_id OR t.source_version_id<>''))''',
            {'matter_id': matter_id, 'receipt_id': receipt_id}).fetchone()
        return bool(row[0])

    def discard(self, matter_id: str, actor_id: str, receipt_id: str, *, confirmed: bool = False,
                upload_is_empty: Callable[[str, int], bool] | None = None) -> tuple[str, ...]:
        """Owner cleanup; the caller holds the source mutation lock for bound uploads."""
        with self.workspace._lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            if self.workspace.membership(matter_id, actor_id).role != 'owner':
                raise KeyError(receipt_id)
            self._receipt_locked(matter_id, actor_id, receipt_id)
            if confirmed is not True:
                raise WorkspaceProblem('Confirm that you want to discard this receipt.')
            if self._has_received_data_locked(matter_id, receipt_id):
                raise WorkspaceProblem('Receipts with received data remain with the matter and cannot be discarded.')
            linked = self.connection.execute('''SELECT u.upload_item_id,u.upload_session_id,u.expected_size
                FROM workbench_intake_transfer t JOIN workbench_upload_item u
                ON u.matter_id=t.matter_id AND u.upload_item_id=t.upload_item_id
                WHERE t.matter_id=? AND t.receipt_id=?''', (matter_id, receipt_id)).fetchall()
            if linked and upload_is_empty is None:
                raise WorkspaceProblem('Open the receipt page to cancel these pending uploads before discarding the receipt.')
            for item in linked:
                if upload_is_empty(item['upload_item_id'], item['expected_size']) is not True:
                    raise WorkspaceProblem('This upload has received data. Keep its receipt and resume the upload.')
            now = self.workspace._now()
            for session_id in sorted({item['upload_session_id'] for item in linked}):
                self.connection.execute("UPDATE workbench_upload_item SET state='cancelled',"
                    "message='Upload cancelled by the matter owner',updated_at=? "
                    "WHERE matter_id=? AND upload_session_id=?", (now, matter_id, session_id))
                self.connection.execute("UPDATE workbench_upload_session SET state='cancelled',updated_at=? "
                    "WHERE matter_id=? AND upload_session_id=?", (now, matter_id, session_id))
            self.connection.execute('DELETE FROM workbench_intake_receipt WHERE matter_id=? AND receipt_id=?',
                (matter_id, receipt_id))
            return tuple(item['upload_item_id'] for item in linked)

    def seal(self, matter_id: str, actor_id: str, receipt_id: str) -> dict:
        with self.workspace._lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            receipt = self._receipt_locked(matter_id, actor_id, receipt_id, write=True)
            rows = self.connection.execute(
                'SELECT ordinal,relative_path,selected_for_upload FROM workbench_intake_item '
                'WHERE receipt_id=? AND matter_id=? ORDER BY ordinal', (receipt_id, matter_id),
            ).fetchall()
            if len(rows) != receipt['selected_count'] or [r['ordinal'] for r in rows] != list(range(receipt['selected_count'])):
                raise WorkspaceProblem('The selection receipt is incomplete. Reselect the same files to finish recording it.')
            included_paths = [r['relative_path'].casefold() for r in rows if r['selected_for_upload']]
            if len(included_paths) != len(set(included_paths)):
                raise WorkspaceProblem('The upload plan repeats a relative path. Review the selection again.')
            included_set = set(included_paths)
            for row in rows:
                if not row['selected_for_upload'] and row['relative_path'].casefold() in included_set:
                    self.connection.execute(
                        "UPDATE workbench_intake_item SET preflight_state='duplicate_candidate',reason=? "
                        "WHERE receipt_id=? AND matter_id=? AND ordinal=? AND preflight_state='valid'",
                        ('This relative path repeats an included file. This occurrence was not uploaded.', receipt_id, matter_id, row['ordinal']),
                    )
            self.connection.execute("UPDATE workbench_intake_receipt SET state='ready',updated_at=? WHERE receipt_id=? AND matter_id=?",
                (self.workspace._now(), receipt_id, matter_id))
        return self.get(matter_id, actor_id, receipt_id)

    def get(self, matter_id: str, actor_id: str, receipt_id: str, *, administrator_override: bool = False) -> dict:
        with self.workspace._lock:
            row = self._receipt_locked(matter_id, actor_id, receipt_id, administrator_override=administrator_override)
            totals = self.connection.execute(_PROJECTED + '''SELECT count(*) AS recorded,
                COALESCE(sum(selected_for_upload=1),0) AS included,
                COALESCE(sum(selected_for_upload=0),0) AS skipped,
                COALESCE(sum(transfer_state='received'),0) AS received,
                COALESCE(sum(transfer_state='partial'),0) AS partial,
                COALESCE(sum(selected_for_upload=1 AND transfer_state='not_received'),0) AS not_received,
                COALESCE(sum(availability='searchable'),0) AS searchable,
                COALESCE(sum(availability IN ('uploading','processing')),0) AS processing,
                COALESCE(sum(availability='playback_only'),0) AS playback_only,
                COALESCE(sum(availability='needs_review'),0) AS needs_review,
                COALESCE(sum(availability='failed'),0) AS failed,
                COALESCE(sum(availability='unavailable'),0) AS unavailable,
                COALESCE(sum(availability='not_started'),0) AS not_started FROM projected''', (matter_id, receipt_id)).fetchone()
            linked = self.connection.execute('SELECT 1 FROM workbench_intake_transfer '
                'WHERE matter_id=? AND receipt_id=? LIMIT 1', (matter_id, receipt_id)).fetchone()
            counts = dict(totals)
            counts['selected'] = row['selected_count']
            counts['unrecorded'] = row['selected_count'] - counts['recorded']
            return {k: row[k] for k in ('receipt_id', 'collection_name', 'selected_count', 'state', 'created_at', 'updated_at')} | {
                'recorded_count': counts['recorded'], 'counts': counts, 'has_upload_bindings': linked is not None,
                'has_received_data': self._has_received_data_locked(matter_id, receipt_id),
            }

    def items(self, matter_id: str, actor_id: str, receipt_id: str, *, offset: int = 0,
              limit: int = 100, administrator_override: bool = False) -> list[dict]:
        if not _integer(offset, 0, MAX_SELECTION_ITEMS) or not _integer(limit, 1, MAX_SELECTION_ITEMS):
            raise WorkspaceProblem('The receipt page is invalid.')
        with self.workspace._lock:
            self._receipt_locked(matter_id, actor_id, receipt_id, administrator_override=administrator_override)
            rows = self.connection.execute(_PROJECTED + '''SELECT ordinal,relative_path,display_name,expected_size,
                expected_type,supplied_type,preflight_state,reason,reviewed_state,reviewed_reason,selection_state,transfer_state,availability,
                COALESCE(received_size,0) AS received_size,document_id,catalog_document_id,version_id,
                producer,inspected_digest,fields_json,COALESCE(upload_message,'') AS upload_message
                FROM projected ORDER BY ordinal LIMIT ? OFFSET ?''',
                (matter_id, receipt_id, limit, offset)).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                producer, digest, fields = (item.pop(k) for k in ('producer', 'inspected_digest', 'fields_json'))
                if producer is not None:
                    item['dat_profile'] = {'producer': producer, 'sha256': digest, 'fields': dict(json.loads(fields))}
                items.append(dict(item, reviewed_basis='browser_report'))
            return items

    def producer_profile(self, matter_id: str, actor_id: str, producer: str) -> dict | None:
        """Return the mapping from the newest confirmed selection for this producer."""
        with self.workspace._lock:
            self.workspace.membership(matter_id, actor_id)
            if not _profile_text(producer, 160):
                raise WorkspaceProblem('Enter a producer name of up to 160 characters.')
            row = self.connection.execute(
                "SELECT p.fields_json FROM workbench_intake_dat_profile p JOIN workbench_intake_receipt r "
                "ON r.receipt_id=p.receipt_id AND r.matter_id=p.matter_id WHERE p.matter_id=? "
                "AND p.producer=? AND r.state='ready' ORDER BY r.created_at DESC,p.receipt_id DESC,p.ordinal DESC LIMIT 1",
                (matter_id, producer)).fetchone()
            return {'producer': producer, 'fields': dict(json.loads(row[0]))} if row else None

    @contextmanager
    def _read_snapshot(self):
        # The savepoint pins one SQLite version even across other connections.
        # Nested readers reuse the existing snapshot without committing writes.
        with self.workspace._lock:
            self.connection.execute('SAVEPOINT intake_snapshot')
            try:
                yield
            finally:
                self.connection.execute('RELEASE SAVEPOINT intake_snapshot')

    def snapshot(self, matter_id: str, actor_id: str, receipt_id: str, *,
                 offset: int = 0, limit: int = MAX_SELECTION_ITEMS,
                 administrator_override: bool = False) -> dict:
        with self._read_snapshot():
            receipt = self.get(matter_id, actor_id, receipt_id,
                administrator_override=administrator_override)
            receipt['items'] = self.items(matter_id, actor_id, receipt_id,
                offset=offset, limit=limit, administrator_override=administrator_override)
            return receipt

    def recent(self, matter_id: str, actor_id: str, *, limit: int = 10, offset: int = 0,
               administrator_override: bool = False) -> list[dict]:
        if not _integer(limit, 1, 1001) or not _integer(offset, 0, 1_000_000):
            raise WorkspaceProblem('The receipt page is invalid.')
        with self._read_snapshot():
            self.workspace._authorize_export_read(matter_id, actor_id, administrator_override=administrator_override)
            rows = self.connection.execute('SELECT receipt_id FROM workbench_intake_receipt WHERE matter_id=? '
                'ORDER BY created_at DESC,receipt_id DESC LIMIT ? OFFSET ?', (matter_id, limit, offset)).fetchall()
            return [self.get(matter_id, actor_id, r['receipt_id'], administrator_override=administrator_override) for r in rows]

    def export(self, matter_id: str, actor_id: str, *, administrator_override: bool = False) -> list[dict]:
        from .work_product_exports import MAX_BUNDLE_UNCOMPRESSED_BYTES

        with self._read_snapshot():
            self.workspace._authorize_export_read(matter_id, actor_id, administrator_override=administrator_override)
            # Bound materialization before reading a potentially large matter.
            total = self.connection.execute(
                'SELECT count(*),COALESCE(sum(length(CAST(relative_path AS BLOB)) + '
                'length(CAST(display_name AS BLOB)) + length(CAST(reason AS BLOB)) + length(CAST(reviewed_reason AS BLOB))),0) '
                'FROM workbench_intake_item WHERE matter_id=?', (matter_id,)).fetchone()
            if total[0] > 100_000 or total[1] > MAX_BUNDLE_UNCOMPRESSED_BYTES:
                raise WorkspaceProblem('No complete bundle was created: selected-file receipts exceed the export limit. Download receipts individually before closing the matter.')
            receipts = self.recent(matter_id, actor_id, limit=1001, administrator_override=administrator_override)
            if len(receipts) > 1000:
                raise WorkspaceProblem('No complete bundle was created: this matter has more than 1,000 intake receipts. Download receipts individually before closing the matter.')
            size = 0
            for receipt in receipts:
                receipt['items'] = [{k: v for k, v in row.items() if k not in {'document_id', 'catalog_document_id', 'version_id'}}
                    for row in self.items(matter_id, actor_id, receipt['receipt_id'], limit=MAX_SELECTION_ITEMS,
                        administrator_override=administrator_override)]
                del receipt['receipt_id']
                size += len(_json(receipt).encode('utf-8'))
                if size > MAX_BUNDLE_UNCOMPRESSED_BYTES:
                    raise WorkspaceProblem('No complete bundle was created: selected-file receipts exceed the export limit. Download receipts individually before closing the matter.')
            return receipts

    def validate_upload_locked(self, matter_id: str, actor_id: str, receipt_id: str,
                               ordinals: Sequence[int], files: Sequence[Mapping[str, object]]) -> str:
        """Inside the session creation transaction: return its prior exact session, if any."""
        receipt = self._receipt_locked(matter_id, actor_id, receipt_id, write=True)
        if (receipt['state'] != 'ready' or not isinstance(ordinals, (list, tuple))
            or not 1 <= len(files) <= MAX_METADATA_BATCH_ITEMS or len(ordinals) != len(files)
            or any(not _integer(i, 0, receipt['selected_count'] - 1) for i in ordinals)
            or len(set(ordinals)) != len(ordinals)):
            raise WorkspaceProblem('The upload does not match a complete selected-file receipt.')
        sessions = []
        for ordinal, file in zip(ordinals, files):
            row = self.connection.execute('SELECT i.*,u.upload_session_id FROM workbench_intake_item i '
                'LEFT JOIN workbench_intake_transfer t ON t.receipt_id=i.receipt_id AND t.ordinal=i.ordinal AND t.matter_id=i.matter_id '
                'LEFT JOIN workbench_upload_item u ON u.upload_item_id=t.upload_item_id AND u.matter_id=i.matter_id '
                'WHERE i.receipt_id=? AND i.matter_id=? AND i.ordinal=?', (receipt_id, matter_id, ordinal)).fetchone()
            if row is None or not row['selected_for_upload'] or (
                row['display_name'], row['relative_path'], row['expected_type'], row['expected_size']) != (
                file.get('display_name'), file.get('relative_path'), file.get('media_type'), file.get('expected_size')):
                raise WorkspaceProblem('The upload does not match the recorded selection. Review the files again.')
            sessions.append(row['upload_session_id'])
        if any(sessions):
            if not all(sessions) or len(set(sessions)) != 1:
                raise WorkspaceProblem('These receipt rows already belong to another upload batch. Resume the original selection.')
            session, items = self.workspace.upload_session(matter_id, actor_id, sessions[0])
            expected_ids = self.connection.execute('SELECT t.ordinal FROM workbench_intake_transfer t JOIN workbench_upload_item u '
                'ON u.upload_item_id=t.upload_item_id AND u.matter_id=t.matter_id WHERE t.matter_id=? AND t.receipt_id=? '
                'AND u.upload_session_id=? ORDER BY u.ordinal', (matter_id, receipt_id, sessions[0])).fetchall()
            if [r['ordinal'] for r in expected_ids] != list(ordinals) or session.state not in {'open', 'complete', 'partial'}:
                raise WorkspaceProblem('The recorded upload batch cannot be changed. Reselect the files for a new attempt.')
            return session.upload_session_id
        return ''

    def adopt_upload(self, matter_id: str, actor_id: str, receipt_id: str,
                     ordinals: Sequence[int], files: Sequence[Mapping[str, object]],
                     session_id: str, *, collection_id: str = '') -> str:
        """Attach a pre-receipt checkpoint's exact collection, retaining bytes.

        Earlier batches must also match included rows; a foreign or changed
        collection is never partly adopted. Uploaded document versions are
        immutable; missing catalog support remains unavailable.
        """
        with self.workspace._lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            prior = self.validate_upload_locked(matter_id, actor_id, receipt_id, ordinals, files)
            if prior:
                if prior != session_id:
                    raise WorkspaceProblem('The saved upload belongs to a different receipt batch.')
                return prior
            try:
                session, items = self.workspace.upload_session(matter_id, actor_id, session_id)
            except KeyError as exc:
                raise IntakeUploadResumeMismatch('The saved upload no longer matches this reviewed selection.') from exc
            manifest = [(i.display_name, i.relative_path, i.media_type, i.expected_size) for i in items]
            requested = [(f.get('display_name'), f.get('relative_path'), f.get('media_type'), f.get('expected_size')) for f in files]
            if (session.state not in {'open', 'complete', 'partial'} or manifest != requested
                or (collection_id and collection_id != session.collection_id)):
                raise IntakeUploadResumeMismatch('The saved upload no longer matches this reviewed selection. Previous bytes are retained.')
            selected = self.connection.execute(
                'SELECT * FROM workbench_intake_item WHERE matter_id=? AND receipt_id=? AND selected_for_upload=1',
                (matter_id, receipt_id)).fetchall()
            by_path = {row['relative_path']: row for row in selected}
            previous = self.connection.execute(
                'SELECT i.*,s.actor_id,c.version_id AS catalog_version FROM workbench_upload_item i '
                'JOIN workbench_upload_session s ON s.upload_session_id=i.upload_session_id AND s.matter_id=i.matter_id '
                'LEFT JOIN workbench_source_catalog c ON c.matter_id=i.matter_id AND c.document_id=i.document_id '
                "AND c.origin='upload' AND c.relative_path=i.relative_path AND c.byte_size=i.expected_size AND c.media_type=i.media_type "
                'WHERE s.matter_id=? AND s.collection_id=? ORDER BY s.created_at,s.upload_session_id,i.ordinal',
                (matter_id, session.collection_id)).fetchall()
            bindings = []
            seen = set()
            for item in previous:
                row = by_path.get(item['relative_path'])
                if (row is None or item['actor_id'] != actor_id or row['ordinal'] in seen
                    or (row['display_name'], row['expected_type'], row['expected_size']) !=
                    (item['display_name'], item['media_type'], item['expected_size'])):
                    raise WorkspaceProblem('The earlier upload collection does not match this selection. Previous bytes are retained.')
                seen.add(row['ordinal'])
                linked = self.connection.execute(
                    'SELECT receipt_id,ordinal FROM workbench_intake_transfer WHERE upload_item_id=?',
                    (item['upload_item_id'],)).fetchone()
                if linked is not None:
                    if (linked['receipt_id'], linked['ordinal']) != (receipt_id, row['ordinal']):
                        raise WorkspaceProblem('The earlier upload already belongs to another selection receipt. Open that receipt to resume.')
                else:
                    bound = self.connection.execute(
                        'SELECT 1 FROM workbench_intake_transfer WHERE receipt_id=? AND ordinal=?',
                        (receipt_id, row['ordinal'])).fetchone()
                    if bound:
                        raise WorkspaceProblem('The selected receipt row already belongs to another upload.')
                    bindings.append((row['ordinal'], item['upload_item_id'], item['catalog_version'] or ''))
            for ordinal, item_id, version in bindings:
                self.bind_upload_locked(matter_id, receipt_id, [ordinal], [item_id])
                self.connection.execute('UPDATE workbench_intake_transfer SET source_version_id=? '
                    'WHERE matter_id=? AND receipt_id=? AND ordinal=?', (version, matter_id, receipt_id, ordinal))
            return session_id

    def bind_upload_locked(self, matter_id: str, receipt_id: str, ordinals: Sequence[int], item_ids: Sequence[str]) -> None:
        for ordinal, item_id in zip(ordinals, item_ids):
            self.connection.execute('INSERT INTO workbench_intake_transfer(receipt_id,matter_id,ordinal,upload_item_id) VALUES (?,?,?,?)',
                (receipt_id, matter_id, ordinal, item_id))
        self.connection.execute('UPDATE workbench_intake_receipt SET updated_at=? WHERE receipt_id=? AND matter_id=?',
            (self.workspace._now(), receipt_id, matter_id))
