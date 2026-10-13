"""Disk-backed per-matter preservation archives, independent of presentation exports.

The caller admits closure and holds its response lease. This module never
acknowledges a download, registers consent, changes lifecycle, or imports data.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import sqlite3
import threading
import uuid
import zipfile
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path

from .matter_archive_projection import project_row, payload_coverage_manifest
from .matter_archive_schema import (EXCLUDED_TABLES, SCHEMA, VOLATILE_TABLES,
    coverage_manifest, exported_columns, scope_query, validate_schema, SOURCE_FIELDS, source_coverage_manifest)
from .source_locations import SourceScanItem
from .workspace_store import WorkspaceProblem

FORMAT = 'exculpata-preservation-v1'
BUFFER_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 100_000
_DIGEST = re.compile(r'[0-9a-f]{64}')
_SOURCE = re.compile(r'[0-9a-f]{32}')


class ArchiveProblem(WorkspaceProblem):
    """A content-free, staff-readable preservation failure."""


@dataclass(frozen=True)
class ArchiveArtifact:
    path: Path
    export_id: str
    exit_id: str
    sha256: str
    snapshot_digest: str
    manifest: dict
    cleanup_directory: Path

    def cleanup(self):
        cleanup_archive(self)


def cleanup_archive(artifact):
    """Remove this response's owned temporary output after completion/cancellation."""
    shutil.rmtree(artifact.cleanup_directory, ignore_errors=False)
    parent = artifact.cleanup_directory.parent
    if re.fullmatch(r'ci-matter-[0-9a-f]{32}', parent.name):
        try:
            parent.rmdir()
        except OSError:
            pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _hash_file(path):
    previous = None
    for attempt in range(2):
        observed = None
        try:
            with _regular(path) as stream:
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(BUFFER_BYTES), b''):
                    digest.update(chunk)
                observed = digest.hexdigest()
            if previous is not None and observed != previous:
                raise ArchiveProblem('Preservation archive changed during final verification.')
            return observed
        except ArchiveProblem:
            # Some filesystems update inode metadata during the first read. A
            # second complete stable read must produce precisely the same hash;
            # unsafe opens and persistent/content changes still fail closed.
            if attempt or observed is None:
                raise
            previous = observed


@contextmanager
def _regular(path, *, original_digest_checked=False):
    """Open every ancestor without following links, then hold the regular file."""
    path = Path(path).absolute()
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        target = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = os.fstat(target)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ArchiveProblem('A retained file is not a safe, exclusively owned regular file.')
            with os.fdopen(target, 'rb', closefd=False) as stream:
                yield stream
                after = os.fstat(target)
                fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_nlink')
                # Original source bytes are compared against the retained SHA256
                # by the consumer. ctime-only updates (e.g. filesystem read
                # metadata) cannot weaken that byte-for-byte integrity check.
                if not original_digest_checked:
                    fields += ('st_ctime_ns',)
                if any(getattr(before, key) != getattr(after, key) for key in fields):
                    raise ArchiveProblem('A retained file changed during preservation. Create a new archive.')
        finally:
            os.close(target)
    finally:
        os.close(fd)


class _Budget:
    def __init__(self, root, policy, lock=None, reserved=lambda: 0):
        self.root, self.policy, self.bytes = root, policy, 0
        self.lock = lock or threading.RLock()
        self.reserved = reserved

    def check(self, incoming=0):
        with self.lock:
            if self.bytes + incoming > self.policy.matter_quota_bytes:
                raise ArchiveProblem('The preservation archive exceeds the configured matter storage quota.')
            if shutil.disk_usage(self.root).free - incoming - self.reserved() < self.policy.reserve_bytes:
                raise ArchiveProblem('Preservation needs more free storage above the configured reserve.')

    def write(self, stream, value, *, account=False):
        with self.lock:
            self.check(len(value))
            stream.write(value)
            if account:
                self.bytes += len(value)


@contextmanager
def _temporary(bench, matter_id):
    if not re.fullmatch(r'ci-matter-[0-9a-f]{32}', matter_id):
        raise ArchiveProblem('Preservation matter identity is invalid.')
    base = bench.storage.root / 'preservation-exports'
    parent = base / matter_id
    if any(p.is_symlink() for p in (parent, *parent.parents)):
        raise ArchiveProblem('Preservation storage is unsafe.')
    for directory in (base, parent):
        directory.mkdir(mode=0o700, exist_ok=True)
        if not directory.is_dir() or stat.S_IMODE(directory.stat().st_mode) & 0o077:
            raise ArchiveProblem('Preservation storage must be private to the service account.')
    root = parent / ('building-' + uuid.uuid4().hex)
    root.mkdir(mode=0o700)
    try:
        yield root
    except BaseException:
        if root.exists():
            shutil.rmtree(root)
        raise
    finally:
        if not root.exists():
            try:
                parent.rmdir()
            except OSError:
                pass


def _write_record(stream, value, budget):
    encoded = _json(value) + b'\n'
    if len(encoded) > MAX_RECORD_BYTES:
        raise ArchiveProblem('A preservation record exceeds the bounded reader limit.')
    budget.write(stream, encoded, account=True)



def _references(value):
    if isinstance(value, dict):
        if isinstance(value.get('document_id'), str):
            yield dict(document_id=value['document_id'],
                       source_version_id=value.get('source_version_id', value.get('version_id')))
        for child in value.values():
            yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def _snapshot_controls(workspace, matter_id, root, budget, documents):
    """Called under source guard; only this bounded capture holds workspace lock."""
    files, counts = {}, {}
    unavailable = root / 'unavailable-references.jsonl'
    refs_fd = os.open(unavailable, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(refs_fd, 'wb') as refs:
        with workspace._lock:
            if workspace.connection.in_transaction:
                raise ArchiveProblem('Preservation requires an independent consistent read snapshot.')
            db = sqlite3.connect(workspace.path.absolute().as_uri() + '?mode=ro', uri=True)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('BEGIN')
            # Establish the independent WAL snapshot while the control lock is
            # held. Spooling and storage-capacity checks then release that lock.
            db.execute('SELECT matter_id FROM workbench_matter WHERE matter_id=?', (matter_id,)).fetchone()
        try:
            validate_schema(db)
            catalog = {row[0]: row[1] for row in db.execute(
                'SELECT document_id,version_id FROM workbench_source_catalog WHERE matter_id=?', (matter_id,))}
            if catalog != {key: value.version_id for key, value in documents.items()}:
                raise ArchiveProblem('Source inventory and registry disagree. Recover the retained originals before preserving.')
            for table in sorted(SCHEMA):
                if table in EXCLUDED_TABLES:
                    continue
                query = scope_query(table)
                selected = ','.join(f'\"{name}\"' for name in exported_columns(table))
                size = '+'.join(f'coalesce(length(CAST(\"{name}\" AS BLOB)),0)' for name in exported_columns(table))
                size_query = query.replace('SELECT ' + selected, 'SELECT ' + size)
                if any(row[0] > MAX_RECORD_BYTES for row in db.execute(size_query, (matter_id,))):
                    raise ArchiveProblem('A preservation record exceeds the bounded reader limit.')
                path = root / (table + '.jsonl')
                count = 0
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    for row in db.execute(query, (matter_id,)):
                        projected = project_row(table, row, matter_id)
                        _write_record(stream, projected, budget)
                        for reference in _references(projected):
                            doc = documents.get(reference['document_id'])
                            version = reference.get('source_version_id')
                            if doc is None or (version and version != doc.version_id):
                                _write_record(refs, dict(table=table, ordinal=count + 1,
                                    **reference, availability='previously_removed' if doc is None else 'superseded'), budget)
                        count += 1
                files[table] = path
                counts[table] = count
        finally:
            db.rollback()
            db.close()
    return files, counts, unavailable


def _check_source_boundary(store):
    from .pilot_uploads import _PILOT_DOCUMENT_FIELDS
    if _PILOT_DOCUMENT_FIELDS != SOURCE_FIELDS:
        raise ArchiveProblem('Source payload coverage requires review.')
    # Unadmitted/quarantined bytes cannot be read through an export bypass.
    for directory in (store.quarantine_root, store.incoming, store.staging):
        if directory.is_symlink() or not directory.is_dir():
            raise ArchiveProblem('Source staging or quarantine storage is unsafe.')
        if any(directory.iterdir()):
            raise ArchiveProblem('Unresolved staged or quarantined material blocks preservation. Resolve it before closing.')
    expected_files = {document.stored_name for document in store.documents.values() if document.stored_name}
    expected_derived = {name for document in store.documents.values()
                        for name in (document.units_file, document.playback_name) if name}
    for directory, expected in ((store.files, expected_files), (store.derived, expected_derived)):
        if directory.is_symlink() or not directory.is_dir() or {p.name for p in directory.iterdir()} != expected:
            raise ArchiveProblem('Unaccounted source or derived files block preservation. Recover their inventory before closing.')
    if store._registry is not None:
        tables = {r[0] for r in store._registry.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != {'source_registry_metadata', 'source_document'}:
            raise ArchiveProblem('Source registry coverage requires review.')
        for name, expected in {
            'source_registry_metadata': ('key', 'value'),
            'source_document': ('document_id', 'ordinal', 'payload'),
        }.items():
            if tuple(r[1] for r in store._registry.execute(f'PRAGMA table_info({name})')) != expected:
                raise ArchiveProblem('Source registry column coverage requires review.')


@contextmanager
def _original(bench, store, document, root, budget):
    if not _SOURCE.fullmatch(document.document_id) or not _DIGEST.fullmatch(document.digest):
        raise ArchiveProblem('A retained original has no verifiable identity or digest.')
    staged = None
    budget.check(document.size)
    if document.origin == 'upload':
        if not _SOURCE.fullmatch(document.stored_name):
            raise ArchiveProblem('A retained original has an unsafe storage reference.')
        path = store.files / document.stored_name
    elif document.origin == 'registered':
        item = SourceScanItem(document.relative_path, document.display_name, document.media_type,
            document.size, document.stable_device, document.stable_inode, document.stable_mtime_ns)
        from .case_lifecycle_support import archive_stage_reservation
        reservation = (archive_stage_reservation(bench, root.name, document.size)
                       if hasattr(bench, '_preservation_reservations') else nullcontext())
        # Reservation protects this bounded full-file staging copy without
        # holding the workspace or capacity lock across bulk I/O. Once staged,
        # actual disk usage accounts for these bytes and the reservation ends.
        with reservation:
            staged = bench.source_registry.stage(document.source_location_id, item, root)
        if staged.sha256 != document.digest or staged.byte_size != document.size:
            staged.path.unlink(missing_ok=True)
            raise ArchiveProblem('A registered original changed. Restore its verified version before preserving.')
        path = staged.path
    else:
        raise ArchiveProblem('A retained original has an unsupported origin.')
    try:
        with _regular(path, original_digest_checked=True) as stream:
            if os.fstat(stream.fileno()).st_size != document.size:
                raise ArchiveProblem('A retained original is missing or changed.')
            yield stream
    finally:
        if staged is not None:
            staged.path.unlink(missing_ok=True)


def _source_record(document):
    return {key: getattr(document, key) for key in (
        'document_id', 'display_name', 'media_type', 'size', 'state', 'digest',
        'version_id', 'page_count', 'origin', 'relative_path', 'processing_stage',
        'completed_units', 'total_units', 'duration_ms', 'has_video',
        'playback_state', 'playback_media_type', 'playback_size', 'transcript_index_basis')}



def _units(store, document):
    from .pilot_uploads import PilotUnit
    from .unit_stream import iter_unit_records
    if document.units_file:
        if document.units or document.units_file != document.document_id + '.json':
            raise ArchiveProblem('Retained extracted text has an ambiguous storage reference.')
        with _regular(store.derived / document.units_file) as binary:
            text = io.TextIOWrapper(binary, encoding='utf-8')
            try:
                for value in iter_unit_records(text, max_record_chars=MAX_RECORD_BYTES // 4):
                    yield PilotUnit(**value)
            finally:
                text.detach()
    else:
        for value in document.units:
            yield PilotUnit(**value)


def _capture_sources(bench, store, root, budget, consume):
    metadata = root / 'sources.jsonl'
    fd = os.open(metadata, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        for identifier in sorted(store.documents):
            document = store.documents[identifier]
            _write_record(output, _source_record(document), budget)
            with _original(bench, store, document, root, budget) as stream:
                consume(f'originals/{identifier}', stream, document.digest, document.size)
            units = root / (identifier + '.units.jsonl')
            fd = os.open(units, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as unit_output:
                for unit in _units(store, document):
                    _write_record(unit_output, asdict(unit), budget)
            with _regular(units) as stream:
                consume(f'extracted/{identifier}.jsonl', stream, None, units.stat().st_size)
            units.unlink()
            # Retained playback renditions are derived deliverables, not arbitrary
            # files under the sources tree. Their exact expected name is required.
            if document.playback_name:
                if document.playback_name != identifier + '.playback.mp4':
                    raise ArchiveProblem('A retained media rendition has an unsafe storage reference.')
                with _regular(store.derived / document.playback_name) as stream:
                    consume(f'playback/{identifier}.mp4', stream, None, document.playback_size)
    return metadata


def _consume(stream, output, budget, expected_digest=None, expected_size=None):
    digest, size = hashlib.sha256(), 0
    while chunk := stream.read(BUFFER_BYTES):
        budget.check(len(chunk))
        size += len(chunk)
        if expected_size is not None and size > expected_size:
            raise ArchiveProblem('A retained file changed size during preservation.')
        if output is not None:
            budget.write(output, chunk)
        digest.update(chunk)
    observed = digest.hexdigest()
    if (expected_size is not None and size != expected_size) or (
            expected_digest is not None and observed != expected_digest):
        raise ArchiveProblem('A retained file failed preservation integrity verification.')
    return observed, size


def _budget(bench, root):
    def reserved():
        return (bench.workspace.pending_upload_bytes() +
                getattr(bench, '_playback_reserved_bytes', lambda: 0)())
    return _Budget(root, bench.storage_policy, getattr(bench, '_storage_reservation_lock', None), reserved)


def _build(bench, matter, store, root, *, archive_path=None):
    budget = _budget(bench, root)
    budget.check()
    entries, output_bytes = {}, 0
    _check_source_boundary(store)
    files, counts, unavailable = _snapshot_controls(bench.workspace, matter.matter_id, root, budget, store.documents)
    archive = zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) if archive_path else None
    try:
        def consume(name, stream, expected_digest=None, expected_size=None, *, snapshot=True):
            nonlocal output_bytes
            if expected_size is not None and output_bytes + expected_size > bench.storage_policy.matter_quota_bytes:
                raise ArchiveProblem('Preservation output exceeds the configured matter quota.')
            if len(entries) >= MAX_ENTRIES:
                raise ArchiveProblem('Preservation inventory exceeds its explicit entry limit.')
            if archive is None:
                digest, size = _consume(stream, None, budget, expected_digest, expected_size)
            else:
                with archive.open(name, 'w', force_zip64=True) as target:
                    digest, size = _consume(stream, target, budget, expected_digest, expected_size)
            entries[name] = {'sha256': digest, 'bytes': size}
            output_bytes += size
            if output_bytes > bench.storage_policy.matter_quota_bytes:
                raise ArchiveProblem('Preservation output exceeds the configured matter quota.')
        from . import matter_archive_verifier
        for name, payload in (
                ('README.txt', _README.encode('utf-8')),
                ('verify_archive.py', Path(matter_archive_verifier.__file__).read_bytes())):
            consume(name, io.BytesIO(payload), expected_size=len(payload))
        for table, path in files.items():
            with _regular(path) as stream:
                consume(f'records/{table}.jsonl', stream, expected_size=path.stat().st_size,
                        snapshot=table not in VOLATILE_TABLES)
            path.unlink()
        with _regular(unavailable) as stream:
            consume('unavailable-references.jsonl', stream, expected_size=unavailable.stat().st_size)
        unavailable.unlink()
        metadata = _capture_sources(bench, store, root, budget, consume)
        with _regular(metadata) as stream:
            consume('sources.jsonl', stream, expected_size=metadata.stat().st_size)
        metadata.unlink()
        from .matter_archive_verifier import snapshot_from_entries
        return entries, counts, snapshot_from_entries(entries)
    finally:
        if archive is not None:
            archive.close()


def matter_snapshot_digest(bench, matter, source_store):
    """Re-read content and re-hash actual originals before irreversible purge."""
    try:
        with _temporary(bench, matter.matter_id) as root:
            try:
                with source_store.mutation_guard():
                    return _build(bench, matter, source_store, root)[2]
            finally:
                shutil.rmtree(root)
    except ArchiveProblem:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise ArchiveProblem('Preservation could not verify all retained matter content.') from exc


def build_matter_archive(bench, matter, actor_id, *, administrator_override=False, source_store=None):
    """Return a verified private ZIP; the caller owns cleanup and consent binding."""
    store = source_store if source_store is not None else bench.archive_source_store(matter)
    try:
        with _temporary(bench, matter.matter_id) as root, store.mutation_guard():
            with bench.workspace._lock:
                bench.workspace.case_lifecycle.read_matter(matter.slug, actor_id,
                    administrator_override=administrator_override)
                lifecycle = bench.workspace.matter_lifecycle(matter.matter_id)
                current = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
                if lifecycle.state != 'closing' or current is None or current.state != 'closing':
                    raise ArchiveProblem('Close the matter before creating its preservation archive.')
                exit_id = current.exit_id
            path = root / 'preservation.zip'
            entries, counts, snapshot = _build(bench, matter, store, root, archive_path=path)
            export_id = 'matter-export-' + uuid.uuid4().hex
            manifest = dict(format=FORMAT, export_id=export_id, exit_id=exit_id,
                matter_id=matter.matter_id, created_at=bench.workspace._now(),
                snapshot_digest=snapshot, entries=entries, table_rows=counts,
                coverage=coverage_manifest(), source_coverage=source_coverage_manifest(),
                payload_coverage=payload_coverage_manifest(),
                original_source_files_included=True,
                notices=[
                    'This is a preservation archive, not an automated restore/import format or legal retention guarantee.',
                    'Previously removed originals are unavailable; historical citations and authored text remain intact.',
                    'Clip definitions and original media are retained; ephemeral rendered clip downloads are not retained by the application.',
                    'Global identities, credentials, authorization grants, runtime requests, host paths and operational diagnostics are excluded; attribution is matter-local.',
                    'Record payloads preserve known authored content; unknown schema or payload fields block creation.',
                ])
            encoded = _json(manifest)
            if len(encoded) > MAX_MANIFEST_BYTES:
                raise ArchiveProblem('Preservation manifest exceeds its bounded verifier limit.')
            budget = _budget(bench, root)
            # Reserve the bounded manifest and ZIP directory/header overhead
            # under the same short capacity lock as other writers.
            overhead = sum(128 + len(name.encode('utf-8')) for name in entries) + 1024
            with budget.lock:
                budget.check(len(encoded) + overhead)
                with zipfile.ZipFile(path, 'a', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                    archive.writestr('manifest.json', encoded)
            if path.stat().st_size > bench.storage_policy.matter_quota_bytes:
                raise ArchiveProblem('Preservation output exceeds the configured matter quota.')
            os.chmod(path, 0o600)
            with path.open('rb') as stream:
                os.fsync(stream.fileno())
            from .matter_archive_verifier import verify_matter_archive
            verify_matter_archive(path)
            with bench.workspace._lock:
                current = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
                if current is None or current.exit_id != exit_id or current.state != 'closing':
                    raise ArchiveProblem('The closure changed during preservation. Create a new archive.')
            return ArchiveArtifact(path, export_id, exit_id, _hash_file(path), snapshot, manifest, root)
    except ArchiveProblem:
        raise
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ArchiveProblem('Preservation could not verify all retained matter content. No archive was certified.') from exc
    finally:
        if source_store is None:
            store.close()


_README = """Exculpata matter preservation archive — format v1

Keep the ZIP and the separately recorded SHA-256 together in your chosen secure
storage. This package contains confidential matter content. Protect it accordingly.

Verification (Python 3 standard library only; no Exculpata installation needed):
  1. Before extracting or running code, use a trusted external SHA-256 tool
     (sha256sum on Linux, shasum -a 256 on macOS) on preservation.zip. Compare its
     output to the digest recorded separately by the application. Stop if unequal.
  2. Only after that check, copy verify_archive.py to a trusted working directory.
  3. Run: python3 verify_archive.py preservation.zip --sha256 RECORDED_SHA256
Alternatively, use a trusted installed scripts/verify-matter-archive.py directly.
The verifier never extracts files. It checks the complete member inventory,
lengths, SHA-256 and ZIP CRC. A separately recorded digest detects replacement of
the whole archive; verification without that digest establishes only internal
consistency. Run the script only if you trust where this package came from.

Contents:
  manifest.json: format, closure/export references, SHA-256 and byte size of every
    other member, row counts, and explicit table/column/source-field coverage.
  sources.jsonl: one source inventory record per retained source, its original
    display name, media type, relative production path, origin and source digest.
  originals/<document_id>: exact original source/media bytes; use sources.jsonl
    to recover the display name and media type. No host storage paths are exposed.
  extracted/<document_id>.jsonl: all retained extracted units, including location,
    text, source digests, page/line or media timestamps and speaker cluster.
  playback/<document_id>.mp4: a retained browser playback rendition, if present.
  unavailable-references.jsonl: historical references whose original is removed
    or superseded. Their saved excerpts and human work remain in record files.
  records/*.jsonl: one JSON object per line for the table named by the filename.

Record families cover matter purpose and context, source roles and collections,
intake receipts and outcomes, conversations and messages, notebooks and support,
reports/sections/citations, human assertions/entities and their complete retained
histories, research jobs/checkpoints, full-review decisions and text-range ledgers,
failed/cancelled jobs and events, media transcripts with machine text and every
retained text/speaker revision, exact media clip recipes, closure/retention history
and a scoped audit chronology. Empty retained tables have empty files and zero
row counts. Failed work keeps its state and retained results; it is not marked as
completed by this export.

Clip downloads are generated temporarily by the application. Their permanent
records preserve original media, time ranges and titles; this archive does not
claim to contain a previously downloaded rendering. Originals previously removed
from the matter are not reconstructed. Missing/corrupt currently retained bytes,
quarantine, pending staged material, unknown storage schemas and unknown payload
fields prevent certification rather than silently omitting them.

Global identities, authentication sessions, credentials, authorization grants,
host paths, live request/lease identifiers, exact runtime transport requests and
unstructured operational diagnostics are excluded. The manifest records these
boundaries. Authorship uses stable matter-local pseudonyms; it does not include
account directory details. Known authored text is preserved without regex
redaction, including any paths or names the author deliberately wrote in it.

This is an inspectable preservation format, not an automated restore/import tool,
a proof of evidentiary completeness, or a legal retention guarantee. No deletion
is authorized by creating or verifying this archive. Review the saved material,
keep the archive safe, and follow the separate application confirmation process.
"""
